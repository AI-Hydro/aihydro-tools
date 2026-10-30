"""Concurrent writers on one study (T1-R).

Sync tools now run in worker threads, so two calls on the same study overlap: a long fetch loaded
the session minutes ago, a note was written since.  ``HydroSession.save`` must keep both writers'
changes (three-way merge against what each loaded) and must never wait unboundedly: a held write
guard yields a structured, retryable ``SESSION_BUSY``.
"""
from __future__ import annotations

import threading
import time

import pytest

import ai_hydro.session.store as store
from ai_hydro.session.store import HydroSession, SessionBusyError


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    monkeypatch.setattr(store, "SESSIONS_DIR", d)
    monkeypatch.setattr(store, "_SESSIONS_DIR", d)
    return d


def _seed(sid="t1-concurrent"):
    s = HydroSession(sid)
    s.site_name = "seed"
    s.add_note("seed note")
    s.set("watershed", {"data": {"area_km2": 10.0}, "meta": {}})
    s.save()
    return sid


def test_long_tool_does_not_overwrite_a_note_written_meanwhile(sessions):
    sid = _seed()
    long_tool = HydroSession.load(sid)                     # loaded before the fetch starts
    note_tool = HydroSession.load(sid)
    note_tool.add_note("written while the fetch ran")
    note_tool.save()

    long_tool.set("forcing", {"data": {"n_days": 12775}, "meta": {}})
    long_tool.save()                                       # minutes later

    final = HydroSession.load(sid)
    assert "written while the fetch ran" in final.notes
    assert final.get("forcing")["data"]["n_days"] == 12775
    assert final.get("watershed")["data"]["area_km2"] == 10.0


def test_two_writers_different_slots_both_survive_either_order(sessions):
    sid = _seed()
    a, b = HydroSession.load(sid), HydroSession.load(sid)
    a.set("signatures", {"data": {"q": 1}, "meta": {}})
    b.set("forcing", {"data": {"p": 2}, "meta": {}})
    b.save()
    a.save()
    final = HydroSession.load(sid)
    assert final.get("signatures")["data"]["q"] == 1
    assert final.get("forcing")["data"]["p"] == 2


def test_same_leaf_changed_by_both_keeps_the_later_save(sessions):
    sid = _seed()
    a, b = HydroSession.load(sid), HydroSession.load(sid)
    a.set("watershed", {"data": {"area_km2": 11.0}, "meta": {}})
    b.set("watershed", {"data": {"area_km2": 12.0}, "meta": {}})
    a.save()
    b.save()
    assert HydroSession.load(sid).get("watershed")["data"]["area_km2"] == 12.0


def test_second_save_after_a_merge_neither_reverts_nor_loses(sessions):
    sid = _seed()
    a, b = HydroSession.load(sid), HydroSession.load(sid)
    b.add_note("from b")
    b.save()
    a.set("forcing", {"data": {"p": 1}, "meta": {}})
    a.save()                                               # merges b's note in
    a.set("signatures", {"data": {"q": 3}, "meta": {}})
    a.save()                                               # a does not hold "from b": must not drop it
    final = HydroSession.load(sid)
    assert "from b" in final.notes
    assert final.get("forcing")["data"]["p"] == 1
    assert final.get("signatures")["data"]["q"] == 3


def test_deleting_a_slot_is_honoured(sessions):
    sid = _seed()
    a = HydroSession.load(sid)
    a.set("watershed", None)
    a.save()
    assert HydroSession.load(sid).get("watershed") is None


def test_fresh_object_still_replaces_as_before(sessions):
    sid = _seed()
    fresh = HydroSession(sid)
    fresh.site_name = "replaced"
    fresh.save()
    final = HydroSession.load(sid)
    assert final.site_name == "replaced"
    assert final.get("watershed") is None


def test_threads_hammering_one_study_lose_nothing(sessions):
    sid = _seed()
    errors = []

    def worker(i):
        try:
            s = HydroSession.load(sid)
            s.add_note(f"note-{i}")
            s.set(f"slot{i}", {"data": {"i": i}, "meta": {}})
            time.sleep(0.01)
            s.save()
        except Exception as exc:                           # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    final = HydroSession.load(sid)
    for i in range(8):
        assert f"note-{i}" in final.notes
        assert final.get(f"slot{i}")["data"]["i"] == i


def test_held_write_guard_gives_session_busy_not_a_hang(sessions, monkeypatch):
    sid = _seed()
    monkeypatch.setenv("AIHYDRO_SESSION_WRITE_WAIT_S", "0.3")
    target = HydroSession._path(sid)
    s = HydroSession.load(sid)
    s.add_note("blocked")
    entered, release = threading.Event(), threading.Event()

    def holder():
        with store._session_write_guard(target, sid):
            entered.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert entered.wait(2)
    try:
        t0 = time.monotonic()
        with pytest.raises(SessionBusyError) as err:
            s.save()
        assert time.monotonic() - t0 < 2.0
    finally:
        release.set()
        t.join()
    env = err.value.to_dict()
    assert env["error"] is True and env["code"] == "SESSION_BUSY"
    assert env["retryable"] is True and env["session_id"] == sid
    assert "retry" in env["recovery"].lower()
    s.save()                                               # the guard is free again: the retry works
    assert "blocked" in HydroSession.load(sid).notes


def test_session_busy_is_a_structured_tool_result():
    from ai_hydro.mcp.helpers import _tool_error_to_dict

    env = _tool_error_to_dict(SessionBusyError("s1", "fetch_forcing_data", 10.0, ["fetch_forcing_data"]))
    assert env["code"] == "SESSION_BUSY"
    assert env["held_by"] == "fetch_forcing_data"
    assert env["running_tools"] == ["fetch_forcing_data"]
