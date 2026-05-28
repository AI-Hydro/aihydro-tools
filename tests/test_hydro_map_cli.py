from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


def _args(**overrides):
    base = {
        "session_id": "map",
        "lat": 25.74401,
        "lon": 79.38185,
        "workspace_dir": None,
        "expected_area_km2": None,
        "method": "auto",
        "name": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _basins_args(**overrides):
    base = {
        "pfaf": "45",
        "lat": None,
        "lon": None,
        "download": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_map_cli_basins_region_check_only_reports_missing_without_download():
    from ai_hydro import hydro_map_cli

    fake_status = SimpleNamespace(
        pfaf_region="45",
        catchments_ready=False,
        catchments_path=None,
        rivers_ready=True,
        rivers_path="/tmp/riv.shp",
        acquisition_required=True,
        acquisition_policy="check_only",
        estimated_download_size_bytes=None,
        metadata_path=None,
        source="local_cache_or_manifest",
        license="license",
        citation="citation",
        message="missing",
        downloaded=None,
    )

    with patch("ai_hydro.data.merit_manager.MeritDataManager") as MockMgr:
        mgr = MockMgr.return_value
        mgr.ensure_basins_region.return_value = fake_status
        out = hydro_map_cli.cmd_merit_ensure_basins_region(_basins_args())

    mgr.ensure_basins_region.assert_called_once_with("45", acquisition_policy="check_only")
    assert out["ok"] is False
    assert out["pfaf_region"] == "45"
    assert out["catchments_ready"] is False
    assert out["rivers_ready"] is True


def test_map_cli_basins_region_download_uses_explicit_policy():
    from ai_hydro import hydro_map_cli

    fake_status = SimpleNamespace(
        pfaf_region="45",
        catchments_ready=True,
        catchments_path="/tmp/cat.shp",
        rivers_ready=True,
        rivers_path="/tmp/riv.shp",
        acquisition_required=False,
        acquisition_policy="download_if_missing",
        estimated_download_size_bytes=None,
        metadata_path="/tmp/meta.json",
        source="mirror",
        license="license",
        citation="citation",
        message="ready",
        downloaded=["catchments_45"],
    )

    with patch("ai_hydro.data.merit_manager.MeritDataManager") as MockMgr:
        mgr = MockMgr.return_value
        mgr.ensure_basins_region.return_value = fake_status
        out = hydro_map_cli.cmd_merit_ensure_basins_region(_basins_args(download=True))

    mgr.ensure_basins_region.assert_called_once_with("45", acquisition_policy="download_if_missing")
    assert out["ok"] is True
    assert out["metadata_path"] == "/tmp/meta.json"
    assert out["downloaded"] == ["catchments_45"]


def test_map_cli_catchment_layers_stages_then_clips_view():
    from ai_hydro import hydro_map_cli

    fake_status = SimpleNamespace(
        pfaf_region="45",
        catchments_ready=True,
        catchments_path="/tmp/cat.shp",
        rivers_ready=True,
        rivers_path="/tmp/riv.shp",
        acquisition_required=False,
        acquisition_policy="download_if_missing",
        estimated_download_size_bytes=None,
        metadata_path="/tmp/meta.json",
        source="mirror",
        license="license",
        citation="citation",
        message="ready",
        downloaded=None,
    )

    with patch("ai_hydro.data.merit_manager.MeritDataManager") as MockMgr, patch(
        "ai_hydro.data.merit_map_layers.merit_map_layers_for_view",
        return_value=[{"id": "merit-catchments-45", "name": "MERIT catchments (Pfaf 45)"}],
    ) as layers_for_view:
        mgr = MockMgr.return_value
        mgr.ensure_basin.return_value = SimpleNamespace(pfaf_code="45")
        mgr.resolve_pfaf_region.return_value = "45"
        mgr.ensure_basins_region.return_value = fake_status
        out = hydro_map_cli.cmd_merit_catchment_layers(
            SimpleNamespace(
                lat=25.7,
                lon=79.3,
                min_lon=79.0,
                min_lat=25.0,
                max_lon=80.0,
                max_lat=26.0,
                pfaf=None,
                no_download=False,
            )
        )

    mgr.ensure_basins_region.assert_called_once_with("45", acquisition_policy="download_if_missing")
    layers_for_view.assert_called_once()
    assert out["ok"] is True
    assert out["layers"][0]["id"] == "merit-catchments-45"
    assert out["staging"]["catchments_ready"] is True


def test_merit_basins_region_uses_google_drive_catchment_fallback(tmp_path):
    from ai_hydro.data.merit_manager import MeritDataManager

    mgr = MeritDataManager(root=tmp_path)
    river_dir = tmp_path / "shp" / "merit_rivers"
    river_dir.mkdir(parents=True)
    (river_dir / "riv_pfaf_46_MERIT_Hydro_v07_Basins_v01.shp").write_bytes(b"river")

    def fake_download(pfaf, dest_dir):
        assert pfaf == "46"
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "cat_pfaf_46_MERIT_Hydro_v07_Basins_v01.shp").write_bytes(b"catchment")
        return True

    with patch("ai_hydro.data.merit_download.download_catchment_shapefile", side_effect=fake_download):
        status = mgr.ensure_basins_region("46", acquisition_policy="download_if_missing")

    assert status.catchments_ready
    assert status.rivers_ready
    assert status.downloaded == ["catchments_46"]
    assert status.metadata_path is not None


def test_map_delineate_auto_stages_nonconus_flowdir_and_uses_local_merit():
    from ai_hydro import hydro_map_cli

    calls = {}

    def fake_delineate(**kwargs):
        calls.update(kwargs)
        return {
            "data": {
                "area_km2": 10781.5,
                "method_used": "local_merit_pyflwdir",
            }
        }

    with patch("ai_hydro.analysis.delineation.router.is_conus", return_value=False), \
        patch(
            "ai_hydro.analysis.delineation.merit_flowdir_pipeline.merit_resolve_pfaf_region",
            return_value="45",
        ), \
        patch(
            "ai_hydro.analysis.delineation.merit_flowdir_pipeline.merit_ensure_routing_region",
            return_value={"pfaf_region": "45", "flowdir_ready": True},
        ) as ensure_region, \
        patch(
            "ai_hydro.mcp.tools_analysis.delineate_watershed_from_point",
            side_effect=fake_delineate,
        ):
        out = hydro_map_cli.cmd_delineate_point(_args())

    assert out["ok"] is True
    assert out["staging"]["flowdir_ready"] is True
    assert calls["method"] == "local_merit"
    ensure_region.assert_called_once_with(
        pfaf_region="45",
        acquisition_policy="download_if_missing",
    )


def test_map_delineate_auto_uses_merit_gee_when_nonconus_flowdir_unavailable():
    from ai_hydro import hydro_map_cli

    calls = {}

    def fake_delineate(**kwargs):
        calls.update(kwargs)
        return {
            "data": {
                "area_km2": 100.0,
                "method_used": "merit_gee_pyflwdir",
            }
        }

    with patch("ai_hydro.analysis.delineation.router.is_conus", return_value=False), \
        patch(
            "ai_hydro.analysis.delineation.merit_flowdir_pipeline.merit_resolve_pfaf_region",
            return_value="45",
        ), \
        patch(
            "ai_hydro.analysis.delineation.merit_flowdir_pipeline.merit_ensure_routing_region",
            return_value={"pfaf_region": "45", "flowdir_ready": False},
        ), \
        patch(
            "ai_hydro.mcp.tools_analysis.delineate_watershed_from_point",
            side_effect=fake_delineate,
        ):
        out = hydro_map_cli.cmd_delineate_point(_args())

    assert out["ok"] is True
    assert calls["method"] == "merit_gee"


def test_map_delineate_auto_preserves_conus_nldi_order():
    from ai_hydro import hydro_map_cli

    calls = {}

    def fake_delineate(**kwargs):
        calls.update(kwargs)
        return {
            "data": {
                "area_km2": 114.0,
                "method_used": "nldi_comid",
            }
        }

    with patch("ai_hydro.analysis.delineation.router.is_conus", return_value=True), \
        patch(
            "ai_hydro.analysis.delineation.merit_flowdir_pipeline.merit_ensure_routing_region"
        ) as ensure_region, \
        patch(
            "ai_hydro.mcp.tools_analysis.delineate_watershed_from_point",
            side_effect=fake_delineate,
        ):
        out = hydro_map_cli.cmd_delineate_point(
            _args(lat=40.71829, lon=-96.41265)
        )

    assert out["ok"] is True
    assert out["staging"] is None
    assert calls["method"] == "auto"
    ensure_region.assert_not_called()
