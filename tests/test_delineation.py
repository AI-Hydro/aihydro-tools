"""Tests for global pour-point watershed delineation."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point, box

from ai_hydro.analysis.delineation.router import _should_escalate
from ai_hydro.analysis.delineation.merit_flowdir_pipeline import (
    MERIT_GEE_DATASET,
    MERIT_SAFE_ENVELOPE_VERSION,
    MeritFlowdirResult,
    MeritRasterBundle,
    MeritSnap,
    MeritSnapReference,
    basin_touches_edge,
    bbox_from_point,
    classify_safe_envelope,
    local_merit_flowdir_pyflwdir,
    merit_build_offline_snap_cache,
    merit_basins_hybrid_delineate,
    merit_dir_to_pyflwdir,
    merit_traverse_upstream_catchments,
    snap_outlet_on_merit,
    validate_area,
    validate_snap,
)
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


def test_merit_dir_to_pyflwdir_masks_invalid_values():
    arr = np.array([[1, 2, 4, 8], [16, 32, 64, 128], [-1, 999, np.nan, 0]])
    out = merit_dir_to_pyflwdir(arr)
    assert out.dtype == np.uint8
    assert out[0, 0] == 1
    assert out[1, 3] == 128
    assert out[2, 0] == 247
    assert out[2, 1] == 247
    assert out[2, 2] == 247
    assert out[2, 3] == 0


def test_merit_snap_prefers_expected_upa_candidate():
    from affine import Affine

    upa = np.ones((5, 5), dtype=float)
    upa[2, 2] = 20.0
    upa[2, 3] = 80.0
    wth = np.zeros((5, 5), dtype=float)
    wth[2, 2] = 5.0
    wth[2, 3] = 5.0
    transform = Affine(0.001, 0, -0.0025, 0, -0.001, 0.0025)

    snap = snap_outlet_on_merit(
        0.0,
        0.0,
        upa=upa,
        wth=wth,
        transform=transform,
        expected_area_km2=80.0,
        search_radius_m=1000.0,
    )
    assert snap.snapped_upa_km2 == 80.0
    assert snap.snap_quality == "area_targeted"


def test_merit_edge_and_area_validation_flags():
    mask = np.zeros((5, 5), dtype=bool)
    mask[0, 2] = True
    assert basin_touches_edge(mask)

    validation, flags = validate_area(
        40.0,
        snapped_upa_km2=100.0,
        expected_area_km2=100.0,
        touches_edge=True,
    )
    assert not validation["ok"]
    assert "BASIN_TOUCHES_WINDOW_EDGE" in flags
    assert "MERIT_UPA_AREA_MISMATCH" in flags
    assert "AREA_DIFFERS_FROM_EXPECTED" in flags


def test_merit_snap_validation_flags_far_or_area_mismatch():
    from ai_hydro.analysis.delineation.merit_flowdir_pipeline import MeritSnap

    snap = MeritSnap(
        lat=0.0,
        lon=0.0,
        row=0,
        col=0,
        distance_m=12_000.0,
        snapped_upa_km2=50.0,
        snap_quality="area_targeted_far",
    )
    validation, flags = validate_snap(snap, expected_area_km2=100.0)
    assert not validation["ok"]
    assert "OUTLET_SNAP_FAR" in flags
    assert "SNAP_UPA_DIFFERS_FROM_EXPECTED" in flags


def test_bbox_from_point_is_centered():
    bbox = bbox_from_point(0.0, 10.0, 10.0)
    assert bbox[0] < 10.0 < bbox[2]
    assert bbox[1] < 0.0 < bbox[3]


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


def test_routing_region_cache_flowdir_only_ready(tmp_path):
    mgr = MeritDataManager(root=tmp_path)
    flowdir_dir = tmp_path / "raster" / "flowdir_basins"
    flowdir_dir.mkdir(parents=True)
    (flowdir_dir / "flowdir77.tif").write_bytes(b"placeholder")

    status = mgr.ensure_routing_region(pfaf_region="77", acquisition_policy="check_only")
    assert status.flowdir_ready
    assert not status.accum_ready
    assert not status.acquisition_required
    assert status.required_assets == ("flowdir",)


def test_basins_region_cache_requires_catchments_without_flowdir_or_accum(tmp_path):
    mgr = MeritDataManager(root=tmp_path)
    river_dir = tmp_path / "shp" / "merit_rivers"
    river_dir.mkdir(parents=True)
    (river_dir / "riv_pfaf_45_MERIT_Hydro_v07_Basins_v01.shp").write_bytes(b"river")

    status = mgr.ensure_basins_region("45", acquisition_policy="check_only")
    assert status.rivers_ready
    assert not status.catchments_ready
    assert status.acquisition_required
    assert status.license
    assert status.citation
    assert not (tmp_path / "raster" / "flowdir_basins").exists()
    assert not (tmp_path / "raster" / "accum_basins").exists()


def test_basins_region_check_only_never_downloads(tmp_path):
    mgr = MeritDataManager(root=tmp_path)
    with patch.object(mgr, "_try_download", side_effect=AssertionError("download not allowed")):
        status = mgr.ensure_basins_region("45", acquisition_policy="check_only")
    assert not status.catchments_ready
    assert status.acquisition_policy == "check_only"


def test_safe_envelope_policy_promotes_overflow():
    status, flags = classify_safe_envelope(
        final_window_cell_count=60_000_001,
        rss_delta_mb=100.0,
        window_complete=True,
        scientific_mode=False,
    )
    assert status == "hybrid_required"
    assert "HYBRID_ROUTING_REQUIRED" in flags
    assert "HYBRID_ROUTING_RECOMMENDED" in flags

    status, flags = classify_safe_envelope(
        final_window_cell_count=60_000_001,
        rss_delta_mb=100.0,
        window_complete=True,
        scientific_mode=True,
    )
    assert status == "scientific_allowed"
    assert flags == ["HYBRID_ROUTING_RECOMMENDED"]

    status, flags = classify_safe_envelope(
        final_window_cell_count=10_000,
        rss_delta_mb=10.0,
        window_complete=False,
        scientific_mode=True,
    )
    assert status == "hybrid_required"
    assert "HYBRID_ROUTING_REQUIRED" in flags


def test_local_merit_flowdir_only_does_not_compute_upstream_area(tmp_path):
    pytest.importorskip("pyflwdir")
    rasterio = pytest.importorskip("rasterio")
    from affine import Affine

    direction = np.zeros((5, 5), dtype=np.uint8)
    flowdir_path = tmp_path / "flowdir77.tif"
    transform = Affine(0.01, 0, -0.025, 0, -0.01, 0.025)
    with rasterio.open(
        flowdir_path,
        "w",
        driver="GTiff",
        height=5,
        width=5,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
        nodata=247,
    ) as dst:
        dst.write(direction, 1)

    class FakeStatus:
        pfaf_code = "77"

    snap_reference = MeritSnapReference(
        snap=MeritSnap(
            lat=0.0,
            lon=0.0,
            row=2,
            col=2,
            distance_m=10.0,
            snapped_upa_km2=25.0,
            snap_quality="gee_official",
        ),
        source="gee",
        cache_key="snap",
        bbox=(-0.01, -0.01, 0.01, 0.01),
        official_merit_upa_km2=25.0,
    )

    with patch("aihydro_watershed.merit.merit_manager.MeritDataManager") as MockMgr:
        inst = MockMgr.return_value
        inst.ensure_basin.return_value = FakeStatus()
        inst.flowdir_path.return_value = flowdir_path
        import pyflwdir

        with patch.object(
            pyflwdir.FlwdirRaster,
            "upstream_area",
            side_effect=AssertionError("upstream_area should be opt-in"),
        ):
            result = local_merit_flowdir_pyflwdir(
                0.0,
                0.0,
                expected_area_km2=25.0,
                snap_reference=snap_reference,
            )

    assert result.area_km2 > 0
    assert result.local_upstream_area_km2 is None
    assert result.official_merit_upa_km2 == 25.0
    assert result.regional_flowdir_cached is True
    assert "gee_official_upa" in (result.validation_sources or [])


def test_local_merit_interactive_window_limit_promotes_to_hybrid(tmp_path, monkeypatch):
    pytest.importorskip("pyflwdir")
    rasterio = pytest.importorskip("rasterio")
    from affine import Affine

    flowdir_path = tmp_path / "flowdir77.tif"
    transform = Affine(0.001, 0, -0.05, 0, -0.001, 0.05)
    with rasterio.open(
        flowdir_path,
        "w",
        driver="GTiff",
        height=100,
        width=100,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
        nodata=247,
    ) as dst:
        dst.write(np.zeros((100, 100), dtype=np.uint8), 1)

    class FakeStatus:
        pfaf_code = "77"

    snap_reference = MeritSnapReference(
        snap=MeritSnap(
            lat=0.0,
            lon=0.0,
            row=50,
            col=50,
            distance_m=10.0,
            snapped_upa_km2=25.0,
            snap_quality="gee_official",
        ),
        source="gee",
        cache_key="snap",
        bbox=(-0.01, -0.01, 0.01, 0.01),
        official_merit_upa_km2=25.0,
    )
    monkeypatch.setattr(
        "aihydro_watershed.delineation.merit_flowdir_pipeline.MERIT_INTERACTIVE_MAX_WINDOW_CELLS",
        10,
    )
    with patch("aihydro_watershed.merit.merit_manager.MeritDataManager") as MockMgr:
        inst = MockMgr.return_value
        inst.ensure_basin.return_value = FakeStatus()
        inst.flowdir_path.return_value = flowdir_path
        with pytest.raises(RuntimeError, match="HYBRID_ROUTING_REQUIRED"):
            local_merit_flowdir_pyflwdir(
                0.0,
                0.0,
                expected_area_km2=25.0,
                snap_reference=snap_reference,
            )


def test_local_merit_scientific_mode_allows_caution_band(tmp_path, monkeypatch):
    pytest.importorskip("pyflwdir")
    rasterio = pytest.importorskip("rasterio")
    from affine import Affine

    flowdir_path = tmp_path / "flowdir77.tif"
    transform = Affine(0.01, 0, -0.025, 0, -0.01, 0.025)
    with rasterio.open(
        flowdir_path,
        "w",
        driver="GTiff",
        height=5,
        width=5,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
        nodata=247,
    ) as dst:
        dst.write(np.zeros((5, 5), dtype=np.uint8), 1)

    class FakeStatus:
        pfaf_code = "77"

    snap_reference = MeritSnapReference(
        snap=MeritSnap(
            lat=0.0,
            lon=0.0,
            row=2,
            col=2,
            distance_m=10.0,
            snapped_upa_km2=25.0,
            snap_quality="gee_official",
        ),
        source="gee",
        cache_key="snap",
        bbox=(-0.01, -0.01, 0.01, 0.01),
        official_merit_upa_km2=25.0,
    )
    monkeypatch.setattr(
        "aihydro_watershed.delineation.merit_flowdir_pipeline.MERIT_INTERACTIVE_MAX_WINDOW_CELLS",
        10,
    )
    monkeypatch.setattr(
        "aihydro_watershed.delineation.merit_flowdir_pipeline.MERIT_SCIENTIFIC_MAX_WINDOW_CELLS",
        100,
    )
    with patch("aihydro_watershed.merit.merit_manager.MeritDataManager") as MockMgr:
        inst = MockMgr.return_value
        inst.ensure_basin.return_value = FakeStatus()
        inst.flowdir_path.return_value = flowdir_path
        result = local_merit_flowdir_pyflwdir(
            0.0,
            0.0,
            expected_area_km2=25.0,
            snap_reference=snap_reference,
            scientific_mode=True,
        )

    assert "HYBRID_ROUTING_RECOMMENDED" in result.quality_flags
    assert "HYBRID_ROUTING_REQUIRED" not in result.quality_flags
    assert result.safe_envelope_version == MERIT_SAFE_ENVELOPE_VERSION


def test_merit_basins_topology_traversal_synthetic():
    gdf = gpd.GeoDataFrame(
        {
            "_aihydro_catchment_id": ["1", "2", "3", "4"],
            "_aihydro_downstream_id": ["0", "1", "1", "2"],
        },
        geometry=[
            box(0, 0, 1, 1),
            box(0, 1, 1, 2),
            box(1, 1, 2, 2),
            box(0, 2, 1, 3),
        ],
        crs=4326,
    )
    upstream = merit_traverse_upstream_catchments("1", gdf)
    assert set(upstream) == {"1", "2", "3", "4"}
    assert upstream[0] == "1"


def test_merit_load_catchment_topology_merges_river_topology(tmp_path):
    catch_dir = tmp_path / "shp" / "merit_catchments"
    river_dir = tmp_path / "shp" / "merit_rivers"
    catch_dir.mkdir(parents=True)
    river_dir.mkdir(parents=True)
    catch = gpd.GeoDataFrame(
        {"COMID": [1, 2], "unitarea": [1.0, 1.0]},
        geometry=[box(0, 0, 1, 1), box(0, 1, 1, 2)],
        crs=4326,
    )
    rivers = gpd.GeoDataFrame(
        {"COMID": [1, 2], "NextDownID": [0, 1], "uparea": [2.0, 1.0]},
        geometry=[Point(0.5, 0.5), Point(0.5, 1.5)],
        crs=4326,
    )
    catch.to_file(catch_dir / "cat_pfaf_77_MERIT_Hydro_v07_Basins_v01.shp")
    rivers.to_file(river_dir / "riv_pfaf_77_MERIT_Hydro_v07_Basins_v01.shp")

    with patch("aihydro_watershed.merit.merit_manager.MeritDataManager") as MockMgr:
        inst = MockMgr.return_value
        inst.catchment_shapefile_path.return_value = (
            catch_dir / "cat_pfaf_77_MERIT_Hydro_v07_Basins_v01.shp"
        )
        inst.river_shapefile_path.return_value = (
            river_dir / "riv_pfaf_77_MERIT_Hydro_v07_Basins_v01.shp"
        )
        from ai_hydro.analysis.delineation.merit_flowdir_pipeline import (
            merit_load_catchment_topology,
        )

        topology = merit_load_catchment_topology("77")

    assert "_aihydro_downstream_id" in topology.columns
    assert topology.loc[topology["_aihydro_catchment_id"] == "2", "_aihydro_downstream_id"].iloc[0] == "1"
    assert "uparea" in topology.columns


@patch("aihydro_watershed.delineation.merit_flowdir_pipeline._refine_terminal_catchment_with_flowdir")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_load_catchment_topology")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_check_routing_region_cache")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_check_basins_region_cache")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_resolve_pfaf_region")
def test_merit_basins_hybrid_assembles_topology_and_refines_terminal(
    mock_pfaf,
    mock_basins_cache,
    mock_routing_cache,
    mock_load,
    mock_refine,
):
    catchments = gpd.GeoDataFrame(
        {
            "_aihydro_catchment_id": ["1", "2", "3"],
            "_aihydro_downstream_id": ["0", "1", "1"],
        },
        geometry=[
            box(0, 0, 1, 1),
            box(0, 1, 1, 2),
            box(1, 1, 2, 2),
        ],
        crs=4326,
    )
    local_gdf = gpd.GeoDataFrame(geometry=[box(0.25, 0.25, 0.75, 0.75)], crs=4326)
    mock_pfaf.return_value = "77"
    mock_basins_cache.return_value = {"catchments_ready": True}
    mock_routing_cache.return_value = {"flowdir_ready": True}
    mock_load.return_value = catchments
    mock_refine.return_value = local_gdf
    snap_reference = MeritSnapReference(
        snap=MeritSnap(
            lat=0.5,
            lon=0.5,
            row=0,
            col=0,
            distance_m=5.0,
            snapped_upa_km2=250.0,
            snap_quality="gee_official",
        ),
        source="gee",
        cache_key="snap",
        bbox=(0, 0, 1, 1),
        official_merit_upa_km2=250.0,
    )

    result = merit_basins_hybrid_delineate(0.5, 0.5, snap_reference=snap_reference)
    assert result.execution_mode == "local_vector_topology_terminal_raster_refinement"
    assert result.terminal_catchment_id == "1"
    assert result.upstream_catchment_count == 3
    assert result.terminal_refinement_used is True
    assert result.area_km2 < result.vector_assembly_area_km2
    assert result.refined_polygon_area_km2 < result.vector_assembly_area_km2
    assert "HYBRID_ROUTING_USED" in result.quality_flags
    assert result.safe_envelope_version == MERIT_SAFE_ENVELOPE_VERSION


def test_offline_snap_cache_uses_published_accum_by_default_without_deriving_upa():
    class FakeStatus:
        accum_ready = False
        accum_path = None

    with patch("aihydro_watershed.merit.merit_manager.MeritDataManager") as MockMgr, patch(
        "aihydro_watershed.delineation.merit_flowdir_pipeline.merit_build_local_upstream_area_cache",
        side_effect=AssertionError("local upstream area must be explicit"),
    ):
        MockMgr.return_value.routing_region_cache.return_value = FakeStatus()
        result = merit_build_offline_snap_cache("77")

    assert result["snap_cache_type"] == "published_accum"
    assert result["ready"] is False
    assert "optional" in result["message"]


def test_offline_snap_cache_published_accum_ready_when_explicitly_staged():
    class FakeStatus:
        accum_ready = True
        accum_path = "/tmp/accum77.tif"

    with patch("aihydro_watershed.merit.merit_manager.MeritDataManager") as MockMgr:
        MockMgr.return_value.routing_region_cache.return_value = FakeStatus()
        result = merit_build_offline_snap_cache("77", offline_snap_asset="published_accum")

    assert result["snap_cache_type"] == "published_accum"
    assert result["ready"] is True
    assert result["accum_path"] == "/tmp/accum77.tif"


@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_basins_hybrid_delineate")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.local_merit_flowdir_pyflwdir")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_check_routing_region_cache")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_resolve_pfaf_region")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_get_snap_reference")
def test_auto_cached_local_overflow_promotes_to_hybrid(
    mock_snap,
    mock_pfaf,
    mock_cache,
    mock_local,
    mock_hybrid,
):
    geom = box(77.1, 28.2, 77.4, 28.6)
    gdf = gpd.GeoDataFrame(geometry=[geom], crs=4326)
    mock_pfaf.return_value = "77"
    mock_cache.return_value = {"flowdir_ready": True}
    mock_snap.return_value = MeritSnapReference(
        snap=MeritSnap(
            lat=28.4,
            lon=77.2,
            row=2,
            col=2,
            distance_m=20.0,
            snapped_upa_km2=1000.0,
            snap_quality="area_targeted",
        ),
        source="gee",
        cache_key="snap",
        bbox=(77.1, 28.3, 77.3, 28.5),
        official_merit_upa_km2=1000.0,
    )
    mock_local.side_effect = RuntimeError("HYBRID_ROUTING_REQUIRED window too large")
    mock_hybrid.return_value = MeritFlowdirResult(
        gdf=gdf,
        area_km2=area_km2(gdf),
        outlet_lat=28.4,
        outlet_lon=77.2,
        snap_distance_m=20.0,
        snapped_upa_km2=1000.0,
        snap_quality="area_targeted",
        snap_validation={"ok": True},
        cache_key="hybrid",
        bbox=(77, 28, 78, 29),
        scout_box_maxed=False,
        touches_edge=False,
        area_validation={"ok": True},
        quality_flags=["HYBRID_ROUTING_USED"],
        source="merit_basins_hybrid",
        routing_dataset="MERIT-Basins + MERIT Hydro flowdir",
        routing_resolution_m=92.77,
        pfaf_region="77",
        routing_data_source="local_merit_basins_vectors_and_flowdir",
        snap_source="gee",
        official_merit_upa_km2=1000.0,
        polygon_area_km2=area_km2(gdf),
        validation_sources=["gee_official_upa"],
        regional_flowdir_cached=True,
        execution_mode="local_vector_topology_terminal_raster_refinement",
        safe_envelope_version=MERIT_SAFE_ENVELOPE_VERSION,
        terminal_catchment_id="123",
        upstream_catchment_count=12,
        terminal_refinement_used=True,
        vector_assembly_area_km2=area_km2(gdf),
        refined_polygon_area_km2=area_km2(gdf),
    )

    from ai_hydro.analysis.delineation.router import delineate_from_point

    result = delineate_from_point(28.4, 77.2, method="auto")
    assert result.data["method_used"] == "merit_basins_hybrid"
    assert result.data["safe_envelope_version"] == MERIT_SAFE_ENVELOPE_VERSION
    assert result.data["terminal_catchment_id"] == "123"
    assert "HYBRID_ROUTING_USED" in result.data["quality_flags"]


@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_basins_hybrid_delineate")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.local_merit_flowdir_pyflwdir")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.delineate_merit_gee")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_check_routing_region_cache")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_resolve_pfaf_region")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_get_snap_reference")
def test_gee_memory_failure_recovers_through_hybrid_when_local_overflows(
    mock_snap,
    mock_pfaf,
    mock_cache,
    mock_gee,
    mock_local,
    mock_hybrid,
):
    geom = box(77.1, 28.2, 77.4, 28.6)
    gdf = gpd.GeoDataFrame(geometry=[geom], crs=4326)
    mock_pfaf.return_value = "77"
    mock_cache.return_value = {"flowdir_ready": True}
    mock_snap.return_value = MeritSnapReference(
        snap=MeritSnap(
            lat=28.4,
            lon=77.2,
            row=2,
            col=2,
            distance_m=20.0,
            snapped_upa_km2=1000.0,
            snap_quality="area_targeted",
        ),
        source="gee",
        cache_key="snap",
        bbox=(77.1, 28.3, 77.3, 28.5),
        official_merit_upa_km2=1000.0,
    )
    mock_gee.side_effect = RuntimeError("User memory limit exceeded.")
    mock_local.side_effect = RuntimeError("HYBRID_ROUTING_REQUIRED adaptive local too large")
    mock_hybrid.return_value = MeritFlowdirResult(
        gdf=gdf,
        area_km2=area_km2(gdf),
        outlet_lat=28.4,
        outlet_lon=77.2,
        snap_distance_m=20.0,
        snapped_upa_km2=1000.0,
        snap_quality="area_targeted",
        snap_validation={"ok": True},
        cache_key="hybrid",
        bbox=(77, 28, 78, 29),
        scout_box_maxed=False,
        touches_edge=False,
        area_validation={"ok": True},
        quality_flags=["HYBRID_ROUTING_USED"],
        source="merit_basins_hybrid",
        routing_dataset="MERIT-Basins + MERIT Hydro flowdir",
        routing_resolution_m=92.77,
        pfaf_region="77",
        routing_data_source="local_merit_basins_vectors_and_flowdir",
        snap_source="gee",
        official_merit_upa_km2=1000.0,
        polygon_area_km2=area_km2(gdf),
        validation_sources=["gee_official_upa"],
        regional_flowdir_cached=True,
        execution_mode="local_vector_topology_terminal_raster_refinement",
        safe_envelope_version=MERIT_SAFE_ENVELOPE_VERSION,
        terminal_catchment_id="123",
        upstream_catchment_count=12,
        terminal_refinement_used=True,
        vector_assembly_area_km2=area_km2(gdf),
        refined_polygon_area_km2=area_km2(gdf),
    )

    from ai_hydro.analysis.delineation.router import delineate_from_point

    result = delineate_from_point(28.4, 77.2, method="merit_gee")
    assert result.data["method_used"] == "merit_basins_hybrid"
    assert result.data["fallback_history"][0]["reason"] == "GEE_MEMORY_LIMIT"
    assert result.data["fallback_history"][1]["method"] == "local_merit_flowdir_pyflwdir"
    assert result.data["fallback_history"][2]["method"] == "merit_basins_hybrid"


@patch("aihydro_watershed.delineation.pysheds_pipeline.delineate_fast")
@patch("aihydro_watershed.delineation.nldi_point.delineate_nldi_at_point")
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
    assert result.data["workflow_steps"][0]["step"] == "nldi_comid_lookup"


@patch("aihydro_watershed.delineation.pysheds_pipeline.delineate_fast")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.delineate_merit_gee")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_check_routing_region_cache")
@patch("aihydro_watershed.delineation.nldi_point.delineate_nldi_at_point")
def test_auto_conus_falls_back_to_raw_dem_when_nldi_and_merit_fail(
    mock_nldi, mock_cache, mock_merit, mock_fast
):
    geom = box(-96.5, 40.5, -96.2, 40.9)
    gdf = gpd.GeoDataFrame(geometry=[geom], crs=4326)
    from ai_hydro.core import HydroMeta, HydroResult

    mock_nldi.return_value = HydroResult(
        data={"area_km2": 60_000.0, "method_used": "nldi_comid"},
        meta=HydroMeta(tool="test", version="0", params={}, sources=[]),
    )
    mock_cache.return_value = {"flowdir_ready": False}
    mock_merit.side_effect = RuntimeError("GEE unavailable")
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
    assert result.data["method_used"] == "dem_raw_fallback"
    assert result.data["workflow_steps"][-1]["step"] == "fallback_warning"


@patch("aihydro_watershed.delineation.pysheds_pipeline.delineate_fast")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.delineate_merit_gee")
def test_auto_global_uses_merit_gee_before_raw_dem(mock_merit, mock_fast):
    geom = box(77.1, 28.2, 77.4, 28.6)
    gdf = gpd.GeoDataFrame(geometry=[geom], crs=4326)
    mock_merit.return_value = MeritFlowdirResult(
        gdf=gdf,
        area_km2=area_km2(gdf),
        outlet_lat=28.4,
        outlet_lon=77.2,
        snap_distance_m=100.0,
        snapped_upa_km2=1000.0,
        snap_quality="nearest_merit_stream",
        snap_validation={"ok": True},
        cache_key="abc",
        bbox=(77.0, 28.0, 77.5, 28.7),
        scout_box_maxed=False,
        touches_edge=False,
        area_validation={"ok": True},
        quality_flags=[],
        source="gee",
        routing_dataset=MERIT_GEE_DATASET,
        routing_resolution_m=92.77,
    )
    from ai_hydro.analysis.delineation.router import delineate_from_point

    with patch(
        "aihydro_watershed.delineation.merit_flowdir_pipeline.merit_check_routing_region_cache",
        return_value={"pfaf_region": "45", "flowdir_ready": False},
    ):
        result = delineate_from_point(28.4, 77.2, method="auto")
    mock_merit.assert_called_once()
    mock_fast.assert_not_called()
    assert result.data["method_used"] == "merit_gee_pyflwdir"
    assert result.data["routing_dataset"] == MERIT_GEE_DATASET
    assert result.data["license"]
    assert result.data["snap_validation"] == {"ok": True}
    assert [s["step"] for s in result.data["workflow_steps"]] == [
        "gee_fetch_merit_hydro",
        "outlet_snap",
        "local_flow_routing",
        "polygonize_and_validate",
    ]


def test_merit_bundle_delineation_synthetic():
    from affine import Affine
    from ai_hydro.analysis.delineation.merit_flowdir_pipeline import delineate_merit_bundle

    pytest.importorskip("pyflwdir")
    direction = np.array(
        [
            [2, 2, 4, 8, 8],
            [1, 2, 4, 8, 16],
            [1, 1, 0, 16, 16],
            [128, 128, 64, 32, 32],
            [128, 128, 64, 32, 32],
        ],
        dtype=np.uint8,
    )
    upa = np.ones((5, 5), dtype=float)
    upa[2, 2] = 25.0
    wth = np.zeros((5, 5), dtype=float)
    wth[2, 2] = 10.0
    transform = Affine(0.01, 0, -0.025, 0, -0.01, 0.025)
    bundle = MeritRasterBundle(
        bands={"dir": direction, "upa": upa, "wth": wth},
        transform=transform,
        crs="EPSG:4326",
        cache_key="synthetic",
        source="synthetic",
        bbox=(-0.025, -0.025, 0.025, 0.025),
    )

    result = delineate_merit_bundle(bundle, 0.0, 0.0, expected_area_km2=25.0)
    assert not result.gdf.empty
    assert result.area_km2 > 0
    assert result.snap_quality == "area_targeted"


@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.delineate_merit_gee")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.local_merit_flowdir_pyflwdir")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_check_routing_region_cache")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_resolve_pfaf_region")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_get_snap_reference")
def test_auto_global_cached_flowdir_selects_local_merit(
    mock_snap,
    mock_pfaf,
    mock_cache,
    mock_local,
    mock_gee,
):
    geom = box(77.1, 28.2, 77.4, 28.6)
    gdf = gpd.GeoDataFrame(geometry=[geom], crs=4326)
    mock_pfaf.return_value = "45"
    mock_cache.return_value = {"flowdir_ready": True}
    mock_snap.return_value = MeritSnapReference(
        snap=MeritSnap(
            lat=28.4,
            lon=77.2,
            row=2,
            col=2,
            distance_m=20.0,
            snapped_upa_km2=1000.0,
            snap_quality="gee_official",
        ),
        source="gee",
        cache_key="snap",
        bbox=(77.1, 28.3, 77.3, 28.5),
        official_merit_upa_km2=1000.0,
    )
    mock_local.return_value = MeritFlowdirResult(
        gdf=gdf,
        area_km2=area_km2(gdf),
        outlet_lat=28.4,
        outlet_lon=77.2,
        snap_distance_m=20.0,
        snapped_upa_km2=1000.0,
        snap_quality="gee_official",
        snap_validation={"ok": True},
        cache_key="local",
        bbox=(70, 20, 80, 30),
        scout_box_maxed=False,
        touches_edge=False,
        area_validation={"ok": True},
        quality_flags=[],
        source="local_merit_flowdir",
        routing_dataset=MERIT_GEE_DATASET,
        routing_resolution_m=92.77,
        pfaf_region="45",
        routing_data_source="local_flowdir",
        snap_source="gee",
        official_merit_upa_km2=1000.0,
        polygon_area_km2=area_km2(gdf),
        validation_sources=["gee_official_upa"],
        regional_flowdir_cached=True,
    )

    from ai_hydro.analysis.delineation.router import delineate_from_point

    result = delineate_from_point(28.4, 77.2, method="auto")
    mock_local.assert_called_once()
    mock_gee.assert_not_called()
    assert result.data["method_used"] == "local_merit_pyflwdir"
    assert result.data["regional_flowdir_cached"] is True
    assert result.data["official_merit_upa_km2"] == 1000.0


@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.local_merit_flowdir_pyflwdir")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.delineate_merit_gee")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_check_routing_region_cache")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_resolve_pfaf_region")
@patch("aihydro_watershed.delineation.merit_flowdir_pipeline.merit_get_snap_reference")
def test_merit_gee_memory_failure_recovers_with_cached_local_flowdir(
    mock_snap,
    mock_pfaf,
    mock_cache,
    mock_gee,
    mock_local,
):
    geom = box(77.1, 28.2, 77.4, 28.6)
    gdf = gpd.GeoDataFrame(geometry=[geom], crs=4326)
    mock_pfaf.return_value = "45"
    mock_cache.return_value = {"flowdir_ready": True}
    mock_snap.return_value = MeritSnapReference(
        snap=MeritSnap(
            lat=28.4,
            lon=77.2,
            row=2,
            col=2,
            distance_m=20.0,
            snapped_upa_km2=1000.0,
            snap_quality="area_targeted",
        ),
        source="gee",
        cache_key="snap",
        bbox=(77.1, 28.3, 77.3, 28.5),
        official_merit_upa_km2=1000.0,
    )
    mock_gee.side_effect = RuntimeError("User memory limit exceeded.")
    mock_local.return_value = MeritFlowdirResult(
        gdf=gdf,
        area_km2=area_km2(gdf),
        outlet_lat=28.4,
        outlet_lon=77.2,
        snap_distance_m=20.0,
        snapped_upa_km2=1000.0,
        snap_quality="area_targeted",
        snap_validation={"ok": True},
        cache_key="local",
        bbox=(77, 28, 78, 29),
        scout_box_maxed=False,
        touches_edge=False,
        area_validation={"ok": True},
        quality_flags=[],
        source="local_merit_flowdir",
        routing_dataset=MERIT_GEE_DATASET,
        routing_resolution_m=92.77,
        pfaf_region="45",
        routing_data_source="local_flowdir",
        snap_source="gee",
        official_merit_upa_km2=1000.0,
        polygon_area_km2=area_km2(gdf),
        validation_sources=["gee_official_upa"],
        regional_flowdir_cached=True,
        execution_mode="local_staged_flowdir_adaptive_window",
        regional_flowdir_file_size_bytes=123,
        window_expansion_iterations=1,
        final_window_bounds={"xmin": 77.0, "ymin": 28.0, "xmax": 78.0, "ymax": 29.0},
        final_window_cell_count=10_000,
        basin_touched_window_boundary=False,
        window_complete=True,
        peak_memory_mb=250.0,
        runtime_seconds=0.5,
    )

    from ai_hydro.analysis.delineation.router import delineate_from_point

    result = delineate_from_point(28.4, 77.2, method="merit_gee")
    assert result.data["method_used"] == "local_merit_pyflwdir"
    assert result.data["official_merit_upa_km2"] == 1000.0
    assert result.data["validation_sources"] == ["gee_official_upa"]
    assert result.data["execution_mode"] == "local_staged_flowdir_adaptive_window"
    assert result.data["fallback_history"] == [
        {
            "method": "merit_gee_pyflwdir",
            "outcome": "failed",
            "reason": "GEE_MEMORY_LIMIT",
        },
        {"method": "local_merit_flowdir_pyflwdir", "outcome": "succeeded"},
    ]


@patch("aihydro_watershed.delineation.pysheds_pipeline.delineate_fast")
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
    assert result.data["method_used"] == "dem_raw_fallback"
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
