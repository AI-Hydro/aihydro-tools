"""Snapshot P3: minimal rows, claim revision/drift/approval fields, per-claim errors."""
import json

import pytest

from ai_hydro.registry import store as registry  # noqa: F401  (isolated registry via conftest)
from ai_hydro.session import claim_revisions as cr
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from ai_hydro.session.surfaces import read_research_snapshot
from approval_helpers import approve
from test_claim_revisions import _add, _supported
from test_approval import _run_record
from ai_hydro.mcp.tools_ledger import promote_claim_to_registry


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)


@pytest.fixture
def session():
    s = HydroSession("rev")
    s.set("_run_log", {"r1": _run_record("rev")})
    s.save()
    return s


def _claim(cid="c1"):
    return read_research_snapshot("rev")["claims"][cid]


def _legacy_session(tmp_path, run_log):
    path = tmp_path / "min.json"
    path.write_text(json.dumps({"session_id": "min", "_run_log": run_log}))
    return read_research_snapshot(str(path))


def test_minimal_rows_have_empty_key_outputs_and_stay_flagged(tmp_path):
    snap = _legacy_session(tmp_path, {
        "r1": {"run_id": "r1", "tool_name": "evaluate", "key_outputs": {"nse": 0.5}},
        "m1": {"run_id": "m1", "session_id": "min", "tool_name": "x", "timestamp": "2026-01-01",
               "minimal": True, "error": True, "error_summary": "boom",
               "record": {"schema": "aihydro.run/2", "record_error": "output_digest_missing"}}})
    runs = {r["run_id"]: r for r in snap["runs"]}
    assert runs["m1"]["minimal"] is True
    assert runs["m1"]["key_outputs"] == {}          # no record/minimal/error leakage
    assert runs["m1"]["record_error"] == "output_digest_missing"
    assert runs["m1"]["record"]["schema"] == "aihydro.run/2"
    assert runs["r1"]["minimal"] is False and runs["r1"]["key_outputs"] == {"nse": 0.5}
    assert snap["record_coverage"]["run_log_rows"] == 2


def test_minimal_row_with_leaky_key_outputs_is_emptied(tmp_path):
    snap = _legacy_session(tmp_path, {"m": {"run_id": "m", "minimal": True, "key_outputs": {"leak": 1}}})
    assert snap["runs"][0]["key_outputs"] == {}


def test_minimal_row_without_key_outputs_does_not_leak_fallback(tmp_path):
    snap = _legacy_session(tmp_path, {"m": {"run_id": "m", "minimal": True, "extra_field": 1}})
    assert snap["runs"][0]["key_outputs"] == {}


def test_claim_revision_fields_and_in_sync(session):
    _add()
    _supported()
    claim = _claim()
    rows = cr.history("rev")["c1"]["rows"]
    assert claim["revision"] == rows[-1]["revision"] == 1
    assert claim["revision_digest"] == rows[-1]["revision_digest"]
    assert claim["history_len"] == 2
    assert claim["revision_drift"]["state"] == "in_sync"
    assert claim["revision_drift"]["evidence_checked"] is False
    assert claim["revision_error"] is None
    assert claim["approval"]["state"] == "none"


def test_out_of_band_edit_reads_as_drifted(session):
    _add()
    raw = json.loads((store._SESSIONS_DIR / "rev.json").read_text())
    raw["claims"]["c1"]["claim"] = "edited outside the tools"
    (store._SESSIONS_DIR / "rev.json").write_text(json.dumps(raw))
    drift = _claim()["revision_drift"]
    assert drift["state"] == "drifted" and drift["drift"] is True
    assert "text" in drift["changed_fields"]


def test_claim_without_history_has_null_revision(session):
    s = HydroSession.load("rev")
    s.claims["old"] = {"id": "old", "claim": "legacy"}
    s.save()
    claim = _claim("old")
    assert claim["revision"] is None and claim["history_len"] == 0
    assert claim["revision_drift"] is None and claim["revision_drift_reason"]
    assert claim["approval"] == {"state": "none"}


def test_approval_states_approved_then_consumed(session):
    _add()
    _supported()
    approve("rev", "c1")
    approval = _claim()["approval"]
    assert approval["state"] == "approved"
    assert approval["for_revision_digest"] == _claim()["revision_digest"]
    assert {"channel", "trust_root", "principal", "policy", "record_digest"} <= set(approval)
    assert promote_claim_to_registry("rev", "c1", researcher_approved=True)["status"] == "promoted"
    assert _claim()["approval"]["state"] == "consumed"


def test_approval_without_trust_root_is_unverifiable_never_approved(session, monkeypatch):
    _add()
    _supported()
    approve("rev", "c1")                       # written under the legacy opt-out
    monkeypatch.setenv("AIHYDRO_REQUIRE_SIGNED", "1")
    approval = _claim()["approval"]
    assert approval["state"] == "unverifiable"
    assert approval["channel"] is None and approval["trust_root"] is None
    assert approval["reason"]


def test_approval_for_older_revision_does_not_apply(session):
    _add()
    approve("rev", "c1")
    _supported()                                # new revision, approval is stale
    assert _claim()["approval"]["state"] == "none"


def test_one_corrupt_chain_does_not_break_snapshot(session):
    _add()
    _add(claim_id="c2", statement="Another synthetic claim")
    conn = cr._connect("rev", create=False)
    conn.execute("DROP TRIGGER claim_revisions_no_update")
    conn.execute("UPDATE claim_revisions SET row_json = 'garbage' WHERE claim_id = 'c1'")
    conn.close()
    snap = read_research_snapshot("rev")
    bad, good = snap["claims"]["c1"], snap["claims"]["c2"]
    assert bad["revision_error"] and bad["revision"] is None
    assert bad["approval"]["state"] == "unverifiable"
    assert good["revision_error"] is None and good["revision"] == 0
    assert snap["runs"] and "record_coverage" in snap


def test_capsule_snapshot_never_reads_live_revision_store(session, tmp_path):
    _add()
    directory = tmp_path / "cap"
    directory.mkdir()
    (directory / "session.json").write_text(json.dumps(
        {"session_id": "rev", "claims": {"c1": {"id": "c1", "claim": "x"}}}))
    claim = read_research_snapshot(str(directory))["claims"]["c1"]
    assert claim["revision"] is None
    assert claim["approval"]["state"] == "unverifiable"


def test_coverage_counters_and_schema_unchanged(session):
    snap = read_research_snapshot("rev")
    assert snap["schema_version"] == 1
    assert snap["record_coverage"]["run_log_rows"] == 1
