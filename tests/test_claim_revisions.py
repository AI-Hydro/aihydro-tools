"""Claim revision store and ledger integration (2040 slice 2, P1).

Each authority-bearing claim change writes exactly one sealed, insert-only
``aihydro.claim_revision_record/1`` row; promotion binds to the latest stored
revision. All IO is under per-test temp dirs (``tests/conftest.py`` isolates
AIHYDRO_HOME; the sessions dir is monkeypatched below).
"""
from __future__ import annotations

import sqlite3

import pytest

from aihydro_core.records import ClaimRevision, verify_chain

from ai_hydro.approval.records import APPROVAL_REQUIRED, session_claim_revision
from ai_hydro.mcp.tools_ledger import (
    add_claim,
    check_registry_staleness,
    promote_claim_to_registry,
    update_claim_status,
)
from ai_hydro.registry import store as registry
from ai_hydro.session import claim_revisions as cr
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from approval_helpers import approve, basin_ref, basin_ref_full, retained
from test_approval import _claim, _run_record


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


def _add(**over):
    args = dict(session_id="rev", claim_id="c1", statement="Synthetic NSE is 0.8",
                claim_type="empirical_result", status="proposed", confidence="low",
                confidence_rationale="Synthetic regression fixture only.",
                basins=["synthetic"], period="2000-2001", metric="nse",
                basin_refs=[basin_ref_full("synthetic")],
                limitations=["Synthetic regression case, no real research conclusion."],
                evidence_spans=[{"source_type": "run", "source_id": "r1", "metric_ref": "nse"}])
    args.update(over)
    return add_claim(**args)


def _supported():
    return update_claim_status("rev", "c1", "supported", "medium",
                               "Synthetic regression fixture only.", uncertainty_verified=True)


def _rows(cid="c1"):
    entry = cr.history("rev")[cid]
    assert entry["ok"], entry
    return entry["rows"]


def test_add_claim_writes_one_sealed_revision_zero(session):
    assert _add()["status"] == "recorded"
    (row,) = _rows()
    rev = ClaimRevision.from_dict(row)
    assert rev.verify() and rev.revision == 0 and rev.supersedes is None
    assert rev.cause == {"tool": "add_claim", "reason": "created"}
    assert rev.actor["kind"] == "package"
    *_, digest_now = session_claim_revision(HydroSession.load("rev"), "c1")
    assert rev.revision_digest == digest_now


def test_each_change_is_one_revision_and_chains(session):
    _add()
    _supported()
    _supported()                         # no authority field moved: no new row
    rows = _rows()
    assert [r["revision"] for r in rows] == [0, 1]
    assert rows[1]["supersedes"] == rows[0]["revision_digest"]
    assert rows[1]["cause"]["reason"] == "status_update"
    assert rows[1]["content"]["status"] == "supported"
    assert verify_chain([ClaimRevision.from_dict(r) for r in rows])


def test_legacy_claim_gets_unrecorded_baseline_on_first_touch(session):
    s = HydroSession.load("rev")
    s.claims["c1"] = _claim(status="proposed", uncertainty_verified=False)
    s.save()
    assert cr.latest("rev", "c1") is None            # nothing back-filled
    _supported()
    rows = _rows()
    assert [r["cause"]["reason"] for r in rows] == ["legacy_unrecorded", "status_update"]
    assert rows[0]["content"]["status"] == "proposed"


def test_sealed_rows_cannot_be_overwritten_or_deleted(session):
    _add()
    (row,) = _rows()
    dup = ClaimRevision.from_dict({**row, "recorded_at": "2030-01-01T00:00:00Z"}).seal()
    conn = cr._connect("rev", create=False)
    with pytest.raises(cr.ClaimRevisionConflict):
        cr._insert_sealed(conn, dup)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE claim_revisions SET row_json = '{}'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM claim_revisions")
    conn.close()
    assert _rows() == [row]


