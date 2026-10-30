"""Forcing fetch is chunked, bounded, cancellable and keeps answering the server (T1-R)."""
from __future__ import annotations

import asyncio
import threading
import time

import mcp.types as mcp_types
import pandas as pd
import pytest

POLY = {"type": "Polygon", "coordinates": [[[-86.0, 40.0], [-85.9, 40.0], [-85.9, 40.1], [-86.0, 40.1], [-86.0, 40.0]]]}


def _series(adata_var: str, start: str, end: str) -> pd.DataFrame:
    dates = pd.date_range(start, end, freq="D")
    base = {"precipitation": 1.0, "tmax": 290.0, "tmin": 280.0, "pet": 3.0}.get(adata_var, 1.0)
    return pd.DataFrame({"date": dates, "value": [base + (d.dayofyear % 7) * 0.1 for d in dates]})


class _Res:
    def __init__(self, df, product="FAKE_" , source="fake-src"):
        self.data, self.product, self.source = df, product, source
        self.units, self.coverage_complete = "mm", True
        self.days_missing_head = self.days_missing_tail = self.days_missing_internal = 0
        self.missing_ranges, self.notes = [], ["fake note"]


@pytest.fixture
def fake_fetch(monkeypatch):
    import aihydro_data

    calls = []
    state = {"delay": 0.0, "fail_pinned": False}

    def fetch(variable, geom, start, end, **kw):
        calls.append((variable, start, end, dict(kw)))
        if state["delay"]:
            time.sleep(state["delay"])
        if state["fail_pinned"] and kw.get("mode") == "manual":
            raise RuntimeError("product cannot serve this window")
        return _Res(_series(variable, start, end), product=f"FAKE_{variable.upper()}")

    monkeypatch.setattr(aihydro_data, "fetch", fetch)
    return calls, state


def _run(**kw):
    from ai_hydro.data.forcing import fetch_forcing_data_routed

    return fetch_forcing_data_routed(POLY, kw.pop("start", "1985-01-01"), kw.pop("end", "2019-12-31"), **kw)


def test_chunks_cover_the_range_exactly():
    from ai_hydro.data.forcing import _chunk_windows

    w = _chunk_windows("1985-01-01", "2019-12-31", 5)
    assert w[0][0] == "1985-01-01" and w[-1][1] == "2019-12-31" and len(w) == 7
    for (_, e), (s, _) in zip(w, w[1:]):
        assert pd.Timestamp(s) - pd.Timestamp(e) == pd.Timedelta(days=1)
    assert _chunk_windows("2000-01-01", "2000-12-31", 5) == [("2000-01-01", "2000-12-31")]


def test_chunked_result_equals_the_single_call_result(fake_fetch, monkeypatch):
    monkeypatch.setenv("AIHYDRO_FORCING_CHUNK_YEARS", "100")
    single = _run(variables=["pr", "tmmx", "tmmn", "pet"]).to_dict()
    calls, _ = fake_fetch
    n_single = len(calls)
    calls.clear()
    monkeypatch.setenv("AIHYDRO_FORCING_CHUNK_YEARS", "5")
    chunked = _run(variables=["pr", "tmmx", "tmmn", "pet"]).to_dict()
    assert len(calls) > n_single * 5                       # really chunked
    assert chunked["data"] == single["data"]               # same values, dates, product, source


def test_later_chunks_are_pinned_to_the_first_chunks_product(fake_fetch):
    calls, _ = fake_fetch
    _run(variables=["pr"])
    assert "mode" not in calls[0][3]
    assert all(c[3].get("mode") == "manual" and c[3].get("product") == "FAKE_PRECIPITATION" for c in calls[1:])


