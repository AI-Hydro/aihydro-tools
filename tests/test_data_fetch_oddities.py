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
