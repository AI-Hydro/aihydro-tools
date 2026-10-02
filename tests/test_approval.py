"""Human approval records and promotion refusal (Slice 1b, ADR-002a).

Covers: claim revision digest, the approval store, ``promote_claim_to_registry``
refusal/acceptance, legacy ``self_asserted`` labelling, the defensibility
report, the ``aihydro-approve`` CLI, and registry write locking.
All IO is under per-test temp dirs (``tests/conftest.py`` isolates AIHYDRO_HOME).
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from aihydro_core.records import Actor, digest

from ai_hydro.approval import records
from ai_hydro.approval.cli import main as approve_cli
from ai_hydro.approval.records import (
    APPROVAL_REQUIRED,
    CHANNEL,
    claim_revision_digest,
    claim_revision_fields,
    find_approval,
    session_claim_revision,
    verify_record,
)
from ai_hydro.approval.writer import write_approval
from ai_hydro.mcp.tools_ledger import list_registry_claims, promote_claim_to_registry
from ai_hydro.registry import store as registry
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from approval_helpers import approve, basin_ref, basin_ref_full, retained

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)


def _claim(**over):
    base = {
        "id": "c1", "claim": "Synthetic evaluation NSE is 0.8",
        "claim_type": "empirical_result", "status": "supported", "confidence": "medium",
        "confidence_rationale": "Synthetic regression fixture only.",
        "scope": {"basins": ["synthetic"], "period": "2000-2001", "metric": "nse",
                  "basin_refs": [basin_ref("synthetic")]},
        "evidence_spans": [{"source_type": "run", "source_id": "r1", "metric_ref": "nse"}],
        "limitations": ["Synthetic regression case, no real research conclusion."],
        "uncertainty_verified": True,
        "basin_ref_records": retained("synthetic"),
    }
    base.update(over)
    return base


EV = {"r1": "sha256-v2:" + "a" * 64}


def _rev(claim, ev=None):
    """Revision digest of a claim dict with fixed evidence fingerprints."""
    return claim_revision_digest(claim, EV if ev is None else ev)


def _session_rev(session_id="appr", claim_id="c1"):
    return session_claim_revision(HydroSession.load(session_id), claim_id)[-1]


def _run_record(session_id):
    from ai_hydro.session.evidence import capture_result_evidence
    result = {"data": {"nse": 0.8, "_uncertainty": {"nse": {
        "value": 0.8, "ci_low": 0.7, "ci_high": 0.9, "ci_level": 0.95, "n": 30,
        "method": "synthetic_fixture"}}},
        "quality_flags": [{"validator": "fixture_check", "status": "pass"}]}
    return {"run_id": "r1", "session_id": session_id, "tool_name": "fixture",
            "key_outputs": {"nse": 0.8}, "evidence": capture_result_evidence(result)}


@pytest.fixture
def session():
    s = HydroSession("appr")
    s.claims["c1"] = _claim()
    s.set("_run_log", {"r1": _run_record("appr")})
    s.save()
    return s


def _promote():
    return promote_claim_to_registry("appr", "c1", researcher_approved=True)


# --------------------------------------------------------------------------
# Claim revision digest
# --------------------------------------------------------------------------

def test_revision_digest_is_stable_and_normalised():
    d = _rev(_claim())
    assert d.startswith("sha256:") and len(d) == len("sha256:") + 64
    assert _rev(_claim()) == d
    # Storage spelling/defaults do not matter: omitting defaulted fields is the same revision.
    spelled = _claim(scope={"basins": ["synthetic"], "period": "2000-2001", "metric": "nse",
                            "basin_refs": [basin_ref("synthetic")], "forcing": None, "model_versions": {}})
    assert _rev(spelled) == d
    assert claim_revision_fields(_claim(), EV)["schema"] == "aihydro.claim_revision/2"


@pytest.mark.parametrize("change", [
    {"claim": "Synthetic evaluation NSE is 0.9"},
    {"claim_type": "negative_result"},
    {"claim_type": "hypothesis"},
    {"prereg_id": "prereg-forged"},
    {"uncertainty_verified": False},
    {"confidence_rationale": "A different rationale, long enough to be valid."},
    {"scope": {"basins": ["other"], "period": "2000-2001", "metric": "nse"}},
    {"scope": {"basins": ["synthetic"], "period": "2000-2002", "metric": "nse"}},
    {"scope": {"basins": ["synthetic"], "period": "2000-2001", "metric": "kge"}},
    {"status": "weakly_supported"},
    {"confidence": "high"},
    {"evidence_spans": [{"source_type": "run", "source_id": "r2", "metric_ref": "nse"}]},
    {"evidence_spans": [{"source_type": "run", "source_id": "r1", "metric_ref": "kge"}]},
    {"evidence_spans": []},
    {"limitations": ["A different limitation."]},
    {"limitations": []},
])
def test_each_authority_field_changes_the_digest(change):
    assert _rev(_claim(**change)) != _rev(_claim())


def test_retained_evidence_fingerprints_change_the_digest():
    assert _rev(_claim(), {"r1": "sha256-v2:" + "b" * 64}) != _rev(_claim())
    assert _rev(_claim(), {}) != _rev(_claim())


def test_every_field_copied_into_the_registry_row_is_bound(session, ):
    """Rule: anything that lands in the registry entry and carries authority is in the digest."""
    approve("appr", "c1")
    assert _promote()["status"] == "promoted"
    entry, = registry.all_entries()
    bound = claim_revision_fields(HydroSession.load("appr").claims["c1"], entry["evidence_versions"])
    assert entry["statement"] == bound["text"]
    for row_key, field in [("claim_type", "claim_type"), ("confidence", "confidence"),
                           ("evidence_spans", "evidence_spans"), ("limitations", "limitations"),
                           ("scope", "scope"), ("prereg_id", "prereg_id"),
                           ("evidence_versions", "evidence_versions")]:
        assert entry[row_key] == bound[field], row_key


@pytest.mark.parametrize("change", [
    {"promoted": True, "registry_id": "reg.x", "promoted_at": "2026-01-01T00:00:00Z"},
    {"updated_at": "2030-01-01T00:00:00+00:00"},
    {"citations": ["doi:10.0/x"]},
    {"contradictions": ["c9"]},
])
def test_bookkeeping_fields_do_not_change_the_digest(change):
    assert _rev(_claim(**change)) == _rev(_claim())


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

def test_record_shape_and_seal():
    rev = _rev(_claim())
    rec = write_approval("s", "c", rev, Actor(kind="human", id="alice"), "looks right")
    assert set(rec) == {"schema", "claim_id", "session_id", "claim_revision_digest",
                        "approver", "channel", "approved_at", "statement", "record_digest"}
    assert rec["channel"] == CHANNEL == "cli_same_user"
    assert rec["approver"] == {"kind": "human", "id": "alice"}
    assert rec["approved_at"].endswith("Z")
    assert rec["record_digest"] == digest({k: v for k, v in rec.items() if k != "record_digest"})
    assert verify_record(rec)
    assert find_approval("s", "c", rev) == rec


@pytest.mark.parametrize("kind", ["agent", "tool", "system"])
def test_writer_refuses_non_human_actor(kind):
    with pytest.raises(ValueError):
        write_approval("s", "c", "sha256:" + "0" * 64, Actor(kind=kind, id="x"), "no")
    assert not records.approvals_file().exists()


def test_store_is_append_only():
    rev = _rev(_claim())
    write_approval("s", "c1", rev, Actor(kind="human", id="alice"), "one")
    first = records.approvals_file().read_bytes()
    write_approval("s", "c2", rev, Actor(kind="human", id="alice"), "two")
    second = records.approvals_file().read_bytes()
    assert second.startswith(first) and len(second) > len(first)


def test_tampered_or_forged_records_are_ignored():
    rev = _rev(_claim())
    rec = write_approval("s", "c", rev, Actor(kind="human", id="alice"), "ok")
    path = records.approvals_file()
    # edit a field in place without resealing
    path.write_text(path.read_text().replace('"alice"', '"mallory"'))
    assert find_approval("s", "c", rev) is None
    # an unsealed forged line
    forged = {k: v for k, v in rec.items() if k != "record_digest"}
    path.write_text(json.dumps(forged) + "\n" + "not json\n")
    assert find_approval("s", "c", rev) is None
    # a sealed record whose approver is not human never verifies
    agent = records.seal_record({**forged, "approver": {"kind": "agent", "id": "bot"}})
    path.write_text(json.dumps(agent) + "\n")
    assert find_approval("s", "c", rev) is None


def test_approval_path_honours_aihydro_home_at_call_time(tmp_path, monkeypatch):
    a, b = tmp_path / "home_a", tmp_path / "home_b"
    monkeypatch.setenv("AIHYDRO_HOME", str(a))
    assert records.approvals_dir() == a / "approvals"
    rev = _rev(_claim())
    write_approval("s", "c", rev, Actor(kind="human", id="alice"), "ok")
    monkeypatch.setenv("AIHYDRO_HOME", str(b))
    assert records.approvals_dir() == b / "approvals"
    assert find_approval("s", "c", rev) is None          # b has no approvals
    assert (a / "approvals" / "approvals.jsonl").exists()


# --------------------------------------------------------------------------
# Promotion
# --------------------------------------------------------------------------

def test_promotion_without_approval_is_refused(session):
    res = _promote()
    assert res["error"] is True
    assert res["code"] == APPROVAL_REQUIRED == "APPROVAL_REQUIRED"
    assert res["approval_command"] == "aihydro-approve appr c1"
    assert "aihydro-approve appr c1" in res["recovery"]
    assert "claim_revision_digest" not in res        # the refusal does not hand the digest to the agent
    assert _session_rev() not in json.dumps(res)
    assert registry.all_entries() == []
    assert not HydroSession.load("appr").claims["c1"].get("promoted")


def test_researcher_approved_flag_alone_never_suffices(session):
    for flag in (True, False):
        res = promote_claim_to_registry("appr", "c1", researcher_approved=flag)
        assert res["code"] == APPROVAL_REQUIRED
    assert registry.all_entries() == []


def test_flag_false_is_refused_even_with_an_approval_record(session):
    approve("appr", "c1")
    res = promote_claim_to_registry("appr", "c1", researcher_approved=False)
    assert res["code"] == APPROVAL_REQUIRED and "approval" in res["message"]
    assert registry.all_entries() == []


def test_promotion_with_matching_approval_succeeds_and_stamps_the_entry(session):
    rec = approve("appr", "c1", approver="alice")
    res = _promote()
    assert res["status"] == "promoted", res
    # A2: the stamp is verifier-derived. A v1 record verified under the test
    # suite's explicit development opt-out is labelled as such, never as signed.
    expected = {"record_digest": rec["record_digest"], "channel": "cli_same_user",
                "trust_root": None, "principal": None, "signer": None,
                "policy": "unsigned_opt_out"}
    assert res["approval"] == {**expected, "approver": "alice"}
    entry, = registry.all_entries()
    assert entry["approval"] == expected


@pytest.mark.parametrize("edit", [
    lambda c: c.update(claim="Synthetic evaluation NSE is 0.85"),
    lambda c: c.update(confidence="high"),
    lambda c: c.update(status="weakly_supported"),
    lambda c: c["limitations"].append("An added limitation."),
    lambda c: c["scope"].update(period="2000-2002"),
    lambda c: c["evidence_spans"][0].update(metric_ref="data.nse"),
    lambda c: c.update(claim_type="negative_result"),
    lambda c: c.update(prereg_id="prereg-forged"),
    lambda c: c.update(confidence_rationale="A rewritten rationale that is long enough."),
], ids=["text", "confidence", "status", "limitations", "scope", "evidence",
        "claim_type", "prereg_id", "confidence_rationale"])
def test_editing_the_claim_invalidates_the_approval(session, edit):
    approve("appr", "c1")
    s = HydroSession.load("appr")
    edit(s.claims["c1"])
    s.save()
    res = _promote()
    assert res["code"] == APPROVAL_REQUIRED, res
    assert registry.all_entries() == []
    # approving the new revision makes it promotable again
    approve("appr", "c1")
    assert _promote()["status"] == "promoted"


def _mutate_run(mutator):
    s = HydroSession.load("appr")
    rec = _run_record("appr")
    mutator(rec)
    s.set("_run_log", {"r1": rec})
    s.save()


def test_mutating_retained_evidence_after_approval_invalidates_it(session):
    """B2: the approval binds the fingerprints of the retained runs, not just the claim text."""
    approve("appr", "c1")

    def worse(rec):
        rec["evidence"]["data"]["nse"] = 0.3
        rec["evidence"]["uncertainty"]["nse"].update(value=0.3, ci_low=0.2, ci_high=0.4)
    _mutate_run(worse)
    res = _promote()
    assert res["code"] == APPROVAL_REQUIRED, res
    assert registry.all_entries() == []
    approve("appr", "c1")                       # approving what is now retained works
    assert _promote()["status"] == "promoted"


def test_cli_shows_and_binds_the_same_evidence_fingerprints(session):
    expected = session_claim_revision(HydroSession.load("appr"), "c1")
    fingerprint = expected[1]["r1"]
    assert fingerprint.startswith("sha256-v2:")
    code, out, err = _run_cli(["appr", "c1", "--approver", "alice"], typed=expected[3].split(":")[1][:12] + "\n")
    assert code == 0, err
    assert fingerprint in out and expected[3] in out
    # what the CLI approved is exactly what promotion computes from verified_versions
    assert _promote()["status"] == "promoted"
    assert registry.all_entries()[0]["evidence_versions"] == expected[1]


def test_cli_approval_goes_stale_when_evidence_changes(session):
    rev = _session_rev()
    assert _run_cli(["appr", "c1", "--approver", "a"], typed=rev.split(":")[1][:12] + "\n")[0] == 0
    _mutate_run(lambda rec: rec["evidence"]["data"].update(nse=0.8, extra=1))
    assert _session_rev() != rev
    assert _promote()["code"] == APPROVAL_REQUIRED


def test_an_approval_authorises_exactly_one_promotion(session):
    """B4: one approval, one promotion; a second row needs a fresh approval."""
    rec = approve("appr", "c1")
    first = _promote()
    assert first["status"] == "promoted"
    # same approval, same claim, same evidence: refused (re-promotion needs re-approval)
    again = _promote()
    assert again["code"] == APPROVAL_REQUIRED and "already used" in again["message"]
    assert len(registry.all_entries()) == 1
    # a fresh approval of the same revision is accepted (idempotent registry id)
    rec2 = approve("appr", "c1")
    assert rec2["record_digest"] != rec["record_digest"]
    assert _promote()["registry_id"] == first["registry_id"]


def test_registry_refuses_a_second_row_citing_the_same_approval():
    row = {"claim_id": "c", "session_id": "s", "status": "promoted",
           "approval": {"record_digest": "sha256:" + "1" * 64}}
    registry.append({**row, "registry_id": "reg.a"})
    registry.append({**row, "registry_id": "reg.a"})           # same id: idempotent no-op
    with pytest.raises(registry.ApprovalAlreadyConsumed):
        registry.append({**row, "registry_id": "reg.b"})
    assert [e["registry_id"] for e in registry.all_entries()] == ["reg.a"]


def test_cli_issues_a_fresh_approval_when_the_previous_one_was_consumed(session):
    approve("appr", "c1")
    assert _promote()["status"] == "promoted"
    code, out, _ = _run_cli(["appr", "c1", "--approver", "a"], typed=_session_rev().split(":")[1][:12] + "\n")
    assert code == 0 and "Already approved" not in out


def test_a_correctly_sealed_forged_record_is_accepted_by_design(session):
    """Documents a NEGATIVE capability: the store does not resist a same-user forger.

    The seal is an unkeyed digest, so any process that can write the approvals
    file (or drive the CLI through a pty) can mint a record that verifies. This
    test exists so nobody cites the tamper tests above as forgery resistance.
    ADR-002b (client-held signing key) is the planned fix; until then records
    are labelled channel ``cli_same_user``.
    """
    forged = records.seal_record({
        "schema": records.APPROVAL_SCHEMA, "claim_id": "c1", "session_id": "appr",
        "claim_revision_digest": _session_rev(), "approver": {"kind": "human", "id": "forged"},
        "channel": CHANNEL, "approved_at": "2026-01-01T00:00:00Z", "statement": "forged"})
    records.approvals_dir().mkdir(parents=True, exist_ok=True)
    records.approvals_file().write_text(json.dumps(forged) + "\n")
    res = _promote()
    assert res["status"] == "promoted"
    assert res["approval"]["channel"] == "cli_same_user"      # the label is the honest part


def test_approval_is_bound_to_session_and_claim(session):
    rev = _session_rev()
    write_approval("other-session", "c1", rev, Actor(kind="human", id="alice"), "wrong session")
    write_approval("appr", "c-other", rev, Actor(kind="human", id="alice"), "wrong claim")
    assert _promote()["code"] == APPROVAL_REQUIRED


def test_other_gates_run_before_the_approval_check(session):
    s = HydroSession.load("appr")
    s.claims["c1"]["evidence_spans"] = []
    s.save()
    res = _promote()
    assert res["code"] != APPROVAL_REQUIRED and "evidence" in res["message"]


# --------------------------------------------------------------------------
# Legacy labelling and the defensibility report
# --------------------------------------------------------------------------

def _legacy_row():
    return {"registry_id": "reg.legacy.20260101.aaa", "claim_id": "old", "session_id": "appr",
            "status": "promoted", "evidence_versions": {}, "staleness": None}


def test_legacy_rows_are_labelled_self_asserted_without_being_rewritten(session):
    registry.append(_legacy_row())
    raw_before = registry.claims_file().read_bytes()
    approve("appr", "c1")
    assert _promote()["status"] == "promoted"
    listing = list_registry_claims(session_id="appr")
    by_claim = {e["claim_id"]: e for e in listing["entries"]}
    assert by_claim["old"]["approval"] == "self_asserted"
    assert isinstance(by_claim["c1"]["approval"], dict)
    assert listing["n_self_asserted"] == 1
    assert registry.claims_file().read_bytes().startswith(raw_before)   # legacy row untouched
    assert "approval" not in registry.all_entries()[0]


def test_defensibility_report_labels_promotions(session):
    from ai_hydro.reports.defensibility import build_defensibility_report
    approve("appr", "c1")
    assert _promote()["status"] == "promoted"
    s = HydroSession.load("appr")
    md, summary = build_defensibility_report(s, "appr", "2026-10-02")
    assert "approved (channel cli_same_user), record sha256:" in md and "self_asserted" not in md
    assert summary["n_promoted_claims"] == 1 and summary["n_self_asserted_promotions"] == 0

    # a legacy (unstamped) registry row for the same claim shows self_asserted
    s.claims["c1"]["registry_id"] = "reg.legacy.20260101.aaa"
    md, summary = build_defensibility_report(s, "appr", "2026-10-02",
                                             registry_entries=[_legacy_row()])
    assert "`approval: self_asserted`" in md
    assert summary["n_self_asserted_promotions"] == 1

    s.claims["c1"]["registry_id"] = "reg.gone"
    md, _ = build_defensibility_report(s, "appr", "2026-10-02", registry_entries=[])
    assert "unknown (registry entry not found)" in md


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

class _Stream(io.StringIO):
    def __init__(self, text="", tty=True):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


def _run_cli(argv, typed="", tty=True):
    out, err = _Stream(tty=tty), _Stream(tty=tty)
    code = approve_cli(argv, stdin=_Stream(typed, tty=tty), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_refuses_when_not_a_tty(session):
    rev = _session_rev()
    code, out, err = _run_cli(["appr", "c1"], typed=rev.split(":")[1][:12] + "\n", tty=False)
    assert code == 3 and "non-interactively" in err
    assert not records.approvals_file().exists()


def test_cli_real_subprocess_without_a_terminal_cannot_approve(session, tmp_path):
    env = {**os.environ, "AIHYDRO_HOME": str(tmp_path / "h"), "HOME": str(tmp_path),
           "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO), os.environ.get("PYTHONPATH", "")]))}
    rev = _session_rev()
    proc = subprocess.run([sys.executable, "-m", "ai_hydro.approval.cli", "appr", "c1"],
                          input=rev.split(":")[1][:12] + "\n", capture_output=True, text=True,
                          env=env, cwd=str(REPO), timeout=120)
    assert proc.returncode == 3, proc.stderr
    assert not (tmp_path / "h" / "approvals").exists()


@pytest.mark.skipif(os.name != "posix", reason="needs a POSIX pseudo-terminal")
def test_cli_real_subprocess_on_a_pseudo_terminal_records_the_approval(tmp_path, monkeypatch):
    """End to end through the console entry module with a real TTY on stdin/stdout."""
    import pty
    import select

    home = tmp_path / "h"
    monkeypatch.setattr(store, "_SESSIONS_DIR", home / ".aihydro" / "sessions")
    s = HydroSession("appr-pty")
    s.claims["c1"] = _claim()
    s.save()
    rev = _session_rev("appr-pty")

    env = {**os.environ, "AIHYDRO_HOME": str(home / "state"), "HOME": str(home),
           "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO), os.environ.get("PYTHONPATH", "")]))}
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, "-m", "ai_hydro.approval.cli", "appr-pty", "c1",
                             "--approver", "alice"],
                            stdin=slave, stdout=slave, stderr=slave, env=env, cwd=str(REPO), close_fds=True)
    os.close(slave)
    seen, typed, deadline = b"", False, time.time() + 120
    while time.time() < deadline:
        ready, _, _ = select.select([master], [], [], 0.5)
        if ready:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            seen += chunk
        if not typed and b"hex characters" in seen:
            os.write(master, (rev.split(":")[1][:12] + "\n").encode())
            typed = True
        if proc.poll() is not None and not ready:
            break
    assert proc.wait(timeout=30) == 0, seen.decode(errors="replace")
    os.close(master)
    monkeypatch.setenv("AIHYDRO_HOME", str(home / "state"))
    assert find_approval("appr-pty", "c1", rev)["approver"]["id"] == "alice"


def test_cli_rejects_a_yes_flag(session):
    code, _, _ = _run_cli(["appr", "c1", "--yes"])
    assert code == 2


def test_cli_shows_the_claim_and_records_the_approval_on_typed_confirmation(session):
    rev = _session_rev()
    code, out, err = _run_cli(["appr", "c1", "--approver", "alice"],
                              typed=rev.split(":")[1][:12] + "\n")
    assert code == 0, err
    for needle in ("Synthetic evaluation NSE is 0.8", "basins=['synthetic']", "run: r1",
                   "metric_ref=nse", "Synthetic regression case", rev):
        assert needle in out
    rec = find_approval("appr", "c1", rev)
    assert rec and rec["approver"] == {"kind": "human", "id": "alice"}
    assert _promote()["status"] == "promoted"


def test_cli_accepts_the_sha256_prefixed_form(session):
    rev = _session_rev()
    code, _, _ = _run_cli(["appr", "c1", "--approver", "a"], typed=rev[:7 + 12] + "\n")
    assert code == 0


def test_cli_wrong_confirmation_records_nothing(session):
    for typed in ("\n", "yes\n", "0" * 12 + "\n"):
        code, _, err = _run_cli(["appr", "c1", "--approver", "alice"], typed=typed)
        assert code == 4 and "No approval recorded" in err
    assert not records.approvals_file().exists()


def test_cli_missing_claim_or_session(session):
    assert _run_cli(["appr", "nope", "--approver", "a"])[0] == 1
    assert _run_cli(["no-such-session", "c1", "--approver", "a"])[0] == 1


def test_cli_default_approver_is_the_os_user(session, monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "osuser")
    rev = _session_rev()
    assert _run_cli(["appr", "c1"], typed=rev.split(":")[1][:12] + "\n")[0] == 0
    assert find_approval("appr", "c1", rev)["approver"]["id"] == "osuser"


def test_cli_is_idempotent_for_an_already_approved_revision(session):
    approve("appr", "c1")
    before = records.approvals_file().read_bytes()
    code, out, _ = _run_cli(["appr", "c1", "--approver", "a"])
    assert code == 0 and "Already approved" in out
    assert records.approvals_file().read_bytes() == before


def test_cli_does_not_modify_the_session(session):
    path = store._SESSIONS_DIR
    snapshot = {p: p.read_bytes() for p in path.rglob("*") if p.is_file()}
    rev = _session_rev()
    _run_cli(["appr", "c1", "--approver", "a"], typed=rev.split(":")[1][:12] + "\n")
    assert {p: p.read_bytes() for p in path.rglob("*") if p.is_file()} == snapshot


def test_entry_point_is_declared():
    text = (REPO / "pyproject.toml").read_text()
    assert 'aihydro-approve = "ai_hydro.approval.cli:main"' in text


# --------------------------------------------------------------------------
# Registry: AIHYDRO_HOME at call time and cross-process locking
# --------------------------------------------------------------------------

def test_registry_path_honours_aihydro_home_at_call_time(tmp_path, monkeypatch):
    a, b = tmp_path / "ra", tmp_path / "rb"
    monkeypatch.setenv("AIHYDRO_HOME", str(a))
    registry.append({"registry_id": "x1", "claim_id": "c", "session_id": "s", "status": "promoted"})
    monkeypatch.setenv("AIHYDRO_HOME", str(b))
    assert registry.all_entries() == []
    assert registry.claims_file() == b / "registry" / "claims.jsonl"
    monkeypatch.setenv("AIHYDRO_HOME", str(a))
    assert [e["registry_id"] for e in registry.all_entries()] == ["x1"]


def test_registry_rmw_waits_for_the_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "locked"))
    done = threading.Event()

    def writer():
        registry.append({"registry_id": "w1", "claim_id": "c", "session_id": "s", "status": "promoted"})
        done.set()

    with registry._lock():
        t = threading.Thread(target=writer)
        t.start()
        time.sleep(0.3)
        assert not done.is_set(), "append must block while another holder owns the lock"
    t.join(timeout=10)
    assert done.is_set() and len(registry.all_entries()) == 1


def test_concurrent_processes_do_not_lose_registry_writes(tmp_path):
    home = tmp_path / "mp"
    code = (
        "import sys; from ai_hydro.registry import store\n"
        "w = sys.argv[1]\n"
        "for i in range(12):\n"
        "    store.append({'registry_id': f'{w}-{i}', 'claim_id': 'c', 'session_id': 's', 'status': 'promoted'})\n"
    )
    env = {**os.environ, "AIHYDRO_HOME": str(home), "HOME": str(tmp_path),
           "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO), os.environ.get("PYTHONPATH", "")]))}
    procs = [subprocess.Popen([sys.executable, "-c", code, f"w{n}"], env=env, cwd=str(REPO))
             for n in range(4)]
    assert [p.wait(timeout=180) for p in procs] == [0, 0, 0, 0]
    ids = {e["registry_id"] for e in
           (json.loads(l) for l in (home / "registry" / "claims.jsonl").read_text().splitlines())}
    assert ids == {f"w{n}-{i}" for n in range(4) for i in range(12)}