def test_tampered_row_fails_closed(session):
    _add()
    conn = cr._connect("rev", create=False)
    conn.execute("DROP TRIGGER claim_revisions_no_update")
    conn.execute("UPDATE claim_revisions SET row_json = replace(row_json, 'proposed', 'supported')")
    conn.close()
    with pytest.raises(cr.ClaimRevisionIntegrityError):
        cr.latest("rev", "c1")


def test_promotion_binds_latest_revision_and_stamps_registry(session):
    _add()
    _supported()
    approve("rev", "c1")
    res = promote_claim_to_registry("rev", "c1", researcher_approved=True)
    assert res["status"] == "promoted", res
    rows = _rows()
    assert [r["cause"]["reason"] for r in rows] == ["created", "status_update", "promotion"]
    approved = rows[1]
    (entry,) = registry.all_entries()
    assert entry["claim_revision_digest"] == approved["revision_digest"]
    assert entry["claim_revision"] == approved["revision"] == 1
    assert rows[2]["revision_digest"] == approved["revision_digest"]
    assert rows[2]["cause"]["registry_id"] == res["registry_id"]


def test_stale_approval_digest_is_refused(session):
    _add()
    _supported()
    approve("rev", "c1")
    update_claim_status("rev", "c1", "weakly_supported", "low", "Downgraded after review of data.",
                        uncertainty_verified=True)
    res = promote_claim_to_registry("rev", "c1", researcher_approved=True)
    assert res["code"] == APPROVAL_REQUIRED
    assert registry.all_entries() == []


def test_evidence_drift_writes_revision_then_refuses(session):
    _add()
    _supported()
    approve("rev", "c1")
    s = HydroSession.load("rev")
    rec = _run_record("rev")
    rec["evidence"]["data"]["nse"] = 0.3
    rec["evidence"]["uncertainty"]["nse"].update(value=0.3, ci_low=0.2, ci_high=0.4)
    s.set("_run_log", {"r1": rec})
    s.save()
    res = promote_claim_to_registry("rev", "c1", researcher_approved=True)
    assert res["code"] == APPROVAL_REQUIRED and "evidence_drift" in res["message"]
    rows = _rows()
    assert rows[-1]["cause"]["reason"] == "evidence_drift" and rows[-1]["revision"] == 2
    assert registry.all_entries() == []
    approve("rev", "c1")
    assert promote_claim_to_registry("rev", "c1", researcher_approved=True)["status"] == "promoted"


def test_legacy_claim_promotes_with_unrecorded_baseline(session):
    s = HydroSession.load("rev")
    s.claims["c1"] = _claim()
    s.save()
    approve("rev", "c1")
    assert promote_claim_to_registry("rev", "c1", researcher_approved=True)["status"] == "promoted"
    rows = _rows()
    assert [r["cause"]["reason"] for r in rows] == ["legacy_unrecorded", "promotion"]
    assert registry.all_entries()[0]["claim_revision"] == 0


def test_staleness_check_writes_a_staleness_revision(session):
    _add()
    _supported()
    approve("rev", "c1")
    assert promote_claim_to_registry("rev", "c1", researcher_approved=True)["status"] == "promoted"
    s = HydroSession.load("rev")
    rec = _run_record("rev")
    rec["evidence"]["data"]["nse"] = 0.1
    s.set("_run_log", {"r1": rec})
    s.save()
    out = check_registry_staleness("rev")
    assert out["n_stale"] == 1, out
    last = _rows()[-1]
    assert last["cause"]["reason"] == "staleness" and last["content"]["status"] == "stale"
    assert last["cause"]["registry_id"]


def test_concurrent_writers_get_consecutive_revisions(session):
    import threading
    _add()
    base = session_claim_revision(HydroSession.load("rev"), "c1")[2]
    errors = []

    def work(i):
        try:
            cr.record_change("rev", "c1", {**base, "text": f"t{i}"}, tool="t", reason="status_update")
        except Exception as exc:                    # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert [r["revision"] for r in _rows()] == list(range(9))


