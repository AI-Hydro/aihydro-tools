"""Run every tool off the event loop (T1-R).

FastMCP 2.14 runs a plain ``def`` tool *inline*: ``FunctionTool.run`` calls
``type_adapter.validate_python(arguments)``, which executes the function on the asyncio loop.  One
sync tool that waits (on a lock a worker thread holds, on a network read, on a ``time.sleep``
poll) therefore stops the whole server: no other call is read, no cancel notification is
delivered, the final-answer audit times out.  Observed in tools 2.8.4: the loop stood still for
over an hour behind one tool.

The mechanism is one rule applied at registration (``offload_sync``): a sync tool function is
registered as an ``async`` wrapper that runs the original in a worker thread, with

* the caller's ``contextvars`` copied in (chat/workspace/study context, run-record capture, call
  notes) so context injection and the middleware chain see the same state as before;
* the original signature, annotations and docstring (``functools.wraps``), so the tool schema is
  unchanged; the decorated name in the module still refers to the plain function;
* a dedicated pool (``AIHYDRO_TOOL_THREADS``, default 64), so long tools can never starve the
  default executor that ``asyncio.to_thread`` and cheap tools use;
* a cooperative cancel flag: when the awaiting call is cancelled (the client sent
  ``notifications/cancelled``, or the transport closed) the flag is set and a long worker stops at
  its next ``check_cancelled()``.  A thread cannot be killed; this is the contract.

Async tools are left alone (they already yield to the loop).  ``tests/test_loop_responsiveness.py``
walks every registered tool and fails if one would still run on the loop.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import inspect
import itertools
import logging
import os
import threading
import time
import types
from typing import Any, Callable

log = logging.getLogger("ai_hydro.mcp.offload")

_MARK = "__aihydro_offloaded__"


from ai_hydro.core.cancel import (  # noqa: E402,F401  (re-exported: tools import them from here)
    ToolCancelled,
    ToolDeadlineExceeded,
    bind_cancel_event,
    cancel_requested,
    check_cancelled,
    check_cancelled_or_expired,
    report_progress,
    run_bounded,
    seconds_left,
    set_deadline,
    set_progress_sink,
)
from ai_hydro.core.cancel import _DEADLINE  # noqa: E402


# ---------------------------------------------------------------------------
# What is running (named in SESSION_BUSY results; cheap to read from any thread)
# ---------------------------------------------------------------------------
_RUNNING_LOCK = threading.Lock()
_RUNNING: dict[int, dict[str, Any]] = {}
_IDS = itertools.count(1)


def running_tools() -> list[dict[str, Any]]:
    """Snapshot of in-flight tool calls: ``[{tool, session_id, started_monotonic, elapsed_s}]``."""
    now = time.monotonic()
    with _RUNNING_LOCK:
        rows = [dict(r) for r in _RUNNING.values()]
    for r in rows:
        r["elapsed_s"] = round(now - r["started_monotonic"], 1)
    return rows


def note_running_session(session_id: str) -> None:
    """Record which study the running call resolved to (for SESSION_BUSY messages)."""
    cid = _CALL_ID.get()
    if cid is None:
        return
    with _RUNNING_LOCK:
        row = _RUNNING.get(cid)
        if row is not None:
            row["session_id"] = session_id


_CALL_ID: contextvars.ContextVar[int | None] = contextvars.ContextVar("aihydro_tool_call_id", default=None)

_POOL: concurrent.futures.ThreadPoolExecutor | None = None
_POOL_LOCK = threading.Lock()


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                try:
                    n = max(4, int(os.environ.get("AIHYDRO_TOOL_THREADS", "64")))
                except ValueError:
                    n = 64
                _POOL = concurrent.futures.ThreadPoolExecutor(
                    max_workers=n, thread_name_prefix="aihydro-tool")
    return _POOL


_RESERVED: concurrent.futures.ThreadPoolExecutor | None = None


def _reserved_pool() -> concurrent.futures.ThreadPoolExecutor:
    """A small pool kept free for the audit, read-only status tools and run-record sealing, so tools
    that hang cannot starve the calls that tell the user what is happening."""
    global _RESERVED
    if _RESERVED is None:
        with _POOL_LOCK:
            if _RESERVED is None:
                try:
                    n = max(2, int(os.environ.get("AIHYDRO_RESERVED_THREADS", "6")))
                except ValueError:
                    n = 6
                _RESERVED = concurrent.futures.ThreadPoolExecutor(
                    max_workers=n, thread_name_prefix="aihydro-reserved")
    return _RESERVED


_ALWAYS_RESERVED = frozenset({"audit_interpretation"})


def _uses_reserved_pool(name: str | None, internal: bool) -> bool:
    if internal or (name in _ALWAYS_RESERVED):
        return True
    if not name or name in _CLASS_OF:       # a long wait never takes a reserved worker
        return False
    try:
        from ai_hydro.mcp.tool_surface import read_only_tools

        return name in read_only_tools()
    except Exception:
        return False


# Concurrency caps for classes of long-lived waiting calls (checked on the loop thread, no lock needed).
# wait_for_job parks a worker for up to its wait budget; unbounded waits would fill the pool.
_CLASS_OF = {"wait_for_job": "wait"}
_CLASS_LIMIT = {"wait": 4}
_CLASS_ACTIVE: dict[str, int] = {}


def class_busy_envelope(name: str, cls: str) -> dict:
    return {"error": True, "code": "TOO_MANY_CONCURRENT_CALLS", "retryable": True,
            "message": (f"{name}: {_CLASS_LIMIT[cls]} calls of this kind are already waiting; "
                        "more would starve the server's workers."),
            "recovery": "Wait for one of them to finish (they return in time), then call again."}


async def run_off_loop(fn: Callable[..., Any], *args: Any, _tool_name: str | None = None,
                       _track: bool = True, **kwargs: Any) -> Any:
    """Run ``fn(*args, **kwargs)`` in the tool pool with this task's contextvars; await the result.

    Cancelling the awaiting task sets the worker's cancel flag (see ``check_cancelled``).
    """
    loop = asyncio.get_running_loop()
    cancel = threading.Event()
    call_id = next(_IDS)
    ctx = contextvars.copy_context()
    ctx.run(bind_cancel_event, cancel)
    ctx.run(_DEADLINE.set, None)
    ctx.run(_CALL_ID.set, call_id)
    if _tool_name is None:
        try:
            from ai_hydro.mcp.app import ACTIVE_TOOL_NAME

            _tool_name = ACTIVE_TOOL_NAME.get()
        except Exception:
            _tool_name = None
    name = _tool_name or getattr(fn, "__name__", "tool")
    _ensure_watchdog(loop)
    if _track:
        with _RUNNING_LOCK:
            _RUNNING[call_id] = {"tool": name, "session_id": None, "started_monotonic": time.monotonic()}

    def _run() -> Any:
        try:
            return ctx.run(fn, *args, **kwargs)
        finally:
            if _track:
                with _RUNNING_LOCK:
                    _RUNNING.pop(call_id, None)

    pool = _reserved_pool() if _uses_reserved_pool(name if _tool_name else None, not _track) else _pool()
    try:
        return await loop.run_in_executor(pool, _run)
    except BaseException:
        # CancelledError (client cancel / transport closed) or a tool error: tell a still-running
        # worker to stop at its next check. Harmless when it already finished.
        cancel.set()
        raise


def progress_sink_for(ctx: Any) -> Callable[[float, float, str], None] | None:
    """A thread-safe ``report(done, total, message)`` that forwards to ``ctx.report_progress``.

    Bind it with ``set_progress_sink`` in an async tool before ``run_off_loop``; long worker code
    calls ``report_progress`` and the client sees MCP progress notifications. ``None`` without ctx.
    """
    if ctx is None:
        return None
    loop = asyncio.get_running_loop()

    def sink(done: float, total: float, message: str = "") -> None:
        fut = asyncio.run_coroutine_threadsafe(
            ctx.report_progress(progress=done, total=total, message=message or None), loop)
        fut.add_done_callback(lambda f: f.exception())   # consume; progress is best-effort

    return sink


def offload_sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    """The async wrapper FastMCP registers in place of a sync tool function (idempotent)."""
    if inspect.iscoroutinefunction(fn) or getattr(fn, _MARK, False):
        return fn

    async def _dispatch(*args: Any, **kwargs: Any) -> Any:
        from ai_hydro.mcp.app import ACTIVE_TOOL_NAME

        name = ACTIVE_TOOL_NAME.get() or fn.__name__
        cls = _CLASS_OF.get(name)
        if cls is not None:
            if _CLASS_ACTIVE.get(cls, 0) >= _CLASS_LIMIT[cls]:
                return class_busy_envelope(name, cls)
            _CLASS_ACTIVE[cls] = _CLASS_ACTIVE.get(cls, 0) + 1
        try:
            return await run_off_loop(fn, *args, _tool_name=name, **kwargs)
        finally:
            if cls is not None:
                _CLASS_ACTIVE[cls] -= 1

    async def _entry(*args: Any, **kwargs: Any) -> Any:
        return await _dispatch(*args, **kwargs)

    # The wrapper must resolve string annotations exactly as the tool function does. Pydantic reads
    # ``__globals__`` of the callable on Python 3.10, and this module's globals lack the tool module's
    # names (``Any``, aliases), which silently turned parameters into ``Any`` in the published schema.
    # Rebuild the entry point with the tool's own globals; its only free variable is ``_dispatch``.
    _offloaded = _entry
    if hasattr(fn, "__globals__"):
        _offloaded = types.FunctionType(_entry.__code__, fn.__globals__, fn.__name__, _entry.__defaults__,
                                        _entry.__closure__)
        _offloaded.__kwdefaults__ = _entry.__kwdefaults__
    functools.update_wrapper(_offloaded, fn)
    # Python 3.10's ``typing.get_type_hints`` wraps a parameter whose default is None in ``Optional`` by
    # reading ``__defaults__`` of the callable it is given; the wrapper has none, so a ``geometry: Any = None``
    # parameter was published differently from the plain function. Publish the annotations as the plain
    # function resolves them (identical on 3.11+).
    try:
        import typing

        _offloaded.__annotations__ = typing.get_type_hints(fn, include_extras=True)
    except Exception:
        pass
    setattr(_offloaded, _MARK, True)
    _offloaded.__sync_origin__ = fn      # type: ignore[attr-defined]
    return _offloaded


# ---------------------------------------------------------------------------
# Loop watchdog: if anything ever blocks the loop again, say what (diagnostic, never fatal)
# ---------------------------------------------------------------------------
_WATCHDOG: threading.Thread | None = None
_WATCHDOG_LOCK = threading.Lock()


def _stall_threshold_s() -> float:
    try:
        return max(0.5, float(os.environ.get("AIHYDRO_LOOP_STALL_WARN_S", "3")))
    except ValueError:
        return 3.0


def _ensure_watchdog(loop: asyncio.AbstractEventLoop) -> None:
    global _WATCHDOG
    if _WATCHDOG is not None and _WATCHDOG.is_alive():
        return
    with _WATCHDOG_LOCK:
        if _WATCHDOG is not None and _WATCHDOG.is_alive():
            return
        _WATCHDOG = start_loop_watchdog(loop)


def start_loop_watchdog(loop: asyncio.AbstractEventLoop, *, threshold_s: float | None = None,
                        on_stall: Callable[[float, str], None] | None = None) -> threading.Thread:
    """Start a daemon thread that logs the loop thread's stack when the loop stops answering.

    A heartbeat callback on the loop stamps a timestamp every 0.5 s; if the stamp is older than
    the threshold the loop is blocked, and the blocked thread's current Python stack is logged
    once per stall (``on_stall(lag_s, stack_text)`` also receives it).
    """
    import sys
    import traceback

    limit = threshold_s if threshold_s is not None else _stall_threshold_s()
    state = {"beat": time.monotonic(), "tid": None, "warned": False}

    def beat() -> None:
        state["beat"] = time.monotonic()
        state["tid"] = threading.get_ident()
        state["warned"] = False
        if not loop.is_closed():
            loop.call_later(0.5, beat)

    loop.call_soon_threadsafe(beat)

    def watch() -> None:
        last_report = 0.0
        while not loop.is_closed():
            time.sleep(min(1.0, limit / 3))
            now = time.monotonic()
            if now - last_report > 60.0:
                last_report = now
                try:
                    from ai_hydro.core.cancel import abandoned_threads

                    ab = abandoned_threads()
                    if ab:
                        log.warning("%d abandoned worker thread(s) still running (their callers gave up): %s",
                                    len(ab), ab[:8])
                    busy = running_tools()
                    if busy and len(busy) >= _pool()._max_workers:
                        log.warning("tool pool saturated: %d calls running (reserved pool keeps the audit "
                                    "and read-only tools responsive): %s", len(busy),
                                    [(r["tool"], r["elapsed_s"]) for r in busy[:8]])
                except Exception:
                    pass
            lag = time.monotonic() - state["beat"]
            tid = state["tid"]
            if lag > limit and not state["warned"] and tid is not None:
                state["warned"] = True
                frame = sys._current_frames().get(tid)
                stack = "".join(traceback.format_stack(frame)[-12:]) if frame else "(no frame)"
                log.warning("event loop blocked for %.1fs; the loop thread is in:\n%s", lag, stack)
                if on_stall is not None:
                    try:
                        on_stall(lag, stack)
                    except Exception:
                        pass

    t = threading.Thread(target=watch, name="aihydro-loop-watchdog", daemon=True)
    t.start()
    return t
