"""
Lazy re-seal of unsealed run-log rows (skeptic-slice3 R2).
"""
from __future__ import annotations

import sqlite3

import pytest

from ai_hydro.session import run_records as rr
from ai_hydro.session import store

from test_run_record_middleware import SID, env  # noqa: F401
from test_run_log_followups import _hold_write_lock

ENTRY = {"tool_name": "t", "timestamp": "1", "key_outputs": {"a": 1}}


@pytest.fixture(autouse=True)
def _clean_pending():
    rr.reset_pending()
    yield
    rr.reset_pending()


def _call(rid, arguments=None, result=None, id_factory=lambda t, s: "mw.1", writer="post_run"):
    return rr.record_call(
        tool="t", arguments=arguments or {}, result=result or {"data": {"x": 1}}, failure=None,
        capture=rr.CallCapture(rows=[(SID, rid, writer)]), chat_id=None, id_factory=id_factory)


def _row(rid):
    return store._run_log_read_one(SID, rid)


def _lock_fails(env, monkeypatch, rid="w1"):
    """Writer row ``rid`` exists; a held lock makes the middleware miss the budget."""
    monkeypatch.setattr(store, "_RUN_LOG_LOCK_WAIT_S", 0.5)
    store._run_log_record(SID, rid, ENTRY)
    holder = _hold_write_lock(SID)
    try:
        outcome = _call(rid)
    finally:
        holder.close()
    return outcome


def test_held_lock_leaves_row_unsealed_then_next_call_reseals_it(env, monkeypatch):
    outcome = _lock_fails(env, monkeypatch)
    assert outcome.record_error and "record_not_stored" in outcome.record_error
    assert "record" not in _row("w1")
    assert rr.pending_seal_ids(SID) == ["w1"]

    monkeypatch.setattr(store, "_RUN_LOG_LOCK_WAIT_S", 5.0)
    store._run_log_record(SID, "w2", ENTRY)
    assert _call("w2").record_error is None            # a later call, same session

    row = _row("w1")
    check = rr.verify_run_log_entry(row)
    assert check["record_ok"] and check["entry_ok"] is True
    assert row["key_outputs"] == {"a": 1}              # body untouched
    assert rr.pending_seal_ids(SID) == []


def test_resealed_digests_are_those_observed_at_the_call(env, monkeypatch):
    _lock_fails(env, monkeypatch)
    expected = rr.build_run_record(
        run_id="w1", tool="t", session_id=SID, arguments={}, result={"data": {"x": 1}}).to_dict()
    rr.reseal_unsealed_rows(SID)
    rec = _row("w1")["record"]
    assert rec["input_digest"] == expected["input_digest"]
    assert rec["output_digest"] == expected["output_digest"]


def test_reseal_does_not_run_past_the_call_deadline(env, monkeypatch):
    _lock_fails(env, monkeypatch)
    out = rr.reseal_unsealed_rows(SID, deadline=0.0)   # already spent
    assert out["resealed"] == [] and out["pending"] == ["w1"]


def test_changed_body_is_marked_unsealable_not_sealed(env, monkeypatch):
    _lock_fails(env, monkeypatch)
    store._run_log_record(SID, "w1", {**ENTRY, "key_outputs": {"a": 2}})   # writer edits it
    out = rr.reseal_unsealed_rows(SID)
    assert out["unsealable"] == ["w1"] and out["resealed"] == []
    row = _row("w1")
    assert "record" not in row and row["record_status"] == "unsealable"
    assert "changed" in row["record_status_reason"]
    assert row["key_outputs"] == {"a": 2}
    cov = rr.coverage_summary({"w1": row})
    assert cov["unsealable"] == 1 and cov["legacy_unrecorded"] == 1
    assert cov["problems"][0]["problem"] == "unsealable"


def test_unobserved_body_is_marked_unsealable(env, monkeypatch):
    """Read failed: nothing was observed, so no seal can be bound to the row."""
    monkeypatch.setattr(store, "_RUN_LOG_LOCK_WAIT_S", 0.5)
    store._run_log_record(SID, "w1", ENTRY)
    real = store._run_log_read_one

    def boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_run_log_read_one", boom)
    assert "row unreadable" in _call("w1").record_error
    monkeypatch.setattr(store, "_run_log_read_one", real)
    out = rr.reseal_unsealed_rows(SID)
    assert out["unsealable"] == ["w1"]
    row = _row("w1")
    assert row["record_status"] == "unsealable" and "not observed" in row["record_status_reason"]
    assert "record" not in row


def test_sealed_row_is_never_rewritten_or_marked(env, monkeypatch):
    _lock_fails(env, monkeypatch)
    # someone else seals it meanwhile
    sealed = rr.build_run_record(run_id="w1", tool="other", session_id=SID, entry=ENTRY).to_dict()
    assert store._run_log_record(SID, "w1", {**ENTRY, "record": sealed}) == "replaced"
    before = _row("w1")
    rr.reseal_unsealed_rows(SID)
    assert _row("w1") == before
    assert store._run_log_mark_unsealable(SID, "w1", None, "x") == "noop"
    assert _row("w1") == before


def test_export_reseals_before_packaging(env, monkeypatch, tmp_path):
    import ai_hydro.mcp.tools_session as ts

    _lock_fails(env, monkeypatch)
    monkeypatch.setattr(store, "_RUN_LOG_LOCK_WAIT_S", 5.0)
    result = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "cap"))
    assert not result.get("error")
    assert "record" in _row("w1")
    import json
    manifest = json.loads((tmp_path / "cap" / "capsule_manifest.json").read_text())
    assert manifest["run_records"]["unsealable"] == 0


def test_pending_set_is_bounded(env):
    for i in range(rr.PENDING_MAX_PER_SESSION + 5):
        rr._register_pending(SID, f"r{i}", body=None, record=None, row_absent=False, reason="x")
    assert len(rr.pending_seal_ids(SID)) == rr.PENDING_MAX_PER_SESSION


def test_late_writer_row_after_absent_minimal_is_marked_with_clear_reason(env, monkeypatch):
    monkeypatch.setattr(store, "_RUN_LOG_LOCK_WAIT_S", 0.5)
    store._run_log_record(SID, "seed", ENTRY)
    holder = _hold_write_lock(SID)
    try:
        _call("late", id_factory=lambda t, s: "late")      # row absent; the minimal write fails
    finally:
        holder.close()
    assert rr.pending_seal_ids(SID) == ["late"]
    store._run_log_record(SID, "late", ENTRY)               # a writer's row appears afterwards
    assert rr.reseal_unsealed_rows(SID)["unsealable"] == ["late"]
    assert _row("late")["record_status_reason"] == "row appeared after the call; its body was never observed"


def test_mark_with_expected_body_does_not_land_on_a_different_body(env):
    store._run_log_record(SID, "m1", ENTRY)
    other = store._run_log_body_json({**ENTRY, "key_outputs": {"a": 2}})
    assert store._run_log_mark_unsealable(SID, "m1", other, "x") == "changed"
    assert "record_status" not in _row("m1")
