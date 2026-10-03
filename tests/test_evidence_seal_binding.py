"""The seal a run row carried at bind time is part of the sealed claim revision
(``evidence_seals``): removing or swapping it is caught even though v3
fingerprints cover the body only; a row that was unsealed at bind time may be
sealed later without drift (lazy re-seal stays neutral)."""
from __future__ import annotations

import json

import pytest

from ai_hydro.approval.records import claim_revision_fields, find_approval, session_claim_revision
from ai_hydro.claims.promotion_policy import promotion_violations
from ai_hydro.registry import store as registry
from ai_hydro.registry.evidence import EvidenceError, resolve_source
from ai_hydro.session import claim_revisions as cr
from ai_hydro.session import run_records as rr
from ai_hydro.session import store, surfaces
from ai_hydro.session.store import HydroSession
from approval_helpers import approve
from test_claim_revisions import _add, _supported, isolated, session  # noqa: F401

SPAN = {"source_type": "run", "source_id": "r1", "metric_ref": "nse"}


def _raw_write(row, sid="rev", rid="r1"):
    conn = store._run_log_connect(sid)
    try:
        conn.execute("INSERT OR REPLACE INTO runs (run_id, timestamp, entry_json) VALUES (?, ?, ?)",
                     (rid, str(row.get("timestamp", "")), json.dumps(row)))
        conn.commit()
    finally:
        conn.close()


def _row():
    return dict(HydroSession.load("rev").get("_run_log")["r1"])


def _seal(row, tool="fixture"):
    body = {k: v for k, v in row.items() if k != "record"}
    return rr.build_run_record(run_id="r1", tool=tool, session_id="rev", entry=body).to_dict()


def _seal_row_in_place(tool="fixture"):
    row = _row()
    assert store._run_log_record("rev", "r1", {**row, "record": _seal(row, tool)}) == "replaced"


def _bind(sealed: bool):
    if sealed:
        _seal_row_in_place()
    _add()
    _supported()
    approve("rev", "c1")
    s = HydroSession.load("rev")
    _, ev, fields, rev = session_claim_revision(s, "c1")
    return s, ev, fields, rev


def test_sealed_row_binds_its_seal_and_unsealed_binds_none(session):
    _, _, fields, _ = _bind(sealed=True)
    assert fields["evidence_seals"] == {"r1": _row()["record"]["record_digest"]}


def test_field_is_omitted_when_there_is_no_seal(session):
    _, ev, fields, _ = _bind(sealed=False)
    assert "evidence_seals" not in fields
    assert "evidence_seals" not in claim_revision_fields(HydroSession.load("rev").claims["c1"], ev)


def test_x1_deleting_the_seal_is_detected_everywhere(session):
    s, ev, fields, rev = _bind(sealed=True)
    bound = fields["evidence_seals"]
    row = {k: v for k, v in _row().items() if k != "record"}
    _raw_write(row)                                           # X1: seal deleted, body untouched
    s2 = HydroSession.load("rev")
    with pytest.raises(EvidenceError) as exc:
        resolve_source(s2, SPAN, bound["r1"])
    assert exc.value.code == "EVIDENCE_SEAL_INVALID" and "seal_removed_or_replaced" in str(exc.value)
    _, ev2, _, rev2 = session_claim_revision(s2, "c1")
    assert ev2["r1"] == "unresolved:EVIDENCE_SEAL_INVALID" and rev2 != rev
    assert find_approval("rev", "c1", rev2) is None
    codes = [v.code for v in promotion_violations(s2, s2.claims["c1"], claim_id="c1") if v.blocking]
    assert codes == ["EVIDENCE_SEAL_INVALID"] or "EVIDENCE_SEAL_INVALID" in codes
    assert registry.check_evidence_staleness(s2, ev, [SPAN], bound) == ["r1"]
    surface = surfaces._claim_surface("rev", "c1", s2.claims["c1"], cr.history("rev"), False,
                                      {"r1": _row()})
    assert surface["revision_drift"]["drift"] is True
    assert surface["revision_drift"]["reason"] == "seal_removed_or_replaced"


def test_swapping_in_a_different_valid_seal_is_detected(session):
    s, ev, fields, rev = _bind(sealed=True)
    row = {k: v for k, v in _row().items() if k != "record"}
    _raw_write({**row, "record": _seal(row, tool="other-tool")})   # valid seal, different record
    s2 = HydroSession.load("rev")
    assert session_claim_revision(s2, "c1")[1]["r1"] == "unresolved:EVIDENCE_SEAL_INVALID"


def test_intact_bound_seal_is_in_sync(session):
    s, ev, fields, rev = _bind(sealed=True)
    assert session_claim_revision(HydroSession.load("rev"), "c1")[3] == rev
    assert registry.check_evidence_staleness(s, ev, [SPAN], fields["evidence_seals"]) == []


def test_late_seal_on_a_row_unsealed_at_bind_is_neutral(session):
    s, ev, fields, rev = _bind(sealed=False)
    _seal_row_in_place()
    s2 = HydroSession.load("rev")
    _, ev2, fields2, rev2 = session_claim_revision(s2, "c1")
    assert rev2 == rev and "evidence_seals" not in fields2 and ev2 == ev
    assert find_approval("rev", "c1", rev2) is not None
