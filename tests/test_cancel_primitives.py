"""Cooperative cancel, budget and bounded-call primitives (ai_hydro.core.cancel)."""
from __future__ import annotations

import contextvars
import threading
import time

import pytest

from ai_hydro.core import cancel as c

VAR = contextvars.ContextVar("t1_var", default="unset")


def test_run_bounded_returns_value_and_propagates_errors_and_context():
    VAR.set("ctx-value")
    assert c.run_bounded(lambda: VAR.get(), poll=0.01) == "ctx-value"
    with pytest.raises(ValueError):
        c.run_bounded(lambda: (_ for _ in ()).throw(ValueError("boom")), poll=0.01)


def test_run_bounded_stops_waiting_on_timeout_but_not_the_process():
    t0 = time.monotonic()
    with pytest.raises(TimeoutError) as err:
        c.run_bounded(time.sleep, 5, timeout=0.3, label="slow thing", poll=0.05)
    assert time.monotonic() - t0 < 1.5
    assert "slow thing" in str(err.value) and not isinstance(err.value, c.ToolDeadlineExceeded)


def test_run_bounded_stops_waiting_on_cancel():
    ev = threading.Event()
    c.bind_cancel_event(ev)
    try:
        threading.Timer(0.2, ev.set).start()
        t0 = time.monotonic()
        with pytest.raises(c.ToolCancelled):
            c.run_bounded(time.sleep, 5, poll=0.05)
        assert time.monotonic() - t0 < 1.5
    finally:
        c.bind_cancel_event(None)


def test_budget_expires_and_is_scoped():
    with c.budget(0.2):
        time.sleep(0.3)
        with pytest.raises(c.ToolDeadlineExceeded):
            c.check_cancelled_or_expired()
        with pytest.raises(c.ToolDeadlineExceeded):
            c.run_bounded(time.sleep, 5, poll=0.05)
    assert c.seconds_left() is None
    c.check_cancelled_or_expired()                       # no budget outside the block


def test_a_tighter_outer_budget_is_kept():
    with c.budget(0.5):
        with c.budget(100):
            assert c.seconds_left() <= 0.5


def test_envelopes_are_structured_and_retryable():
    assert c.ToolDeadlineExceeded("x").to_dict()["code"] == "TIME_BUDGET_EXCEEDED"
    assert c.ToolCancelled("x").to_dict()["retryable"] is True
