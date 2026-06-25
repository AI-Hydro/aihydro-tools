"""Vertical datum metadata for 3D inundation manifests."""
from __future__ import annotations

from ai_hydro.analysis.inundation_3d import (
    bench_terrain_vertical_offset,
    egm96_geoid_undulation_m,
    terrain_vertical_metadata,
)


def test_penobscot_geoid_offset_range():
    n = egm96_geoid_undulation_m(44.55, -68.0)
    assert -35.0 < n < -20.0


def test_terrain_vertical_metadata_conus_3dep():
    stack = {"dem_product": "3dep_1m", "crs": "EPSG:5070"}
    meta = terrain_vertical_metadata(stack, [-68.1, 44.5, -67.9, 44.6])
    assert meta["mesh_vertical_datum"] == "orthometric_navd88"
    assert meta["terrain_vertical_datum"] == "wgs84_ellipsoid_terrarium"
    assert meta["terrain_vertical_offset_m"] < 0


def test_terrain_vertical_metadata_ellipsoid_dem():
    stack = {"dem_product": "merit_hydro", "crs": "EPSG:4326"}
    meta = terrain_vertical_metadata(stack, [10.0, 44.0, 11.0, 45.0])
    assert meta["mesh_vertical_datum"] == "ellipsoid_wgs84"
    assert meta["terrain_vertical_offset_m"] == 0.0


def test_bench_terrain_vertical_offset():
    out = bench_terrain_vertical_offset()
    assert out["mesh_vertical_datum"] == "orthometric_navd88"
    assert out["offset_in_penobscot_range"] is True
