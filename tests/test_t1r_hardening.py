"""Data-safety hardening for concurrent tools (T1-R review: B1-B4, N1-N5)."""
from __future__ import annotations

import asyncio
import errno
import json
import threading
import time

import mcp.types as mcp_types
import pytest

import ai_hydro.session.store as store
from ai_hydro.session.store import HydroSession, SessionBusyError, SessionConflictError


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    monkeypatch.setattr(store, "SESSIONS_DIR", d)
    monkeypatch.setattr(store, "_SESSIONS_DIR", d)
    from ai_hydro.session import chat_binding
    from ai_hydro.session.chat_binding import ChatBindingStore

    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    return d


def _poly(x0):
    return {"type": "Polygon", "coordinates": [[[x0, 0], [x0 + 1, 0], [x0 + 1, 1], [x0, 1], [x0, 0]]]}


def _ws(x0, gauge):
    return {"data": {"gauge_id": gauge, "geojson": _poly(x0), "area_km2": 10.0}, "meta": {}}


# ---- B1: locks are atomic and never overwritten ---------------------------------------------------

def test_two_concurrent_plan_locks_exactly_one_wins(sessions):
    from ai_hydro.mcp.tools_ledger import register_research_plan

    HydroSession("p1").save()
    barrier = threading.Barrier(2)
    results = {}

    def go(tag):
        barrier.wait()
        results[tag] = register_research_plan("p1", f"hypothesis {tag}", ["extract_hydrological_signatures"])

    ts = [threading.Thread(target=go, args=(t,)) for t in "AB"]
    [t.start() for t in ts]
    [t.join() for t in ts]
    won = [t for t, r in results.items() if not r.get("error")]
    lost = [t for t, r in results.items() if r.get("error")]
    assert len(won) == 1 and len(lost) == 1, results
    assert results[lost[0]]["error"] == "plan_already_locked"
    plan = HydroSession.load("p1").get("_research_plan")
    assert plan["hypothesis"] == f"hypothesis {won[0]}"


def test_a_locked_record_on_disk_is_never_replaced_by_a_different_one(sessions):
    HydroSession("p2").save()
    a, b = HydroSession.load("p2"), HydroSession.load("p2")
    a.set("_research_plan", {"hypothesis": "A", "locked": True, "content_hash": "ha"})
    b.set("_research_plan", {"hypothesis": "B", "locked": True, "content_hash": "hb"})
    a.save()
    with pytest.raises(SessionConflictError) as err:
        b.save()
    env = err.value.to_dict()
    assert env["code"] == "SESSION_CONFLICT" and env["retryable"] is False
    assert HydroSession.load("p2").get("_research_plan")["hypothesis"] == "A"


def test_concurrent_claim_revisions_do_not_mix_fields(sessions):
    s = HydroSession("c1")
    s.claims["k"] = {"status": "draft", "digest": "d0", "evidence": ["a"], "v": 0}
    s.save()
    a, b = HydroSession.load("c1"), HydroSession.load("c1")
    a.claims["k"].update(status="supported", digest="dA")
    b.claims["k"].update(status="revised", digest="dB", v=1)
    a.save()
    b.save()
    assert HydroSession.load("c1").claims["k"] == {"status": "revised", "digest": "dB", "evidence": ["a"], "v": 1}


# ---- N3: a removed list item stays removed ---------------------------------------------------------

def test_a_removed_note_is_not_revived_by_an_older_save(sessions):
    s = HydroSession("n1")
    s.add_note("one")
    s.add_note("two")
    s.save()
    a, b = HydroSession.load("n1"), HydroSession.load("n1")
    b.add_note("three")
    b.save()
    a.notes.remove("one")
    a.save()                                   # saved AFTER b: must keep b's note and drop "one"
    assert HydroSession.load("n1").notes == ["two", "three"]


# ---- N2: a basin change survives a merge --------------------------------------------------------------

