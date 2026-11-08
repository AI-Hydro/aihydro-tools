"""``data_fetch`` oddities found producing AGU evidence (2026-11), driven through the MCP client (schema + middleware).

1. ``geometry={"lat","lon"}`` (the documented point) is ONE place, not a label mapping.
2. ``data_fetch(variable="streamflow", gauge_id=...)`` routes to the pinned USGS fetch; it is not a collection.
3. ``mode="manual"`` + ``product=`` is strict over MCP: the pinned product serves or the call fails with its error.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pandas as pd
import pytest
from fastmcp import Client

from aihydro_data.contracts import FetchResult
from test_geometry_boundary import GAUGE, LAT, LON, world  # noqa: F401  (fixture)
from test_run_record_middleware import needs_fastmcp2

pytestmark = needs_fastmcp2

DAYS = ("2020-01-01", "2020-01-14")


def mcp_call(args: dict, tool: str = "data_fetch") -> dict:
    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp.app import mcp

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(tool, args, raise_on_error=False)

    res = asyncio.run(go())
    return json.loads(res.content[0].text)


def _result_for(spec, req):
    df = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=14), "precipitation": [1.0] * 14})
    return FetchResult(variable=spec.variable, product=spec.id, source=spec.source, request=req, data=df,
                       license=spec.license, citation=spec.citation)


def _fail(pinned):
    def fake(spec, geom, start, end, agg, req, **kw):
        if spec.id == pinned:
            raise RuntimeError("pinned boom")
        return _result_for(spec, req)
    return fake


# ------------------------------------------------------------------ lat/lon dict
def test_lat_lon_dict_through_mcp_is_one_place(world):  # noqa: F811
    seen = {}

    def fake(spec, geom, start, end, agg, req, **kw):
        seen["geom"] = geom
        return _result_for(spec, req)

    with patch("aihydro_data._pipeline._fetch_one", side_effect=fake):
        r = mcp_call({"variable": "precipitation", "geometry": {"lat": 45.175, "lon": -69.3147},
                      "start": DAYS[0], "end": DAYS[1], "session_id": "gb", "cache": False})
    assert not r.get("error"), r
    assert (seen["geom"].x, seen["geom"].y) == (-69.3147, 45.175)
    assert r["resolved_place"]["lat"] == 45.175


def test_west_south_east_north_dict_through_mcp(world):  # noqa: F811
    with patch("aihydro_data._pipeline._fetch_one", side_effect=lambda spec, g, s, e, a, req, **kw: _result_for(spec, req)):
        r = mcp_call({"variable": "precipitation", "geometry": {"west": -69.4, "south": 45.1, "east": -69.2, "north": 45.25},
                      "start": DAYS[0], "end": DAYS[1], "session_id": "gb", "cache": False})
    assert not r.get("error"), r


# ------------------------------------------------------------------ gauge_id
def test_streamflow_gauge_id_through_mcp_is_not_a_collection(world, monkeypatch):  # noqa: F811
    import aihydro_data as pipe
    from aihydro_data.exceptions import TerminalSourceError

    seen = {}
    real = pipe.fetch

    def spy(*a, **k):
        seen.update(k)
        # run the REAL fetch(): the collection dispatch is what is under test; stop at the backend
        with patch("aihydro_data._pipeline._fetch_one",
                   side_effect=TerminalSourceError(code="GAUGE_REQUIRED", message="stopped at the backend")):
            return real(*a, **k)

    monkeypatch.setattr(pipe, "fetch", spy)
    r = mcp_call({"variable": "streamflow", "gauge_id": GAUGE, "start": DAYS[0], "end": DAYS[1], "session_id": "gb"})
    assert r["code"] != "GAUGE_WITH_COLLECTION", r
    assert r["code"] == "GAUGE_REQUIRED"                      # reached the pinned backend
    assert seen["gauge_id"] == GAUGE and seen["product"] == "NWIS_STREAMFLOW"
    assert r["resolved_location"]["gauge"] == "USGS-" + GAUGE


def test_gauge_id_with_explicit_point_through_mcp(world, monkeypatch):  # noqa: F811
    from aihydro_data.exceptions import TerminalSourceError

    with patch("aihydro_data._pipeline._fetch_one",
               side_effect=TerminalSourceError(code="GAUGE_REQUIRED", message="stopped at the backend")):
        r = mcp_call({"variable": "streamflow", "gauge_id": GAUGE, "geometry": {"lat": LAT, "lon": LON},
                      "start": DAYS[0], "end": DAYS[1], "session_id": "gb"})
    assert r["code"] == "GAUGE_REQUIRED"


# ------------------------------------------------------------------ pinned product
def test_manual_pin_is_strict_through_mcp(world):  # noqa: F811
    args = {"variable": "precipitation", "geometry": {"lat": 45.175, "lon": -69.3147}, "start": DAYS[0], "end": DAYS[1],
            "mode": "manual", "product": "GRIDMET_PRECIP", "session_id": "gb", "cache": False}
    with patch("aihydro_data._pipeline._fetch_one", side_effect=_fail("GRIDMET_PRECIP")):
        r = mcp_call(args)
    assert r.get("error") is True, r
    assert r.get("product") != "CHIRPS_IRI" and "product_substituted" not in r
    trail = json.dumps(r)
    assert "GRIDMET_PRECIP" in trail and "pinned boom" in trail


def test_manual_pin_opt_in_fallback_is_flagged_at_top_level(world):  # noqa: F811
    args = {"variable": "precipitation", "geometry": {"lat": 45.175, "lon": -69.3147}, "start": DAYS[0], "end": DAYS[1],
            "mode": "manual", "product": "GRIDMET_PRECIP", "fallback": ["DAYMET_PRECIP"], "session_id": "gb", "cache": False}
    with patch("aihydro_data._pipeline._fetch_one", side_effect=_fail("GRIDMET_PRECIP")):
        r = mcp_call(args)
    assert not r.get("error"), r
    assert r["product"] == "DAYMET_PRECIP"
    assert r["product_substituted"] is True and r["requested_product"] == "GRIDMET_PRECIP"
    assert any("SUBSTITUTED PRODUCT" in n for n in r["notes"])


def test_manual_pin_that_serves_is_not_flagged(world):  # noqa: F811
    args = {"variable": "precipitation", "geometry": {"lat": 45.175, "lon": -69.3147}, "start": DAYS[0], "end": DAYS[1],
            "mode": "manual", "product": "GRIDMET_PRECIP", "session_id": "gb", "cache": False}
    with patch("aihydro_data._pipeline._fetch_one", side_effect=lambda spec, g, s, e, a, req, **kw: _result_for(spec, req)):
        r = mcp_call(args)
    assert r["product"] == "GRIDMET_PRECIP" and r["product_substituted"] is False


# ------------------------------------------------------------------ async runner
def _runner_cfg(**kw):
    return {"variable": "precipitation", "geometry": {"lat": 45.175, "lon": -69.3147}, "start": DAYS[0], "end": DAYS[1],
            "product": "GRIDMET_PRECIP", "aggregation": "basin_mean", "gauge_id": None, **kw}


def test_background_runner_pin_is_strict(world, tmp_path):  # noqa: F811
    from ai_hydro.mcp.runners.data_fetch_runner import DataFetchRunner
    from aihydro_data.exceptions import SourceUnavailable

    with patch("aihydro_data._pipeline._fetch_one", side_effect=_fail("GRIDMET_PRECIP")):
        with pytest.raises(SourceUnavailable):
            DataFetchRunner.__new__(DataFetchRunner).run_job(_runner_cfg(), tmp_path)


def test_background_runner_opt_in_fallback_echoes_the_substitution(world, tmp_path):  # noqa: F811
    from ai_hydro.mcp.runners.data_fetch_runner import DataFetchRunner

    with patch("aihydro_data._pipeline._fetch_one", side_effect=_fail("GRIDMET_PRECIP")):
        out = DataFetchRunner.__new__(DataFetchRunner).run_job(_runner_cfg(fallback=["DAYMET_PRECIP"], cache=False), tmp_path)
    assert out["product"] == "DAYMET_PRECIP"
    assert out["product_substituted"] is True and out["requested_product"] == "GRIDMET_PRECIP"


def test_background_tool_forwards_fallback_and_runner_pin_passes_fallback_empty(world, monkeypatch, tmp_path):  # noqa: F811
    import aihydro_data
    from ai_hydro.mcp import tools_data_async as tda
    from ai_hydro.mcp.runners.data_fetch_runner import DataFetchRunner

    started = {}
    monkeypatch.setattr(tda.jobs, "start_job", lambda **k: started.update(k) or {
        "job_id": "j", "artifact_dir": str(tmp_path), "log_path": "l", "started_at": "t"})
    fn = getattr(tda.data_fetch_background, "fn", tda.data_fetch_background)
    fn("precipitation", DAYS[0], DAYS[1], geometry={"lat": 45.175, "lon": -69.3147}, product="GRIDMET_PRECIP",
       fallback=["DAYMET_PRECIP"])
    assert started["config"]["fallback"] == ["DAYMET_PRECIP"]
    got = {}
    monkeypatch.setattr(aihydro_data, "fetch", lambda **k: got.update(k) or (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError):
        DataFetchRunner.__new__(DataFetchRunner).run_job(_runner_cfg(), tmp_path)
    assert got["fallback"] == [] and got["product"] == "GRIDMET_PRECIP"


# ------------------------------------------------------------------ sealed run record
def test_substituted_product_is_in_the_sealed_record(tmp_path, monkeypatch):
    import fastmcp
    from aihydro_core.records import RunRecord
    from ai_hydro.mcp import app
    from ai_hydro.session import chat_binding, store
    from ai_hydro.session.chat_binding import ChatBindingStore
    from ai_hydro.session.store import HydroSession

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    (tmp_path / "sessions").mkdir()
    HydroSession("sealed").save()
    server = fastmcp.FastMCP(name="sealed")
    server.add_middleware(app._ContextInjectionMiddleware())
    server.add_middleware(app.RunRecordMiddleware())

    @server.tool()
    def data_fetch(session_id: str, fallback: list[str] | None = None) -> dict:
        from ai_hydro.mcp.tools_data_fetch import data_fetch as real
        return real("precipitation", DAYS[0], DAYS[1], geometry={"lat": 45.175, "lon": -69.3147}, mode="manual",
                    product="GRIDMET_PRECIP", fallback=fallback, cache=False, session_id=session_id)

    async def go(args):
        async with Client(server) as c:
            return await c.call_tool("data_fetch", args, raise_on_error=False)

    with patch("aihydro_data._pipeline._fetch_one", side_effect=_fail("GRIDMET_PRECIP")):
        body = json.loads(asyncio.run(go({"session_id": "sealed", "fallback": ["DAYMET_PRECIP"]})).content[0].text)
    assert body["product_substituted"] is True
    log = store._run_log_read_all("sealed")
    recs = [r["record"] for r in log.values() if isinstance(r, dict) and r.get("record") and r["record"]["tool"] == "data_fetch"]
    assert len(recs) == 1
    extra = recs[0]["extra"]
    assert extra["product_substituted"] is True and extra["requested_product"] == "GRIDMET_PRECIP"
    assert extra["served_product"] == "DAYMET_PRECIP"
    assert RunRecord.from_dict(recs[0]).verify()