def test_a_pinned_chunk_that_fails_falls_back_to_one_range(fake_fetch, monkeypatch):
    calls, state = fake_fetch
    monkeypatch.setenv("AIHYDRO_FORCING_CHUNK_YEARS", "100")
    ref = _run(variables=["pr"]).to_dict()
    calls.clear()
    monkeypatch.setenv("AIHYDRO_FORCING_CHUNK_YEARS", "5")
    state["fail_pinned"] = True
    out = _run(variables=["pr"]).to_dict()
    assert out["data"] == ref["data"]
    assert calls[-1][1:3] == ("1985-01-01", "2019-12-31")  # the whole-range answer


def test_budget_stops_between_chunks(fake_fetch, monkeypatch):
    from ai_hydro.core.cancel import ToolDeadlineExceeded

    calls, state = fake_fetch
    state["delay"] = 0.2
    monkeypatch.setenv("AIHYDRO_FORCING_BUDGET_S", "0.5")
    t0 = time.monotonic()
    with pytest.raises(ToolDeadlineExceeded):
        _run(variables=["pr"])
    assert time.monotonic() - t0 < 2.5
    assert len(calls) < 7


def test_a_hung_upstream_is_bounded(fake_fetch, monkeypatch):
    calls, state = fake_fetch
    state["delay"] = 5.0
    monkeypatch.setenv("AIHYDRO_FETCH_CHUNK_TIMEOUT_S", "0.5")
    from ai_hydro.core import ToolError

    t0 = time.monotonic()
    with pytest.raises(ToolError) as err:
        _run(variables=["pr"], start="2000-01-01", end="2001-12-31")
    assert time.monotonic() - t0 < 3.0
    assert err.value.code == "NO_FORCING_DATA"
    assert "did not finish within" in err.value.message


def test_cancel_stops_the_worker_between_chunks(fake_fetch):
    from ai_hydro.mcp.offload import run_off_loop

    calls, state = fake_fetch
    state["delay"] = 0.3

    async def scenario():
        task = asyncio.create_task(run_off_loop(_run, variables=["pr"]))
        await asyncio.sleep(0.45)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.0)                           # a zombie would keep fetching
        return len(calls)

    seen = asyncio.run(scenario())
    assert seen <= 3, f"worker kept fetching after the cancel ({seen} chunks)"


def test_forcing_tool_keeps_the_server_answering(fake_fetch, tmp_path, monkeypatch):
    """The 2.8.4 scenario: a long fetch_forcing_data running; audit and summary stay prompt."""
    import ai_hydro.mcp  # noqa: F401
    import ai_hydro.session.store as store
    from ai_hydro.mcp.app import mcp

    d = tmp_path / "sessions"
    monkeypatch.setattr(store, "SESSIONS_DIR", d)
    monkeypatch.setattr(store, "_SESSIONS_DIR", d)
    calls, state = fake_fetch
    state["delay"] = 0.4                                   # 7 chunks x 4 vars: seconds of "network"
    handler = mcp._mcp_server.request_handlers[mcp_types.CallToolRequest]

    async def call(name, args):
        req = mcp_types.CallToolRequest(method="tools/call",
                                        params=mcp_types.CallToolRequestParams(name=name, arguments=args))
        res = await handler(req)
        root = res.root if hasattr(res, "root") else res
        return bool(root.isError), (root.content[0].text if root.content else "")

    async def scenario():
        import json

        long_call = asyncio.create_task(call("fetch_forcing_data", {
            "start_date": "1985-01-01", "end_date": "2019-12-31", "session_id": "t1-forcing",
            "geometry_geojson": json.dumps(POLY)}))
        await asyncio.sleep(0.5)
        t0 = time.monotonic()
        await call("get_session_summary", {"session_id": "t1-forcing"})
        s_t = time.monotonic() - t0
        t0 = time.monotonic()
        await call("audit_interpretation", {"session_id": "t1-forcing", "prose": "No numbers here."})
        a_t = time.monotonic() - t0
        running = not long_call.done()
        long_call.cancel()
        try:
            await long_call
        except asyncio.CancelledError:
            pass
        return s_t, a_t, running

    s_t, a_t, running = asyncio.run(scenario())
    assert running, "the long forcing call should still have been running"
    assert s_t < 1.0 and a_t < 1.0, f"summary {s_t:.2f}s / audit {a_t:.2f}s while forcing ran"


