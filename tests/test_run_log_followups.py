"""
Run-log lock follow-ups (skeptic-slice3 L1-L4):

L1  one deadline budget bounds connect, transaction retry and record_call.
L2  a writer row lost to an error is reported (``writer_row_lost`` on the minimal
    row, ``_record_error`` on the tool result), not silently replaced.
L3  export_session fails loudly when the run log cannot be read.
L4  "schema has changed" is retried once.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession

from test_run_record_middleware import SID, call, run_log  # noqa: F401  (fixtures below reuse these)
import test_run_record_middleware as mw

env = mw.env
server = mw.server

ENTRY = {"tool_name": "t", "timestamp": "1", "key_outputs": {"a": 1}}


def _hold_write_lock(session_id):
    """A second connection that holds the write lock (a hung writer)."""
    conn = sqlite3.connect(str(store._run_log_db_path(session_id)), timeout=0.1, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    return conn


# ------------------------------------------------------------------ L1
def test_held_lock_bounds_a_single_write(env, monkeypatch):
    monkeypatch.setattr(store, "_RUN_LOG_LOCK_WAIT_S", 1.0)
    store._run_log_record(SID, "seed", ENTRY)            # creates the db in WAL mode
    holder = _hold_write_lock(SID)
    try:
        t0 = time.monotonic()
        assert store._run_log_record(SID, "r1", ENTRY) == "error"
        assert time.monotonic() - t0 < 2.5               # ~90 s before the shared budget
    finally:
        holder.close()


def test_held_lock_bounds_record_call_total(env, monkeypatch):
    monkeypatch.setattr(store, "_RUN_LOG_LOCK_WAIT_S", 1.0)
    store._run_log_record(SID, "seed", ENTRY)
    holder = _hold_write_lock(SID)
    try:
        t0 = time.monotonic()
        outcome = rr.record_call(
            tool="t", arguments={}, result={"data": {}}, failure=None,
            capture=rr.CallCapture(rows=[(SID, "seed", "post_run")]), chat_id=None,
            id_factory=lambda tool, sid: "x.1")
        assert time.monotonic() - t0 < 2.5               # was several minutes
        assert outcome.label == "error" and outcome.record_error
    finally:
        holder.close()


def test_deadline_is_never_exceeded_by_retry_loop():
    calls = []

    def fn():
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    t0 = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        store._retry_when_locked(fn, deadline=time.monotonic() + 0.3)
    assert time.monotonic() - t0 < 0.6 and len(calls) > 1


# ------------------------------------------------------------------ L4
def test_schema_changed_is_retried_once():
    n = {"c": 0}

    def fn():
        n["c"] += 1
        if n["c"] < 2:
            raise sqlite3.OperationalError("database schema has changed")
        return "ok"

    assert store._retry_when_locked(fn) == "ok" and n["c"] == 2

    def always():
        raise sqlite3.OperationalError("database schema has changed")

    with pytest.raises(sqlite3.OperationalError):
        store._retry_when_locked(always)                 # only one retry, then it raises


# ------------------------------------------------------------------ L2
def test_writer_row_loss_is_reported_on_the_minimal_row_and_tool_result(env, server, monkeypatch):
    fail = [True]
    real_connect = store._run_log_connect
    real_record_call = rr.record_call

    def connect(session_id, **kw):
        if fail[0]:
            raise sqlite3.OperationalError("disk I/O error")   # not a lock error: no retry
        return real_connect(session_id, **kw)

    def record_call(**kw):
        fail[0] = False                                         # the middleware's own write succeeds
        return real_record_call(**kw)

    monkeypatch.setattr(store, "_run_log_connect", connect)
    monkeypatch.setattr(rr, "record_call", record_call)

    is_error, body, _ = call(server, "post_run_tool", {"session_id": SID})
    assert not is_error
    assert "writer_row_lost: disk I/O error" in body["_record_error"]
    rows = run_log()
    assert len(rows) == 1
    (row,) = rows.values()
    assert row["record"]["extra"]["entry"] == "minimal"
    assert row["record"]["record_error"].startswith("writer_row_lost: disk I/O error")


def test_note_row_failed_is_a_noop_outside_a_call():
    rr.note_row_failed("s", "r", "put_result", "x")      # must not raise


# ------------------------------------------------------------------ L3
def test_strict_read_all_raises_and_lenient_read_logs(env, caplog):
    store._run_log_record(SID, "r1", ENTRY)
    store._run_log_db_path(SID).write_bytes(b"this is not a sqlite database" * 50)
    with caplog.at_level("WARNING"):
        assert store._run_log_read_all(SID) == {}
    assert "Failed to read run log" in caplog.text
    with pytest.raises(store.RunLogUnreadable):
        store._run_log_read_all(SID, strict=True)


def test_missing_run_log_is_an_empty_log_even_when_strict(env):
    assert store._run_log_read_all("never-written", strict=True) == {}


def test_export_session_fails_loudly_on_an_unreadable_run_log(env, tmp_path):
    import ai_hydro.mcp.tools_session as ts

    store._run_log_record(SID, "r1", ENTRY)
    store._run_log_db_path(SID).write_bytes(b"this is not a sqlite database" * 50)
    capsule = tmp_path / "capsule_out"
    result = ts.export_session(session_id=SID, capsule_path=str(capsule))
    assert result["error"] is True and result["code"] == "RUN_LOG_UNREADABLE"
    assert not (capsule / "run_log.json").exists()
    assert not (capsule / "capsule_manifest.json").exists()
