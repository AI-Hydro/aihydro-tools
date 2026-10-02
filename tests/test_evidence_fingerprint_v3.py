"""sha256-v3 evidence fingerprints: body-only for run rows (skeptic-runlog-r2r3 M1).

Sealing a run-log row, or marking it ``record_status: unsealable``, must not
change the fingerprint of claims bound to it, while a real body change still
does. A stored v2 fingerprint stays whole-row and is recomputed in v2.
"""
from __future__ import annotations

import json

import pytest

from ai_hydro.approval.records import find_approval, session_claim_revision
from ai_hydro.capsule import standalone_replay as sr
from ai_hydro.registry import store as registry
from ai_hydro.registry.evidence import (
    FINGERPRINT_V2, FINGERPRINT_V3, evidence_fingerprint, fingerprint, fingerprint_version,
    is_run_row_metadata_key,
)
from ai_hydro.session import claim_revisions as cr
from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from approval_helpers import approve
from test_approval import _run_record
from test_claim_revisions import _add, _supported, isolated, session  # noqa: F401

SPANS = [{"source_type": "run", "source_id": "r1", "metric_ref": "nse"}]


def _seal_row(sid="rev", rid="r1"):
    row = store._run_log_read_one(sid, rid)
    body = {k: v for k, v in row.items() if k != "record"}
    record = rr.build_run_record(run_id=rid, tool="fixture", session_id=sid, entry=body)
    assert store._run_log_record(sid, rid, {**body, "record": record.to_dict()}) == "replaced"


def _mark_row(sid="rev", rid="r1"):
    assert store._run_log_mark_unsealable(sid, rid, None, "test") == "marked"


def _bound_and_approved():
    _add()
    _supported()
    approve("rev", "c1")
    s = HydroSession.load("rev")
    claim, ev, fields, rev = session_claim_revision(s, "c1")
    assert ev["r1"].startswith("sha256-v3:")
    assert find_approval("rev", "c1", rev) is not None
    return ev, rev


@pytest.mark.parametrize("change", [_seal_row, _mark_row])
def test_v3_binding_survives_sealing_or_marking(session, change):
    ev, rev = _bound_and_approved()
    change()
    s = HydroSession.load("rev")
    assert s.get("_run_log")["r1"].get("record") or s.get("_run_log")["r1"].get("record_status")
    _, ev2, _, rev2 = session_claim_revision(s, "c1")
    assert ev2 == ev and rev2 == rev                       # revision digest unchanged
    assert find_approval("rev", "c1", rev2) is not None    # approval binding still valid
    assert registry.check_evidence_staleness(s, ev, SPANS) == []


def test_v3_detects_a_real_body_change(session):
    ev, rev = _bound_and_approved()
    row = store._run_log_read_one("rev", "r1")
    store._run_log_record("rev", "r1", {**row, "key_outputs": {"nse": 0.1}})
    s = HydroSession.load("rev")
    assert registry.check_evidence_staleness(s, ev, SPANS) == ["r1"]
    assert session_claim_revision(s, "c1")[3] != rev


def test_v2_stays_whole_row_and_detects_a_real_change(session):
    row = HydroSession.load("rev").get("_run_log")["r1"]
    v2 = fingerprint(row, FINGERPRINT_V2)
    s = HydroSession.load("rev")
    assert registry.check_evidence_staleness(s, {"r1": v2}, SPANS) == []     # verifies unchanged
    assert evidence_fingerprint(row, "run", like=v2) == v2                   # recomputed in v2
    store._run_log_record("rev", "r1", {**row, "key_outputs": {"nse": 0.1}})
    s = HydroSession.load("rev")
    assert registry.check_evidence_staleness(s, {"r1": v2}, SPANS) == ["r1"]
    # an unknown prefix is never accepted
    assert registry.check_evidence_staleness(s, {"r1": "sha256-v9:abc"}, SPANS) == ["r1"]


def test_v2_bound_claim_keeps_its_revision_digest(session):
    """A claim whose chain stored v2 fingerprints does not mass-drift to v3."""
    _add()
    s = HydroSession.load("rev")
    claim = s.claims["c1"]
    from ai_hydro.approval.records import claim_revision_fields
    from aihydro_core.records import digest
    v2 = {"r1": fingerprint(s.get("_run_log")["r1"], FINGERPRINT_V2)}
    stored = cr.record_change("rev", "c1", claim_revision_fields(claim, v2), tool="t", reason="evidence_drift")
    _, ev, _, rev = session_claim_revision(HydroSession.load("rev"), "c1")
    assert ev == v2 and rev == stored.revision_digest


def test_excluded_key_rule_is_shared_with_standalone_replay():
    row = {"run_id": "r", "k": 1, "record": {"x": 1}, "record_status": "unsealable",
           "record_status_reason": "r", "record_statusX": 1, "recorded": 2}
    assert {k for k in row if is_run_row_metadata_key(k)} == \
           {k for k in row if sr._is_run_row_metadata_key(k)}
    for version in (FINGERPRINT_V2, FINGERPRINT_V3):
        assert sr._run_fingerprint(row, version) == evidence_fingerprint(
            row, "run", like=version + ":x")
    assert fingerprint_version(sr._run_fingerprint(row)) == FINGERPRINT_V3


def test_non_run_sources_are_hashed_whole_in_v3():
    ds = {"record": 1, "record_status": "x", "v": 2}
    assert evidence_fingerprint(ds, "dataset").split(":")[1] == fingerprint(ds).split(":")[1]


# ----------------------------------------------------------- capsule replay
def test_capsule_replay_unchanged_by_reseal_and_reports_unsealable(tmp_path, monkeypatch):
    import ai_hydro.mcp.tools_session as ts
    from test_capsule_approvals import Case, RUN_ID, SID

    c = Case(tmp_path, monkeypatch, {"c1": "v1"})
    code, before = c.replay()
    assert code == 0, before

    _seal_row_sid = store._run_log_read_one(SID, RUN_ID)
    record = rr.build_run_record(run_id=RUN_ID, tool="t", session_id=SID, entry=_seal_row_sid)
    assert store._run_log_record(SID, RUN_ID, {**_seal_row_sid, "record": record.to_dict()}) == "replaced"
    store._run_log_record(SID, "other", {"run_id": "other", "tool_name": "t", "timestamp": "2"})
    assert store._run_log_mark_unsealable(SID, "other", None, "body not observed") == "marked"

    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "capsule2"))
    assert "error" not in res, res
    c.dir = tmp_path / "capsule2"
    code, after = c.replay()
    assert code == 0, after                                  # resealed row: approval still re-derives
    assert "FAIL" not in after
    assert "1 of them marked unsealable" in after and "NOTE  run other: unsealable: body not observed" in after
