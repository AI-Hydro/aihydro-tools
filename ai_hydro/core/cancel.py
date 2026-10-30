"""Cooperative cancellation, wall-clock budgets and progress for long tool work (T1-R).

A tool runs in a worker thread (``ai_hydro.mcp.offload``).  A thread cannot be killed, so a client
cancel / timeout is delivered as a flag the work polls between chunks, and a hung network call is
bounded by ``run_bounded`` (the caller stops waiting; at worst one finished-in-bounded-time
daemon thread lingers).  This module has no MCP imports so the data layer can use it.
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from typing import Any, Callable

_CANCEL: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "aihydro_tool_cancel", default=None)
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "aihydro_tool_deadline", default=None)
_PROGRESS: contextvars.ContextVar[Callable[[float, float, str], None] | None] = contextvars.ContextVar(
    "aihydro_tool_progress", default=None)


class ToolCancelled(Exception):
    """The call was cancelled by the client; raised inside the worker by ``check_cancelled()``."""

    def to_dict(self) -> dict:
        return {"error": True, "code": "CANCELLED", "retryable": True, "message": str(self),
                "recovery": "Call again if the result is still wanted."}


class ToolDeadlineExceeded(TimeoutError):
    """A bounded long operation ran out of its wall-clock budget."""

    def to_dict(self) -> dict:
        return {"error": True, "code": "TIME_BUDGET_EXCEEDED", "retryable": True, "message": str(self),
                "recovery": ("Call again: chunks already fetched are cached, so the retry resumes. "
                             "Raise AIHYDRO_FORCING_BUDGET_S, or request a shorter window.")}


def bind_cancel_event(event: threading.Event | None) -> None:
    _CANCEL.set(event)


def cancel_requested() -> bool:
    """True when the running call was cancelled (always False outside a tool call)."""
    ev = _CANCEL.get()
    return bool(ev is not None and ev.is_set())


def check_cancelled() -> None:
    """Raise ``ToolCancelled`` when the call was cancelled. Call between chunks of long work."""
    if cancel_requested():
        raise ToolCancelled("call cancelled by the client")


def set_deadline(seconds: float | None) -> None:
    """Give the running call a wall-clock budget; ``check_cancelled_or_expired`` enforces it."""
    _DEADLINE.set(None if seconds is None else time.monotonic() + float(seconds))


@contextlib.contextmanager
def budget(seconds: float):
    """Scope a wall-clock budget to the block (an already-active, tighter budget is kept)."""
    left = seconds_left()
    if left is not None and left <= seconds:
        yield
        return
    token = _DEADLINE.set(time.monotonic() + float(seconds))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def seconds_left() -> float | None:
    dl = _DEADLINE.get()
    return None if dl is None else dl - time.monotonic()


def check_cancelled_or_expired() -> None:
    check_cancelled()
    left = seconds_left()
    if left is not None and left <= 0:
        raise ToolDeadlineExceeded("the wall-clock budget for this operation is exhausted")


def set_progress_sink(sink: Callable[[float, float, str], None] | None) -> None:
    _PROGRESS.set(sink)


def report_progress(done: float, total: float, message: str = "") -> None:
    """Forward progress to the MCP client when a sink is bound; never raises."""
    sink = _PROGRESS.get()
    if sink is None:
        return
    try:
        sink(done, total, message)
    except Exception:
        pass


def run_bounded(fn: Callable[..., Any], *args: Any, timeout: float | None = None, label: str = "",
                poll: float = 0.25, **kwargs: Any) -> Any:
    """Run ``fn`` in a daemon thread and wait for it, honouring cancel, the call's budget and ``timeout``.

    Returns ``fn``'s result or re-raises its exception.  When the caller is cancelled, the budget
    is spent, or ``timeout`` elapses, the caller stops waiting at once (``ToolCancelled`` /
    ``ToolDeadlineExceeded`` / ``TimeoutError``); the abandoned thread ends when its blocking call
    returns and its result is dropped.  This is the bound on a network read that offers none.
    """
    ctx = contextvars.copy_context()
    box: dict[str, Any] = {}
    done = threading.Event()

    def _target() -> None:
        try:
            box["value"] = ctx.run(fn, *args, **kwargs)
        except BaseException as exc:      # noqa: BLE001 - handed to the waiting caller
            box["error"] = exc
        finally:
            done.set()

    started = time.monotonic()
    threading.Thread(target=_target, name=f"aihydro-bounded-{label or 'call'}", daemon=True).start()
    while not done.wait(poll):
        check_cancelled_or_expired()
        if timeout is not None and time.monotonic() - started > timeout:
            raise TimeoutError(f"{label or 'operation'} did not finish within {timeout:g}s")
    if "error" in box:
        raise box["error"]
    return box["value"]