def test_budget_envelope_is_retryable_and_a_resumed_call_matches_the_single_call(fake_fetch, monkeypatch):
    """N4: a budget stop says how far it got; calling again resumes from cached chunks, same values."""
    from ai_hydro.core.cancel import ToolDeadlineExceeded

    calls, state = fake_fetch
    cache = {}
    import aihydro_data

    inner = aihydro_data.fetch

    def cached_fetch(variable, geom, start, end, **kw):          # aihydro-data's disk cache, per window
        key = (variable, start, end)
        if key not in cache:
            if state["delay"]:
                time.sleep(state["delay"])
            cache[key] = inner.__wrapped__(variable, geom, start, end, **kw) if hasattr(inner, "__wrapped__") \
                else _Res(_series(variable, start, end), product=f"FAKE_{variable.upper()}")
        return cache[key]

    monkeypatch.setattr(aihydro_data, "fetch", cached_fetch)
    monkeypatch.setenv("AIHYDRO_FORCING_CHUNK_YEARS", "100")
    ref = _run(variables=["pr"]).to_dict()
    cache.clear()
    monkeypatch.setenv("AIHYDRO_FORCING_CHUNK_YEARS", "5")
    state["delay"] = 0.25
    monkeypatch.setenv("AIHYDRO_FORCING_BUDGET_S", "0.8")
    with pytest.raises(ToolDeadlineExceeded) as err:
        _run(variables=["pr"])
    env = err.value.to_dict()
    assert env["code"] == "TIME_BUDGET_EXCEEDED" and env["retryable"] is True
    assert 0 < env["chunks_done"] < env["chunks_total"] == 7
    assert "call again to continue" in env["recovery"].lower() and "raise" not in env["recovery"].lower()
    monkeypatch.setenv("AIHYDRO_FORCING_BUDGET_S", "30")
    state["delay"] = 0.0
    resumed = _run(variables=["pr"]).to_dict()
    assert resumed["data"] == ref["data"]


def test_default_budget_is_under_the_client_timeout():
    from ai_hydro.data import forcing

    assert forcing._DEFAULT_BUDGET_S == 480.0


def test_async_tool_save_waits_for_a_busy_guard_instead_of_failing(fake_fetch, tmp_path, monkeypatch):
    """N5: fetch_forcing_data's save runs off the loop with the normal wait, not the 0.25 s loop backstop."""
    import json

    import ai_hydro.mcp  # noqa: F401
    import ai_hydro.session.store as store
    from ai_hydro.mcp.app import mcp

    d = tmp_path / "sessions"
    monkeypatch.setattr(store, "SESSIONS_DIR", d)
    monkeypatch.setattr(store, "_SESSIONS_DIR", d)
    store.HydroSession("t1-async").save()
    target = store.HydroSession._path("t1-async")
    handler = mcp._mcp_server.request_handlers[mcp_types.CallToolRequest]
    entered, release = threading.Event(), threading.Event()

    def holder():
        with store._session_write_guard(target, "t1-async"):
            entered.set()
            release.wait(5)

    threading.Thread(target=holder, daemon=True).start()
    entered.wait(2)
    threading.Timer(0.8, release.set).start()
    req = mcp_types.CallToolRequest(method="tools/call", params=mcp_types.CallToolRequestParams(
        name="fetch_forcing_data", arguments={"start_date": "2000-01-01", "end_date": "2001-12-31",
                                              "session_id": "t1-async", "geometry_geojson": json.dumps(POLY)}))
    r = asyncio.run(handler(req))
    root = r.root if hasattr(r, "root") else r
    text = root.content[0].text
    assert "SESSION_BUSY" not in text, text[:300]
    assert not root.isError
