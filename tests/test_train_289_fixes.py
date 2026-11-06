"""2.8.9: watershed 0.4.0 integration fixes (slope fail-closed, relief flag, delineation arguments)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def test_slope_unavailable_becomes_a_structured_error():
    from ai_hydro.analysis._dem import SlopeUnavailableError

    from ai_hydro.mcp.helpers import _tool_error_to_dict

    env = _tool_error_to_dict(SlopeUnavailableError("DEM has no CRS; refusing to assume a metric grid"))
    assert env["error"] is True
    assert env["code"] == "SLOPE_UNAVAILABLE"
    assert env["recovery"]


def test_hand_stack_fails_closed_when_reprojection_fails():
    from ai_hydro.analysis import inundation as inu

    class _Rio:
        def reproject(self, *_a, **_k):
            raise RuntimeError("no transform")

    class _Dem:
        rio = _Rio()
        ndim = 2

    geom = type("G", (), {"x": 10.0, "y": 50.0, "centroid": None})()
    geom.centroid = geom
    with patch("ai_hydro.analysis._dem.fetch_dem", return_value=(_Dem(), "p", "s")), patch(
        "ai_hydro.analysis._dem._geom_in_conus", return_value=False
    ):
        from ai_hydro.core import ToolError

        with pytest.raises(ToolError) as ei:
            inu._prepare_hand_stack(geom)
    assert ei.value.code == "DEM_REPROJECTION_FAILED"


# ---------------------------------------------------------------------------------------------
# UTM zone wrap, called through the real _prepare_hand_stack with a stubbed DEM
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("lon,lat,epsg", [(180.0, 10.0, 32601), (179.9, 10.0, 32660), (-180.0, -10.0, 32701), (0.0, 5.0, 32631)])
def test_hand_stack_utm_zone_wraps(lon, lat, epsg):
    from ai_hydro.analysis import inundation as inu
    from ai_hydro.core import ToolError

    seen: list[str] = []

    class _Rio:
        def reproject(self, crs, *_a, **_k):
            seen.append(crs)
            raise RuntimeError("stop after recording the target CRS")

    class _Dem:
        rio = _Rio()
        ndim = 2

    geom = type("G", (), {"x": lon, "y": lat})()
    geom.centroid = geom
    with patch("ai_hydro.analysis._dem.fetch_dem", return_value=(_Dem(), "p", "s")), patch(
        "ai_hydro.analysis._dem._geom_in_conus", return_value=False
    ):
        with pytest.raises(ToolError):
            inu._prepare_hand_stack(geom)
    assert seen == [f"EPSG:{epsg}"]


# ---------------------------------------------------------------------------------------------
# delineate_watershed_from_point: alias, expansion flag, ambiguity note; gauge tool untouched
# ---------------------------------------------------------------------------------------------
PB = {"type": "Polygon", "coordinates": [[[-100, 35], [-99, 35], [-99, 36], [-100, 36], [-100, 35]]]}


@pytest.fixture
def point_router(monkeypatch):
    from ai_hydro.core import HydroMeta, HydroResult
    import ai_hydro.analysis.delineation as dl
    import ai_hydro.mcp.map_events as me

    calls: list[dict] = []
    extra: dict = {}

    def fake(lat, lon, **kw):
        calls.append(kw)
        data = {"geometry_geojson": PB, "area_km2": 50.0, "method_used": "stub", "quality_flags": [], **extra}
        return HydroResult(data=data, meta=HydroMeta(tool="x", version="0"))

    monkeypatch.setattr(dl, "delineate_from_point", fake)
    monkeypatch.setattr(me, "push_layer", lambda *a, **k: True)
    monkeypatch.setattr(me, "push_gauge_point", lambda *a, **k: True)
    return calls, extra


def _fn(tool):
    return getattr(tool, "fn", tool)


def test_merit_hybrid_is_an_alias_of_merit_basins(point_router):
    from ai_hydro.mcp import tools_analysis as ta

    calls, _ = point_router
    out = _fn(ta.delineate_watershed_from_point)(lat=35.5, lon=-99.5, method="merit_hybrid", session_id="t289-alias")
    assert not out.get("error"), out
    assert calls[0]["method"] == "merit_basins"


def test_allow_downstream_expansion_passed_only_when_true(point_router):
    from ai_hydro.mcp import tools_analysis as ta

    calls, _ = point_router
    _fn(ta.delineate_watershed_from_point)(lat=35.5, lon=-99.5, session_id="t289-exp1")
    _fn(ta.delineate_watershed_from_point)(lat=35.5, lon=-99.5, session_id="t289-exp2", allow_downstream_expansion=True)
    assert "allow_downstream_expansion" not in calls[0]
    assert calls[1]["allow_downstream_expansion"] is True


def test_outlet_ambiguity_is_surfaced_in_the_note(point_router):
    from ai_hydro.mcp import tools_analysis as ta

    calls, extra = point_router
    extra["outlet_ambiguity"] = {"candidates": [{"comid": 1, "area_km2": 12.0}, {"comid": 2, "area_km2": 900.0}]}
    out = _fn(ta.delineate_watershed_from_point)(lat=35.5, lon=-99.5, session_id="t289-amb")
    assert out["data"]["outlet_ambiguity"]["candidates"]
    assert "NLDI_OUTLET_AMBIGUOUS" in out["_note"] and "allow_downstream_expansion" in out["_note"]


def test_gauge_delineation_has_no_ambiguity_note_and_does_not_error(monkeypatch):
    from ai_hydro.core import HydroMeta, HydroResult
    import ai_hydro.analysis.watershed as ws
    import ai_hydro.mcp.map_events as me
    from ai_hydro.mcp import tools_analysis as ta

    monkeypatch.setattr(me, "push_layer", lambda *a, **k: True)
    monkeypatch.setattr(me, "push_gauge_point", lambda *a, **k: True)
    monkeypatch.setattr(ws, "delineate_watershed", lambda gauge_id: HydroResult(
        data={"geometry_geojson": PB, "area_km2": 50.0, "gauge_id": gauge_id, "gauge_lat": 35.5, "gauge_lon": -99.5,
              "gauge_name": "G", "outlet_ambiguity": {"candidates": []}},
        meta=HydroMeta(tool="x", version="0")))
    out = _fn(ta.delineate_watershed)(gauge_id="01013500", session_id="t289-gauge")
    assert not out.get("error"), out
    assert "NLDI_OUTLET_AMBIGUOUS" not in json.dumps(out.get("_note", ""))


# ---------------------------------------------------------------------------------------------
# relief_flag pass-through; compute_twi re-raise; hybrid-served record
# ---------------------------------------------------------------------------------------------
def _session_with_basin(sid):
    from ai_hydro.session import HydroSession
    from ai_hydro.session.store import _SESSIONS_DIR

    s = HydroSession.load(sid)
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    p = _SESSIONS_DIR / f"{sid}.geojson"
    p.write_text(json.dumps(PB))
    s.set("watershed", {"data": {"area_km2": 50.0, "geometry_geojson_path": str(p)}, "meta": {}})
    s.save()


def test_relief_flag_passes_through_and_is_flagged(monkeypatch):
    from ai_hydro.core import HydroMeta, HydroResult
    import ai_hydro.analysis.geomorphic as gm
    from ai_hydro.mcp import tools_analysis as ta

    _session_with_basin("t289-relief")
    flag = "relief_unavailable:SlopeUnavailableError: DEM has no CRS"
    monkeypatch.setattr(gm, "extract_geomorphic_parameters_result", lambda **kw: HydroResult(
        data={"DA_km2": 50.0, "H_m": None, "relief_flag": flag, "_units": {}}, meta=HydroMeta(tool="x", version="0")))
    out = _fn(ta.extract_geomorphic_parameters)(session_id="t289-relief")
    assert not out.get("error"), out
    assert out["data"]["relief_flag"] == flag
    assert "RELIEF_UNAVAILABLE" in out["data"]["quality_flags"]


def test_compute_twi_reraises_slope_unavailable_instead_of_retrying_stats_only(monkeypatch):
    import asyncio

    from ai_hydro.analysis._dem import SlopeUnavailableError
    import ai_hydro.analysis.twi as twi
    from ai_hydro.mcp import tools_analysis as ta

    _session_with_basin("t289-twi")
    stats_calls: list = []

    def boom(*a, **k):
        raise SlopeUnavailableError("DEM has no CRS; refusing to assume a metric grid")

    def stats(*a, **k):
        stats_calls.append(1)
        raise AssertionError("stats-only fallback must not run")

    monkeypatch.setattr(twi, "compute_twi", boom)
    monkeypatch.setattr(twi, "compute_twi_result", stats)
    out = asyncio.run(_fn(ta.compute_twi)(session_id="t289-twi", create_map=True))
    assert out.get("error") and out["code"] == "SLOPE_UNAVAILABLE"
    assert not stats_calls


def test_hybrid_served_result_gets_a_record_trail():
    from ai_hydro.session import run_records as rr

    result = {
        "data": {"method_used": "merit_basins_hybrid", "quality_flags": ["HYBRID_ROUTING_USED", "MERIT_VERSION_MIX"],
                 "fallback_history": [
                     {"method": "merit_gee", "outcome": "failed", "reason": "GEE unavailable"},
                     {"method": "merit_basins_hybrid", "outcome": "succeeded", "reason": "global default tier"}]},
    }
    t = rr.attempt_trail(result)
    assert t["source_used"] == "merit_basins_hybrid"
    assert [a["outcome"] for a in t["attempts"]] == ["failed", "succeeded"]
    assert t["limitations"] == ["HYBRID_ROUTING_USED", "MERIT_VERSION_MIX"]
