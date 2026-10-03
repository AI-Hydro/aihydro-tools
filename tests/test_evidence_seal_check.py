"""A run row whose ``record`` is present but does not verify is refused as
evidence (EVIDENCE_SEAL_INVALID), at the single resolution point."""
from __future__ import annotations

import json

import pytest

from ai_hydro.approval.records import evidence_fingerprints, session_claim_revision
from ai_hydro.claims.promotion_policy import promotion_violations
from ai_hydro.registry import store as registry
from ai_hydro.registry.evidence import EvidenceError, resolve_source
from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from test_claim_revisions import _add, _supported, isolated, session  # noqa: F401

SPAN = {"source_type": "run", "source_id": "r1", "metric_ref": "nse"}


def _raw_write(row, sid="rev", rid="r1"):
    """Bypass the store's own seal checks, as a same-user SQLite edit would."""
    conn = store._run_log_connect(sid)
    try:
        conn.execute("INSERT OR REPLACE INTO runs (run_id, timestamp, entry_json) VALUES (?, ?, ?)",
                     (rid, str(row.get("timestamp", "")), json.dumps(row)))
        conn.commit()
    finally:
        conn.close()


def _sealed(session_obj_row, **kw):
    body = {k: v for k, v in session_obj_row.items() if k != "record"}
    return rr.build_run_record(run_id="r1", tool="fixture", session_id="rev", entry=body, **kw).to_dict()


def _row():
    return dict(HydroSession.load("rev").get("_run_log")["r1"])


def _code(s):
    with pytest.raises(EvidenceError) as exc:
        resolve_source(s, SPAN)
    return exc.value.code


def test_valid_seal_and_unsealed_legacy_row_still_resolve(session):
    s = HydroSession.load("rev")
    resolve_source(s, SPAN)                                   # unsealed legacy row
    row = _row()
    _raw_write({**row, "record": _sealed(row)})
    resolve_source(HydroSession.load("rev"), SPAN)            # valid seal


@pytest.mark.parametrize("how", ["forged_digest", "swapped_seal", "wrong_run", "not_object"])
def test_bad_seal_is_refused(session, how):
    row = _row()
    rec = _sealed(row)
    if how == "forged_digest":
        rec["tool"] = "something-else"                         # edited after sealing
    elif how == "swapped_seal":
        other = {**row, "key_outputs": {"nse": 0.1}}
        rec = _sealed(other)                                   # valid seal of a different body
    elif how == "wrong_run":
        rec = rr.build_run_record(run_id="zz", tool="t", session_id="rev").to_dict()
    else:
        rec = "seal"
    _raw_write({**row, "record": rec})
    assert _code(HydroSession.load("rev")) == "EVIDENCE_SEAL_INVALID"


def test_promotion_binding_and_staleness_all_refuse(session):
    _add()
    _supported()
    row = _row()
    _raw_write({**row, "record": {**_sealed(row), "tool": "forged"}})
    s = HydroSession.load("rev")
    codes = [v.code for v in promotion_violations(s, s.claims["c1"], claim_id="c1") if v.blocking]
    assert "EVIDENCE_SEAL_INVALID" in codes
    assert evidence_fingerprints(s, [SPAN])["r1"] == "unresolved:EVIDENCE_SEAL_INVALID"
    assert session_claim_revision(s, "c1")[1]["r1"] == "unresolved:EVIDENCE_SEAL_INVALID"
    assert registry.check_evidence_staleness(s, {"r1": "sha256-v3:" + "0" * 64}, [SPAN]) == ["r1"]


def test_snapshot_surface_flags_the_forged_seal(session):
    _add()
    _supported()
    row = _row()
    _raw_write({**row, "record": {**_sealed(row), "tool": "forged"}})
    from ai_hydro.session import surfaces
    records = {"r1": _row()}
    versions, reason = surfaces._live_evidence("rev", HydroSession.load("rev").claims["c1"], records)
    assert versions["r1"] == "unresolved:EVIDENCE_SEAL_INVALID"


def test_redacted_flag_does_not_excuse_a_tampered_body(session):
    row = _row()
    rec = _sealed(row)                                         # valid seal of the original body
    _raw_write({**row, "key_outputs": {"nse": 0.99}, "redacted_for_privacy": True, "record": rec})
    assert _code(HydroSession.load("rev")) == "EVIDENCE_SEAL_INVALID"
