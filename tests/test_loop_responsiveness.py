"""The server answers while a long tool runs (T1-R).

Field failure (tools 2.8.4): a sync ``def`` tool was run by FastMCP *on the event loop*
(``type_adapter.validate_python`` calls it inline).  It waited on an RLock that a worker thread of
a long ``fetch_forcing_data`` held, so the loop stood still for over an hour: every later call hit
the client timeout and the final-answer audit never ran.

These tests drive the real ``CallToolRequest`` handler (argument validation + the whole middleware
chain, as the extension does) and prove the class of defect is closed:

* a sync tool that blocks on a lock held by another thread no longer stalls cheap tools or the
  audit entry point;
* a contract test walks every registered tool and fails if one would run on the loop;
* a client cancel reaches the worker as a cooperative flag.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
import time

import mcp.types as mcp_types
import pytest

HOLD_S = 1.5          # how long the stand-in "long fetch" holds the lock
BUDGET_S = 1.0        # a cheap call must come back inside this, while the lock is held


def _handler():
    from ai_hydro.mcp.app import mcp

    return mcp._mcp_server.request_handlers[mcp_types.CallToolRequest]


async def _call(name: str, arguments: dict | None = None) -> tuple[bool, str]:
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments or {}),
    )
    result = await _handler()(req)
    root = result.root if hasattr(result, "root") else result
    return bool(root.isError), (root.content[0].text if root.content else "")


@pytest.fixture
def probes():
    """Register stand-in tools on the real server, remove them afterwards."""
    import ai_hydro.mcp  # noqa: F401  (registers every tool)
    from ai_hydro.mcp.app import mcp

    lock = threading.RLock()
    state = {"holder_started": threading.Event(), "thread_of_sync": None, "cancel_seen": threading.Event()}

    @mcp.tool(name="_t1_long_holder")
    async def _t1_long_holder() -> dict:
        """Stand-in for fetch_forcing_data: a worker thread holds a lock across slow work."""
        def work():
            with lock:
                state["holder_started"].set()
                time.sleep(HOLD_S)
            return {"held": HOLD_S}
        return await asyncio.to_thread(work)

    @mcp.tool(name="_t1_sync_waiter")
    def _t1_sync_waiter() -> dict:
        """A plain ``def`` tool that needs the lock the long tool holds (the 2.8.4 hang)."""
        state["thread_of_sync"] = threading.current_thread()
        with lock:
            return {"ok": True}

    @mcp.tool(name="_t1_sync_cancellable")
    def _t1_sync_cancellable() -> dict:
        """A sync tool that polls the cooperative cancel flag, as chunked fetches do."""
        from ai_hydro.mcp.offload import ToolCancelled, check_cancelled

        try:
            for _ in range(200):
                check_cancelled()
                time.sleep(0.02)
        except ToolCancelled:
            state["cancel_seen"].set()
            raise
        return {"finished": True}

    yield state
    for name in ("_t1_long_holder", "_t1_sync_waiter", "_t1_sync_cancellable"):
        try:
            mcp.remove_tool(name)
        except Exception:
            pass


def test_sync_tool_waiting_on_a_held_lock_does_not_stall_the_server(probes):
    """Reproduction of the 2.8.4 hang: loop must keep answering cheap tools and the audit."""
    lag = {"max": 0.0}

    async def heartbeat():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            lag["max"] = max(lag["max"], now - last - 0.05)
            last = now

    async def scenario():
        hb = asyncio.create_task(heartbeat())
        holder = asyncio.create_task(_call("_t1_long_holder"))
        while not probes["holder_started"].is_set():
            await asyncio.sleep(0.01)
        waiter = asyncio.create_task(_call("_t1_sync_waiter"))
        t0 = time.monotonic()

        async def timed(name, args=None):
            out = await _call(name, args)
            return out, time.monotonic() - t0

        # Issued at the same moment the waiter starts, so a wedged loop shows up as latency.
        (cheap, cheap_s), (audit, audit_s), (summary, summary_s) = await asyncio.gather(
            timed("list_available_tools"),
            timed("audit_interpretation", {"session_id": "t1-no-such-session", "prose": "No numbers."}),
            timed("get_session_summary", {"session_id": "t1-no-such-session"}),
        )
        still_waiting = not waiter.done()              # the waiter really is blocked on the lock
        await asyncio.gather(holder, waiter)
        hb.cancel()
        return cheap, cheap_s, audit, audit_s, summary, summary_s, still_waiting

    cheap, cheap_s, audit, audit_s, summary, summary_s, still_waiting = asyncio.run(scenario())

    assert cheap[0] is False
    assert cheap_s < BUDGET_S, f"list_available_tools took {cheap_s:.2f}s while a sync tool waited on a lock"
    assert audit_s < BUDGET_S, f"audit_interpretation took {audit_s:.2f}s while a sync tool waited on a lock"
    assert summary_s < BUDGET_S, f"get_session_summary took {summary_s:.2f}s while a sync tool waited on a lock"
    assert lag["max"] < BUDGET_S * 0.5, f"event loop stalled for {lag['max']:.2f}s"
    assert still_waiting, "the sync probe should have been waiting on the held lock"
    assert probes["thread_of_sync"] is not threading.main_thread()


def test_every_registered_tool_runs_off_the_loop():
    """Contract: no registered tool function is a plain sync callable (FastMCP would run it inline)."""
    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp.app import mcp
    from ai_hydro.mcp.tool_surface import _components

    on_loop = []
    checked = 0
    for comp in _components(mcp).values():
        fn = getattr(comp, "fn", None)
        if fn is None:                      # resources / prompts
            continue
        checked += 1
        if not inspect.iscoroutinefunction(fn):
            on_loop.append(getattr(comp, "name", repr(fn)))
    assert checked > 100, "expected to find the full tool set"
    assert not on_loop, f"sync tools that would run on the event loop: {sorted(on_loop)}"


def test_offload_keeps_schema_signature_and_direct_callability():
    """Registration must not change what clients see or how tests/internal callers use the function."""
    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp import tools_session
    from ai_hydro.mcp.app import mcp
    from ai_hydro.mcp.tool_surface import _components

    assert not inspect.iscoroutinefunction(tools_session.get_session_summary)      # still the plain function
    tool = next(c for c in _components(mcp).values() if getattr(c, "name", "") == "get_session_summary")
    assert "session_id" in tool.parameters["properties"]
    assert tool.description and "computed/pending slots" in tool.description


def test_context_vars_reach_the_worker_thread(probes):
    """Chat/workspace injection must be visible to a sync tool now running in a thread."""
    from ai_hydro.mcp.app import ACTIVE_CHAT_ID, ACTIVE_WORKSPACE, mcp

    seen = {}

    @mcp.tool(name="_t1_ctx_probe")
    def _t1_ctx_probe() -> dict:
        """Report the request context as the worker sees it."""
        seen["chat"] = ACTIVE_CHAT_ID.get()
        seen["ws"] = ACTIVE_WORKSPACE.get()
        seen["thread"] = threading.current_thread()
        return {"ok": True}

    try:
        asyncio.run(_call("_t1_ctx_probe", {"_chat_id": "01KV1EPM88P9FV5CWNVJ9V60JY", "_workspace": "/tmp/ws-t1"}))
    finally:
        mcp.remove_tool("_t1_ctx_probe")
    assert seen["chat"] == "01KV1EPM88P9FV5CWNVJ9V60JY"
    assert seen["ws"] == "/tmp/ws-t1"
    assert seen["thread"] is not threading.main_thread()


def test_client_cancel_sets_the_workers_cancel_flag(probes):
    """A cancelled call (client ``notifications/cancelled``) stops the worker at its next check."""
    async def scenario():
        task = asyncio.create_task(_call("_t1_sync_cancellable"))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if probes["cancel_seen"].is_set():
                break
            await asyncio.sleep(0.02)

    asyncio.run(scenario())
    assert probes["cancel_seen"].is_set(), "worker kept running after the call was cancelled"


def test_offloaded_tools_keep_their_published_schema():
    """Every offloaded tool advertises exactly the schema its plain function would."""
    import ai_hydro.mcp  # noqa: F401
    from fastmcp.tools.tool import FunctionTool

    from ai_hydro.mcp.app import mcp
    from ai_hydro.mcp.tool_surface import _components

    compared, drift = 0, []
    for comp in _components(mcp).values():
        origin = getattr(getattr(comp, "fn", None), "__sync_origin__", None)
        if origin is None or not isinstance(comp, FunctionTool):
            continue
        ref = FunctionTool.from_function(origin, name=comp.name)
        compared += 1
        if comp.parameters != ref.parameters or comp.output_schema != ref.output_schema:
            drift.append(comp.name)
    assert compared > 100
    assert not drift, f"offload changed the schema of: {drift}"


def test_sealing_the_run_record_does_not_run_on_the_loop(tmp_path, monkeypatch):
    """The run-log write (SQLite, long lock waits) is part of every recorded call: off the loop too."""
    import ai_hydro.mcp  # noqa: F401
    import ai_hydro.session.store as store
    from ai_hydro.session import chat_binding, run_records
    from ai_hydro.session.chat_binding import ChatBindingStore
    from ai_hydro.session.store import HydroSession

    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    HydroSession("t1-seal").save()
    seen = {}
    real = run_records.record_call

    def spy(**kw):
        seen["thread"] = threading.current_thread()
        return real(**kw)

    monkeypatch.setattr(run_records, "record_call", spy)
    is_error, _ = asyncio.run(_call("add_note", {"session_id": "t1-seal", "note": "hello"}))
    assert not is_error
    assert seen["thread"] is not threading.main_thread()


def test_loop_watchdog_reports_what_blocked_the_loop():
    """If something ever blocks the loop again, the log names the blocking frame."""
    from ai_hydro.mcp.offload import start_loop_watchdog

    got = {}

    def blocking_function_under_test():
        time.sleep(1.3)

    async def scenario():
        start_loop_watchdog(asyncio.get_running_loop(), threshold_s=0.5,
                            on_stall=lambda lag, stack: got.update(lag=lag, stack=stack))
        await asyncio.sleep(0.7)                 # let the heartbeat start
        blocking_function_under_test()           # blocks the loop thread
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert got.get("lag", 0) > 0.5
    assert "blocking_function_under_test" in got["stack"]


def test_offloaded_wrapper_resolves_annotations_in_the_tools_own_namespace():
    """Python 3.10 pydantic reads the callable's ``__globals__``; a wrapper from another module turned
    ``Any``-annotated parameters (data_fetch's geometry) into a different schema in CI."""
    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp.app import mcp
    from ai_hydro.mcp.tool_surface import _components

    checked = 0
    for comp in _components(mcp).values():
        fn = getattr(comp, "fn", None)
        origin = getattr(fn, "__sync_origin__", None)
        if origin is not None and hasattr(origin, "__globals__"):
            checked += 1
            assert fn.__globals__ is origin.__globals__, comp.name
    assert checked > 100
