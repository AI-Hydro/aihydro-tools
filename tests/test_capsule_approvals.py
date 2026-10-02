"""Capsule approvals and external verification (ADR-002b, packet A3).

Throwaway ed25519 keys in a scratch HOME. ``export_session`` carries the
approval record each promoted claim consumed plus the registry stamp;
``replay.py --allowed-signers FILE`` verifies against a key file the verifier
supplies (never the machine's trust root).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from aihydro_core.records import Actor, digest

from ai_hydro.approval import records, trust
from ai_hydro.approval.records import approval_stamp
from ai_hydro.approval.signing import enrol_line, read_pubkey_file
from ai_hydro.approval.writer import write_approval, write_signed_approval
from ai_hydro.capsule import standalone_replay as sr
from ai_hydro.capsule.manifest import MANIFEST_FILE, build_manifest
from ai_hydro.registry import store as registry

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs OpenSSH ssh-keygen")

SID = "cap-approvals"
REV = "sha256:" + "cd" * 32
HUMAN = Actor(kind="human", id="alice")


@pytest.fixture(autouse=True)
def scratch_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AIHYDRO_SYSTEM_TRUST_FILE", str(tmp_path / "etc" / "allowed_signers"))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("AIHYDRO_REQUIRE_SIGNED", raising=False)
    return tmp_path


def make_key(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)],
                   check=True, capture_output=True)
    return path


def signer_file(path: Path, key: Path, principal: str = "alice") -> Path:
    path.write_text(enrol_line(read_pubkey_file(Path(str(key) + ".pub")), principal) + "\n")
    return path


@pytest.fixture
def keys(tmp_path):
    good = make_key(tmp_path / "keys", "good")
    other = make_key(tmp_path / "keys", "other")
    # the machine's own trust root enrols the good key (needed to *write* a signed approval)
    user = trust.user_trust_file()
    user.parent.mkdir(parents=True, exist_ok=True)
    signer_file(user, good)
    return {"good": good, "other": other,
            "supplied_good": signer_file(tmp_path / "supplied_good", good),
            "supplied_other": signer_file(tmp_path / "supplied_other", other)}


RUN_ID = "sigs.20261002.cap.ab12"


def _claim(text="Mean flow is stable", kind="run"):
    return {"id": "x", "claim": text, "claim_type": "empirical_result", "status": "supported",
            "confidence": "medium", "confidence_rationale": "one retained run supports this claim",
            "scope": {"basins": ["01013500"], "period": "1990-2000", "forcing": None, "metric": None,
                      "model_versions": {}},
            "evidence_spans": [{"source_type": kind, "source_id": RUN_ID if kind == "run" else "ds",
                                "metric_ref": "q_mean", "page": None, "passage_hash": None}],
            "limitations": ["one gauge"], "promoted": True}


class Case:
    """A session whose claims are promoted with the given approval kinds, exported to a capsule."""

    def __init__(self, tmp_path, monkeypatch, claims: dict, key: Path | None = None, span_kind="run"):
        import ai_hydro.mcp.tools_session as ts
        import ai_hydro.session.store as store
        from ai_hydro.approval.records import session_claim_revision
        from ai_hydro.session.store import HydroSession

        sessions = tmp_path / "sessions"
        sessions.mkdir(exist_ok=True)
        monkeypatch.setattr(store, "SESSIONS_DIR", sessions)
        monkeypatch.setattr(store, "_SESSIONS_DIR", sessions)
        session = HydroSession(SID)
        session.set("_run_log", {RUN_ID: {"run_id": RUN_ID, "session_id": SID, "tool_name": "t",
                                          "timestamp": "2026-10-02T00:00:00+00:00",
                                          "key_outputs": {"q_mean": 1.25}}})
        session.claims = {}
        self.approvals, self.revs = {}, {}
        for claim_id, kind in claims.items():
            rid = f"{SID}.{claim_id}"
            session.claims[claim_id] = {**_claim(f"Claim {claim_id}", span_kind), "registry_id": rid}
        session.save()
        loaded = HydroSession.load(SID)
        for claim_id, kind in claims.items():
            rid = f"{SID}.{claim_id}"
            rev = session_claim_revision(loaded, claim_id)[3]
            self.revs[claim_id] = rev
            row = {"registry_id": rid, "claim_id": claim_id, "session_id": SID, "status": "promoted",
                   "claim_revision_digest": rev, "claim_revision": 1}
            if kind == "signed":
                rec = write_signed_approval(SID, claim_id, rev, HUMAN, "ok", key=str(key))
            elif kind == "v1":
                rec = write_approval(SID, claim_id, rev, HUMAN, "ok")
            else:
                rec = None
            if rec is not None:
                self.approvals[claim_id] = rec
                row["approval"] = approval_stamp(rec) if kind == "signed" else {
                    "record_digest": rec["record_digest"], "channel": "cli_same_user"}
            registry.append(row)
        res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "capsule"))
        assert "error" not in res, res
        self.dir = Path(res["capsule_dir"])
        self.result = res

    def replay(self, *args):
        p = subprocess.run([sys.executable, "replay.py", *args], cwd=self.dir,
                           capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout

    def reseal_manifest(self):
        (self.dir / MANIFEST_FILE).write_text(json.dumps(build_manifest(self.dir)))


def test_valid_signature_passes(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 0, out
    assert "PASS  approval c1 (" in out
    assert "approvals: 1 verified against supplied signers, 0 failed, 0 unsigned (cli_same_user/opt-out)" in out
    # carried verbatim, hashed in the manifest
    m = json.loads((c.dir / MANIFEST_FILE).read_text())
    paths = {f["path"] for f in m["files"]}
    assert "approvals/index.json" in paths and f"approvals/{c.approvals['c1']['record_digest'][7:]}.json" in paths
    assert m["approvals"]["n_with_approval_record"] == 1 and m["approvals"]["n_no_approval"] == 0
    assert all(f["sha256"] for f in m["approvals"]["files"])
    # equals-sign spelling works too
    assert c.replay(f"--allowed-signers={keys['supplied_good']}")[0] == 0


def test_standalone_seal_matches_core_digest(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    rec = c.approvals["c1"]
    body = {k: v for k, v in rec.items() if k not in ("record_digest", "signature")}
    assert sr.c14n_digest(body) == digest(body) == rec["record_digest"]


def test_wrong_key_fails(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    code, out = c.replay("--allowed-signers", str(keys["supplied_other"]))
    assert code == 1
    assert "FAIL  approval c1 (" in out and "PASS  approval " not in out
    assert "0 verified against supplied signers, 1 failed" in out


def test_wrong_principal_fails(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    mallory = signer_file(tmp_path / "mallory", keys["good"], principal="mallory")
    code, out = c.replay("--allowed-signers", str(mallory))
    assert code == 1 and "FAIL  approval c1 (" in out


def test_tampered_approval_fails_even_with_regenerated_manifest(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    f = c.dir / c.result["approvals"]["files"][1]["path"]
    rec = json.loads(f.read_text())
    rec["statement"] = "approved something else"
    f.write_text(json.dumps(rec))
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "FAIL  approvals/" in out          # the manifest hash check flags it
    c.reseal_manifest()                                    # attacker hides it from the hash check
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1
    assert "FAIL  approval c1 (cap-approvals.c1): sealed body digest mismatch" in out


def test_resealed_record_fails_signature(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    f = c.dir / c.result["approvals"]["files"][1]["path"]
    rec = json.loads(f.read_text())
    rec["statement"] = "forged"
    rec = records.seal_record(rec)                         # new digest, old signature
    f.unlink()
    new_rel = f"approvals/{rec['record_digest'][7:]}.json"
    (c.dir / new_rel).write_text(json.dumps(rec))
    idx = c.dir / "approvals" / "index.json"
    ix = json.loads(idx.read_text())
    ix["claims"][0]["approval_file"] = new_rel
    ix["claims"][0]["record_digest"] = rec["record_digest"]
    ix["claims"][0]["registry_stamp"]["approval"]["record_digest"] = rec["record_digest"]
    idx.write_text(json.dumps(ix))
    c.reseal_manifest()
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "FAIL  approval c1 (cap-approvals.c1): signature not accepted" in out


def test_revision_digest_must_match_registry_stamp(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    idx = c.dir / "approvals" / "index.json"
    ix = json.loads(idx.read_text())
    ix["claims"][0]["registry_stamp"]["claim_revision_digest"] = "sha256:" + "00" * 32
    idx.write_text(json.dumps(ix))
    c.reseal_manifest()
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "claim_revision_digest differs from the registry stamp's" in out


def test_unsigned_v1_is_unsigned_never_pass(tmp_path, monkeypatch, keys):
    monkeypatch.setenv("AIHYDRO_REQUIRE_SIGNED", "0")
    c = Case(tmp_path, monkeypatch, {"c1": "v1"})
    for args in ([], ["--allowed-signers", str(keys["supplied_good"])]):
        code, out = c.replay(*args)
        assert code == 0, out
        assert "UNSIGNED  approval c1 (" in out and "PASS  approval " not in out
        assert "1 unsigned (cli_same_user/opt-out)" in out


def test_missing_flag_is_not_verified(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    code, out = c.replay()
    assert code == 0, out
    assert "not verified (no signer file supplied)" in out
    assert "PASS  approval " not in out and "verified against supplied signers" not in out


def test_missing_signer_file_fails(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    code, out = c.replay("--allowed-signers", str(tmp_path / "nope"))
    assert code == 1 and "signer" in out.lower() and "PASS  approval " not in out


def test_claim_without_approval_is_listed_not_approved(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed", "c2": "none"}, keys["good"])
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 0, out
    assert "NOTE  approval c2 (" in out and "not approved" in out
    assert "1 verified against supplied signers" in out
    ix = json.loads((c.dir / "approvals" / "index.json").read_text())
    assert {e["claim_id"]: e["status"] for e in ix["claims"]} == {"c1": "record_carried", "c2": "no_approval"}
    assert c.result["approvals"]["n_no_approval"] == 1


def test_capsule_without_promoted_claims_is_fine(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {})
    assert not (c.dir / "approvals").exists()
    assert c.result["approvals"]["n_promoted_claims"] == 0
    for args in ([], ["--allowed-signers", str(keys["supplied_good"])]):
        code, out = c.replay(*args)
        assert code == 0, out
        assert "none in this capsule" in out


# --- adversarial-review regressions (skeptic wave 5: S1-S4, S6) -------------------------

def _reindex(c, fn):
    idx = c.dir / "approvals" / "index.json"
    ix = json.loads(idx.read_text())
    fn(ix)
    idx.write_text(json.dumps(ix))
    c.reseal_manifest()


def test_s1_stamp_copied_onto_unapproved_claim_fails(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed", "c2": "none"}, keys["good"])

    def swap(ix):
        e1 = next(e for e in ix["claims"] if e["claim_id"] == "c1")
        e2 = next(e for e in ix["claims"] if e["claim_id"] == "c2")
        e2.update({k: e1[k] for k in ("status", "record_digest", "approval_file", "registry_stamp")})
        e2.pop("reason", None)
    _reindex(c, swap)
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "PASS  approval c2 (" not in out
    assert "approves claim 'c1', not 'c2'" in out
    assert "PASS  approval c1 (" in out


def test_s2_cross_session_replay_fails(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    for name in ("session.json", "approvals/index.json"):
        f = c.dir / name
        raw = json.loads(f.read_text())
        raw["session_id"] = "victim-session"
        f.write_text(json.dumps(raw))
    c.reseal_manifest()
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "PASS  approval c1 (" not in out and "session id differs" in out


def test_s3_claim_text_edit_in_session_json_fails(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    sj = c.dir / "session.json"
    raw = json.loads(sj.read_text())
    raw["claims"]["c1"]["claim"] = "FORGED claim text"
    sj.write_text(json.dumps(raw))
    c.reseal_manifest()
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "PASS  approval c1 (" not in out
    assert "recomputed claim_revision_digest differs" in out


@pytest.mark.parametrize("edit", ["scope", "limitations", "span_metric", "status"])
def test_s3_other_claim_fields_are_bound(tmp_path, monkeypatch, keys, edit):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    sj = c.dir / "session.json"
    raw = json.loads(sj.read_text())
    cl = raw["claims"]["c1"]
    if edit == "scope":
        cl["scope"]["basins"] = ["99999999"]
    elif edit == "limitations":
        cl["limitations"] = []
    elif edit == "span_metric":
        cl["evidence_spans"][0]["metric_ref"] = "other"
    else:
        cl["status"] = "retracted"
    sj.write_text(json.dumps(raw))
    c.reseal_manifest()
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "PASS  approval c1 (" not in out


def test_s3_cited_run_row_edit_fails(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    rl = c.dir / "run_log.json"
    raw = json.loads(rl.read_text())
    raw[RUN_ID]["key_outputs"]["q_mean"] = 99.0
    rl.write_text(json.dumps(raw))
    c.reseal_manifest()
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "PASS  approval c1 (" not in out and "recomputed claim_revision_digest differs" in out


def test_s3_cited_run_row_missing_fails(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    (c.dir / "run_log.json").write_text("{}")
    c.reseal_manifest()
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "not retained in run_log.json" in out and "PASS  approval c1 (" not in out


def test_s3_non_reproducible_evidence_fails_with_reason(tmp_path, monkeypatch, keys):
    """Dataset spans need the raw slot: replay cannot re-derive the digest, so it must not PASS."""
    c = Case.__new__(Case)
    import ai_hydro.approval.records as rec_mod
    monkeypatch.setattr(rec_mod, "evidence_fingerprints", lambda session, spans, like=None: {"ds": "sha256-v2:" + "00" * 32})
    c.__init__(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"], span_kind="dataset")
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "PASS  approval c1 (" not in out
    assert "dataset span" in out and "not reproducible in stdlib" in out


@pytest.mark.parametrize("bad", ["abs", "traverse", "other_name", "symlink"])
def test_s4_approval_file_is_confined(tmp_path, monkeypatch, keys, bad):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    rel = json.loads((c.dir / "approvals" / "index.json").read_text())["claims"][0]["approval_file"]
    outside = tmp_path / "outside.json"
    shutil.copy(c.dir / rel, outside)

    def point(ix):
        e = ix["claims"][0]
        if bad == "abs":
            e["approval_file"] = str(outside)
        elif bad == "traverse":
            e["approval_file"] = "approvals/../../outside.json"
        elif bad == "other_name":
            shutil.copy(c.dir / rel, c.dir / "approvals" / "copy.json")
            e["approval_file"] = "approvals/copy.json"
        else:
            (c.dir / rel).unlink()
            (c.dir / rel).symlink_to(outside)
    _reindex(c, point)
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "PASS  approval c1 (" not in out
    assert "approvals/<64 hex>.json" in out or "FAIL  approvals/" in out


def test_s6_non_human_approver_fails(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    f = c.dir / c.result["approvals"]["files"][1]["path"]
    rec = json.loads(f.read_text())
    rec["approver"]["kind"] = "agent"
    f.write_text(json.dumps(rec))
    c.reseal_manifest()
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "PASS  approval c1 (" not in out
