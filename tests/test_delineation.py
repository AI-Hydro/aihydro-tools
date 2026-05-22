"""Tests for global pour-point watershed delineation."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point, box

from ai_hydro.analysis.delineation.router import _should_escalate
from ai_hydro.analysis.delineation.types import FastDelineationResult, area_km2
from ai_hydro.analysis.delineation.utils import lonlat_to_utm_epsg, square_bbox_proj
from ai_hydro.data.merit_manager import MeritDataManager


def test_lonlat_to_utm_epsg_northern():
    assert lonlat_to_utm_epsg(-86.9, 40.4).startswith("EPSG:326")


def test_square_bbox_proj_area_reasonable():
    poly = square_bbox_proj(40.0, -86.0, 50.0, "EPSG:32616")
    assert poly.area > 0


def test_area_km2_small_polygon():
    gdf = gpd.GeoDataFrame(geometry=[box(-86.1, 40.0, -86.0, 40.1)], crs=4326)
    a = area_km2(gdf)
    assert 50 < a < 150


def test_should_escalate_tiny_area():
    fast = FastDelineationResult(
        gdf=gpd.GeoDataFrame(),
        area_km2=0.5,
        scout_box_maxed=False,
        outlet_lat=40.0,
        outlet_lon=-86.0,
        merit_snap_distance_m=100.0,
        pfaf_code="05",
    )
    assert _should_escalate(fast, expected_area_km2=None) is not None


def test_should_escalate_area_mismatch():
    fast = FastDelineationResult(
        gdf=gpd.GeoDataFrame(),
        area_km2=100.0,
        scout_box_maxed=False,
        outlet_lat=40.0,
        outlet_lon=-86.0,
        merit_snap_distance_m=100.0,
        pfaf_code="05",
    )
    reason = _should_escalate(fast, expected_area_km2=500.0)
    assert reason is not None
    assert "differs" in reason


def test_snap_outlet_nldi_conus():
    from ai_hydro.analysis.delineation.nldi_point import snap_outlet_nldi

    lat, lon = 35.03, -120.48
    lat2, lon2, ok = snap_outlet_nldi(lat, lon)
    assert ok
    assert abs(lat2 - lat) + abs(lon2 - lon) > 0


def test_merit_manager_resolve_basin_field(tmp_path):
    import geopandas as gpd
    from shapely.geometry import box

    mgr = MeritDataManager(root=tmp_path)
    shp_dir = tmp_path / "shp" / "basins_level2"
    shp_dir.mkdir(parents=True)
    gdf = gpd.GeoDataFrame({"BASIN": [77]}, geometry=[box(-121, 34, -119, 36)], crs=4326)
    gdf.to_file(shp_dir / "merit_hydro_vect_level2.shp")
    assert mgr.resolve_pfaf_code(35.03, -120.48) == "77"


def test_merit_manager_resolve_requires_level2(tmp_path):
    mgr = MeritDataManager(root=tmp_path)
    with pytest.raises(FileNotFoundError):
        mgr.resolve_pfaf_code(40.0, -86.0)


@patch("ai_hydro.analysis.delineation.pysheds_pipeline.delineate_fast")
@patch("ai_hydro.analysis.delineation.nldi_point.delineate_nldi_at_point")
def test_auto_conus_uses_nldi_quick_when_area_in_range(mock_nldi, mock_fast):
    """CONUS auto without expected_area should use fast NLDI, not cloud DEM."""
    from ai_hydro.core import HydroMeta, HydroResult

    geom = box(-96.5, 40.5, -96.2, 40.9)
    mock_nldi.return_value = HydroResult(
        data={
            "geometry_geojson": json.loads(gpd.GeoDataFrame(geometry=[geom], crs=4326).to_json())[
                "features"
            ][0],
            "area_km2": 420.0,
            "method_used": "nldi_comid",
            "comid": 1,
        },
        meta=HydroMeta(tool="test", version="0", params={}, sources=[]),
    )
    from ai_hydro.analysis.delineation.router import delineate_from_point

    result = delineate_from_point(40.71829, -96.41265, method="auto")
    mock_nldi.assert_called_once()
    mock_fast.assert_not_called()
    assert result.data["method_used"] == "nldi_comid"
    assert result.data["area_km2"] == 420.0


@patch("ai_hydro.analysis.delineation.pysheds_pipeline.delineate_fast")
@patch("ai_hydro.analysis.delineation.nldi_point.delineate_nldi_at_point")
def test_auto_conus_escalates_when_nldi_area_out_of_range(mock_nldi, mock_fast):
    geom = box(-96.5, 40.5, -96.2, 40.9)
    gdf = gpd.GeoDataFrame(geometry=[geom], crs=4326)
    from ai_hydro.core import HydroMeta, HydroResult

    mock_nldi.return_value = HydroResult(
        data={"area_km2": 60_000.0, "method_used": "nldi_comid"},
        meta=HydroMeta(tool="test", version="0", params={}, sources=[]),
    )
    mock_fast.return_value = FastDelineationResult(
        gdf=gdf,
        area_km2=area_km2(gdf),
        scout_box_maxed=False,
        outlet_lat=40.71829,
        outlet_lon=-96.41265,
        merit_snap_distance_m=50.0,
        pfaf_code="10",
    )
    from ai_hydro.analysis.delineation.router import delineate_from_point

    result = delineate_from_point(40.71829, -96.41265, method="auto")
    mock_fast.assert_called_once()
    assert result.data["method_used"] == "fast"


@patch("ai_hydro.analysis.delineation.pysheds_pipeline.delineate_fast")
def test_delineate_from_point_fast_mock(mock_fast):
    geom = box(-86.2, 40.3, -86.0, 40.5)
    gdf = gpd.GeoDataFrame(geometry=[geom], crs=4326)
    mock_fast.return_value = FastDelineationResult(
        gdf=gdf,
        area_km2=area_km2(gdf),
        scout_box_maxed=False,
        outlet_lat=40.4,
        outlet_lon=-86.1,
        merit_snap_distance_m=50.0,
        pfaf_code="05",
    )
    from ai_hydro.analysis.delineation.router import delineate_from_point

    result = delineate_from_point(40.4, -86.1, method="fast")
    assert result.data["method_used"] == "fast"
    assert result.data["area_km2"] > 0
    assert result.data["geometry_geojson"]["type"] == "Feature"


def test_merit_ensure_basin_mcp_smoke(tmp_path):
    from ai_hydro.mcp.tools_analysis import merit_ensure_basin

    with patch("ai_hydro.data.merit_manager.MeritDataManager") as MockMgr:
        inst = MockMgr.return_value
        from ai_hydro.data.merit_manager import BasinEnsureStatus

        inst.ensure_basin.return_value = BasinEnsureStatus(
            pfaf_code="21",
            level2_ready=False,
            rivers_ready=False,
            catchments_ready=False,
            flowdir_ready=False,
            message="level-2 index missing",
        )
        inst.root = tmp_path
        inst.delineator_ready.return_value = False
        out = merit_ensure_basin(41.0, -7.0, download=False)
    assert out["data"]["pfaf_code"] == "21"


def test_delineate_watershed_from_point_invalid_method():
    from ai_hydro.mcp.tools_analysis import delineate_watershed_from_point

    out = delineate_watershed_from_point("test-session", 40.0, -86.0, method="invalid")
    assert "error" in out or "message" in str(out).lower()


@pytest.mark.live
def test_delineate_from_point_iceland_fast():
    """Live fast-tier test near Iceland (requires network + delineation extras)."""
    pytest.importorskip("planetary_computer")
    from ai_hydro.analysis.delineation.router import delineate_from_point

    result = delineate_from_point(64.15, -21.95, method="fast", verbose=True)
    assert result.data["area_km2"] > 1