def _seed_basin(sid="bs"):
    s = HydroSession(sid)
    s.site_id = "01111111"
    s.set("watershed", _ws(0, "01111111"))
    s.set("streamflow", {"data": {"q": [1, 2, 3]}, "meta": {}})
    s.save()


def test_signatures_computed_for_the_old_basin_do_not_land_next_to_a_new_basin(sessions):
    _seed_basin()
    a, b = HydroSession.load("bs"), HydroSession.load("bs")
    b.set("watershed", _ws(5, "02222222"))
    b.save()
    a.set("signatures", {"data": {"x": 1}, "meta": {}})        # stamped for basin X
    a.save()
    raw = json.loads(HydroSession._path("bs").read_text())
    assert not raw.get("signatures") and not raw.get("streamflow")
    final = HydroSession.load("bs")
    assert final.get("watershed")["data"]["gauge_id"] == "02222222"
    assert final.site_id == ""


def test_a_slot_dropped_by_a_basin_change_is_not_resurrected_by_another_save(sessions):
    _seed_basin()
    a, b = HydroSession.load("bs"), HydroSession.load("bs")
    b.set("streamflow", {"data": {"q": [9, 9, 9]}, "meta": {}})
    b.save()
    a.set("watershed", _ws(5, "02222222"))
    a.save()
    raw = json.loads(HydroSession._path("bs").read_text())
    assert not raw.get("streamflow")
    assert HydroSession.load("bs").get("streamflow") is None


def test_results_for_the_new_basin_computed_after_the_change_survive(sessions):
    _seed_basin()
    a = HydroSession.load("bs")
    other = HydroSession.load("bs")
    other.add_note("unrelated write")
    other.save()
    a.set("watershed", _ws(5, "02222222"))
    a.set("signatures", {"data": {"x": 1}, "meta": {}})        # stamped for the new basin
    a.save()
    assert HydroSession.load("bs").get("signatures")["data"]["x"] == 1


# ---- B3: a filesystem without flock --------------------------------------------------------------------

def test_flock_unsupported_degrades_instead_of_reporting_busy(sessions, monkeypatch, caplog):
    import fcntl

    from ai_hydro.registry import locking

    real = fcntl.flock

    def no_locks(fd, op):
        if op & fcntl.LOCK_NB:
            raise OSError(errno.ENOLCK, "No locks available")
        return real(fd, op)

    monkeypatch.setattr(locking._fcntl, "flock", no_locks)
    monkeypatch.setattr(locking, "_warned_unsupported", False)
    monkeypatch.setenv("AIHYDRO_SESSION_WRITE_WAIT_S", "1")
    s = HydroSession("lk")
    s.add_note("x")
    with caplog.at_level("WARNING"):
        s.save()
        s.add_note("y")
        s.save()
    assert HydroSession.load("lk").notes == ["x", "y"]
    assert sum("File locking is unavailable" in r.message for r in caplog.records) == 1


def test_real_contention_is_still_busy(sessions, monkeypatch):
    from ai_hydro.registry.locking import LockTimeout, file_lock_bounded

    p = sessions / "x.lock"
    release, held = threading.Event(), threading.Event()

    def holder():
        with file_lock_bounded(p, 2):
            held.set()
            release.wait(3)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(2)
    try:
        # same-process second fd: flock contends across descriptors
        with pytest.raises(LockTimeout):
            with file_lock_bounded(p, 0.2):
                pass
    finally:
        release.set()
        t.join()


# ---- B4: a cancelled call writes nothing and is sealed as cancelled ---------------------------------

def test_a_cancelled_call_cannot_save_its_result(sessions):
    from ai_hydro.core.cancel import ToolCancelled, bind_cancel_event

    s = HydroSession("cx")
    s.save()
    s = HydroSession.load("cx")
    s.add_note("must not be written")
    ev = threading.Event()
    ev.set()
    bind_cancel_event(ev)
    try:
        with pytest.raises(ToolCancelled):
            s.save()
    finally:
        bind_cancel_event(None)
    assert "must not be written" not in HydroSession.load("cx").notes


