"""create_cn_grid seals the package's basin_statistics and one canonical stats_basis (DEMO-1 F15).

Offline: aihydro-watershed's grid computation is stubbed with a result in the
post-#29 shape (statistics.stats_basis, basin_statistics, quality_flags).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

_POLY = json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]})


def _pkg_result(*, basis: str = "basin_polygon", post_29: bool = True, flags=None) -> dict:
    res = {
        "statistics": {"cn_mean": 78.8, "cn_median": 79.0, "cn_min": 30.0, "cn_max": 98.0, "cn_std": 9.0,
                       "cn_p10": 66.0, "cn_p25": 72.0, "cn_p75": 85.0, "cn_p90": 89.0},
        "cn_zones": {"percent_low_cn": 10.0, "percent_medium_cn": 60.0, "percent_high_cn": 30.0},
        "lulc_stats": {"classes": [11, 41, 42], "counts": [5, 80, 15], "percentages": [5.0, 80.0, 15.0],
                       "class_fractions": {"41": 0.8, "42": 0.2}, "open_water_fraction": 0.0,
                       "fraction_basis": "basin_polygon"},
        "soil_stats": {"hsg_method": "ssurgo_recorded", "soil_group_percentages": {"B": 100.0},
                       "hsg_coverage": 0.97, "cn_mean_drained": 78.8, "cn_mean_undrained": 83.1,
                       "cn_mean_basis": "basin_polygon"},
        "watershed_info": {"area_km2": 765.0},
        "data_provenance": {"landcover_product": "NLCD", "soil_product": "SSURGO",
                            "cn_stats_basis": basis, "lulc_fraction_basis": "basin_polygon"},
        "file_paths": {},
    }
    if post_29:
        res["statistics"]["stats_basis"] = basis
        res["basin_statistics"] = {"cn_mean_basin": 78.8, "cn_median_basin": 79.0, "cn_min_basin": 30.0,
                                   "cn_max_basin": 98.0, "cn_cells_basin": 850000, "stats_basis": basis}
        res["quality_flags"] = list(flags or [])
        res["warnings"] = ["stub warning"] if flags else []
    return res


async def _run(tmp_path, pkg_result: dict) -> dict:
    from ai_hydro.session import HydroSession

    with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
         patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
         patch("ai_hydro.mcp.tools_analysis._resolve_session", return_value="cn-stats"), \
         patch("ai_hydro.analysis.curve_number.create_curve_number_grid_from_geometry",
               lambda **k: pkg_result):
        HydroSession("cn-stats").save()
        from ai_hydro.mcp.tools_analysis import create_cn_grid

        return await create_cn_grid(session_id="cn-stats", geometry_geojson=_POLY, create_map=False)


@pytest.mark.asyncio
async def test_basin_statistics_and_canonical_basis_are_sealed(tmp_path):
    out = await _run(tmp_path, _pkg_result())
    d = out["data"]
    assert d["basin_statistics"]["cn_mean_basin"] == 78.8
    assert d["basin_statistics"]["cn_cells_basin"] == 850000
    assert d["stats_basis"] == "basin_polygon"
    assert d["basin_statistics"]["stats_basis"] == d["stats_basis"]
    assert out["meta"]["data_provenance"]["cn_stats_basis"] == d["stats_basis"]
    assert d["cn_mean"] == 78.8


@pytest.mark.asyncio
async def test_raster_extent_fallback_is_carried_with_its_flags(tmp_path):
    out = await _run(tmp_path, _pkg_result(basis="raster_extent", flags=["basin_cn_stats_unavailable"]))
    d = out["data"]
    assert d["stats_basis"] == "raster_extent"
    assert d["basin_statistics"]["stats_basis"] == "raster_extent"
    assert d["quality_flags"] == ["basin_cn_stats_unavailable"]
    assert d["quality_warnings"] == ["stub warning"]
    assert d["field_bases"]["basin_statistics"] == "raster_extent"


@pytest.mark.asyncio
async def test_every_area_field_names_its_basis(tmp_path):
    d = (await _run(tmp_path, _pkg_result()))["data"]
    fb = d["field_bases"]
    # Class codes and soil shares are over the raster (the basin's bounding box).
    assert fb["lulc_classes"] == "raster_extent"
    assert fb["soil_group_percentages / hsg_coverage / kw_mean / pct_dual_hsg"] == "raster_extent"
    assert fb["lulc_class_fractions / open_water_fraction"] == "basin_polygon"
    assert fb["cn_mean_drained / cn_mean_undrained"] == "basin_polygon"
    assert d["lulc_class_fractions"] == {"41": 0.8, "42": 0.2}
    assert all(v in {"basin_polygon", "raster_extent", "unknown"} for v in fb.values())


@pytest.mark.asyncio
async def test_older_package_without_a_basis_is_not_labelled_polygon(tmp_path):
    pkg = _pkg_result(post_29=False)
    pkg["data_provenance"].pop("cn_stats_basis")
    pkg["data_provenance"].pop("lulc_fraction_basis")
    pkg["lulc_stats"].pop("fraction_basis")
    pkg["soil_stats"].pop("cn_mean_basis")
    d = (await _run(tmp_path, pkg))["data"]
    assert d["stats_basis"] == "unknown"
    assert d["basin_statistics"] == {}
    assert d["field_bases"]["lulc_class_fractions / open_water_fraction"] == "unknown"
    assert d["field_bases"]["cn_mean_drained / cn_mean_undrained"] == "unknown"


@pytest.mark.asyncio
async def test_released_0_4_5_shape_headline_basis_is_not_taken_from_the_basin_block(tmp_path):
    """0.4.5: statistics.stats_basis = raster_extent while the basin block and provenance say basin_polygon."""
    r = _pkg_result(basis="basin_polygon")
    r["statistics"]["stats_basis"] = "raster_extent"
    r["basin_statistics"]["stats_basis"] = "basin_polygon"
    r["data_provenance"]["cn_stats_basis"] = "basin_polygon"
    r["soil_stats"].pop("cn_mean_basis")
    d = (await _run(tmp_path, r))["data"]
    assert d["stats_basis"] == "raster_extent"
    assert d["basin_statistics"]["stats_basis"] == "basin_polygon"
    assert d["field_bases"]["lulc_class_fractions / open_water_fraction"] == "basin_polygon"
    assert d["field_bases"]["cn_mean_drained / cn_mean_undrained"] == "unknown"


@pytest.mark.asyncio
async def test_headline_basis_is_unknown_when_only_the_basin_block_states_one(tmp_path):
    r = _pkg_result()
    r["statistics"].pop("stats_basis")
    d = (await _run(tmp_path, r))["data"]
    assert d["stats_basis"] == "unknown"