def _promoted():
    _add(prereg_id="prereg.x.1")
    _supported()
    approve("rev", "c1")
    assert promote_claim_to_registry("rev", "c1", researcher_approved=True)["status"] == "promoted"


def _raw():
    conn = cr._connect("rev", create=False)
    conn.execute("DROP TRIGGER claim_revisions_no_update")
    conn.execute("DROP TRIGGER claim_revisions_no_delete")
    return conn


def test_prereg_id_is_in_the_revision_content(session):
    _add(prereg_id="prereg.x.1")
    assert _rows()[0]["content"]["prereg_id"] == "prereg.x.1"


def test_intact_chain_has_no_registry_mismatch(session):
    _promoted()
    assert check_registry_staleness("rev")["revision_chain_mismatches"] == []


def test_truncated_chain_detected_by_registry_anchor(session):
    _promoted()                                    # revisions 0..2; row anchored at 1
    conn = _raw()
    conn.execute("DELETE FROM claim_revisions WHERE revision >= 1")
    conn.close()
    (m,) = check_registry_staleness("rev")["revision_chain_mismatches"]
    assert m["reason"] == "missing_revision" and m["flag"] == "revision_chain_mismatch"


def test_resealed_chain_detected_by_registry_anchor(session):
    from aihydro_core.records import digest
    _promoted()
    rows = _rows()
    conn = _raw()
    conn.execute("DELETE FROM claim_revisions WHERE revision >= 1")
    prev = ClaimRevision.from_dict(rows[0])
    for old in rows[1:]:                           # rewrite revisions 1.. with other content, resealed
        content = {**old["content"], "confidence_rationale": "rewritten after the fact"}
        new = ClaimRevision(session_id="rev", claim_id="c1", revision=old["revision"],
                            supersedes=prev.revision_digest, revision_digest=digest(content),
                            content=content, cause=old["cause"], actor=old["actor"]).seal()
        cr._insert_sealed(conn, new)
        prev = new
    conn.close()
    assert cr.latest("rev", "c1")["revision"] == 2   # the chain itself still verifies
    (m,) = check_registry_staleness("rev")["revision_chain_mismatches"]
    assert m["reason"] == "different_digest"


def test_history_isolates_a_corrupt_claim(session):
    _add()
    _add(claim_id="c2")
    conn = _raw()
    conn.execute("UPDATE claim_revisions SET row_json = replace(row_json, 'proposed', 'supported')"
                 " WHERE claim_id = 'c1'")
    conn.close()
    h = cr.history("rev")
    assert h["c1"]["ok"] is False and "error" in h["c1"]
    assert h["c2"]["ok"] is True and len(h["c2"]["rows"]) == 1
    with pytest.raises(cr.ClaimRevisionIntegrityError):
        cr.latest("rev", "c1")


def test_revision_drift_helper(session):
    _add()
    fields = session_claim_revision(HydroSession.load("rev"), "c1")[2]
    assert cr.revision_drift(fields, None)["state"] == "no_history"
    assert cr.revision_drift(fields, cr.latest("rev", "c1"))["state"] == "in_sync"
    out = cr.revision_drift({**fields, "status": "supported"}, cr.latest("rev", "c1"))
    assert out["drift"] and out["changed_fields"] == ["status"]


def test_non_finite_content_gets_a_helpful_error(session):
    conn = cr._connect("rev", create=True)
    bad = ClaimRevision(session_id="rev", claim_id="cx", revision=0, revision_digest="sha256:" + "0" * 64,
                        content={"x": float("nan")}, cause={"tool": "t", "reason": "created"},
                        actor={"kind": "package", "id": "a"}).seal()
    with pytest.raises(cr.ClaimRevisionError, match="NaN"):
        cr._insert_sealed(conn, bad)
    conn.close()