def test_a_cancelled_call_is_sealed_as_cancelled(sessions):
    from fastmcp import FastMCP

    from ai_hydro.mcp import app

    srv = FastMCP(name="probe")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())

    @srv.tool()
    async def slow_tool(session_id: str) -> dict:
        await asyncio.sleep(5)
        return {"data": {}}

    HydroSession("sealc").save()
    handler = srv._mcp_server.request_handlers[mcp_types.CallToolRequest]

    async def scenario():
        req = mcp_types.CallToolRequest(method="tools/call", params=mcp_types.CallToolRequestParams(
            name="slow_tool", arguments={"session_id": "sealc"}))
        task = asyncio.create_task(handler(req))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            await asyncio.sleep(0.05)
            log = json.dumps(HydroSession.load("sealc").get("_run_log") or {})
            if "ToolCancelled" in log and "may still complete" in log:
                return True
        return False

    assert asyncio.run(scenario()), "the cancelled call left no CANCELLED record"


# ---- N1: reserved workers and the wait cap -----------------------------------------------------------

def _call(name, args=None):
    from ai_hydro.mcp.app import mcp

    handler = mcp._mcp_server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(method="tools/call",
                                    params=mcp_types.CallToolRequestParams(name=name, arguments=args or {}))

    async def run():
        r = await handler(req)
        root = r.root if hasattr(r, "root") else r
        return bool(root.isError), (root.content[0].text if root.content else "")

    return run()


def test_audit_answers_while_the_main_pool_is_saturated(sessions, monkeypatch):
    import concurrent.futures

    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp import offload
    from ai_hydro.mcp.app import mcp

    monkeypatch.setattr(offload, "_POOL", concurrent.futures.ThreadPoolExecutor(max_workers=3))
    monkeypatch.setattr(offload, "_RESERVED", None)

    @mcp.tool(name="_t1_hang")
    def _t1_hang() -> dict:
        """hangs"""
        time.sleep(1.5)
        return {"ok": 1}

    HydroSession("au").save()

    async def scenario():
        hangs = [asyncio.create_task(_call("_t1_hang")) for _ in range(3)]
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        res = await _call("audit_interpretation", {"session_id": "au", "prose": "No numbers."})
        took = time.monotonic() - t0
        await asyncio.gather(*hangs)
        return res, took

    try:
        res, took = asyncio.run(scenario())
    finally:
        mcp.remove_tool("_t1_hang")
    assert took < 1.0, f"audit took {took:.2f}s behind saturated workers"


def test_concurrent_waits_are_capped(monkeypatch):
    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp import offload
    from ai_hydro.mcp.app import mcp

    monkeypatch.setitem(offload._CLASS_OF, "_t1_wait", "wait")
    monkeypatch.setitem(offload._CLASS_LIMIT, "wait", 1)

    @mcp.tool(name="_t1_wait")
    def _t1_wait() -> dict:
        """waits"""
        time.sleep(0.6)
        return {"ok": 1}

    async def scenario():
        first = asyncio.create_task(_call("_t1_wait"))
        await asyncio.sleep(0.2)
        second = await _call("_t1_wait")
        return second, await first

    try:
        second, first = asyncio.run(scenario())
    finally:
        mcp.remove_tool("_t1_wait")
    assert second[0] is True and "TOO_MANY_CONCURRENT_CALLS" in second[1]
    assert first[0] is False


def test_abandoned_threads_are_reported():
    from ai_hydro.core import cancel as c

    with pytest.raises(TimeoutError):
        c.run_bounded(time.sleep, 1.0, timeout=0.2, label="slow-upstream", poll=0.05)
    assert any(a["label"] == "slow-upstream" for a in c.abandoned_threads())
    time.sleep(1.3)
    assert not any(a["label"] == "slow-upstream" for a in c.abandoned_threads())
