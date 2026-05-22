from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_hydro.mcp import tools_gee


def test_gee_status_schema_accepts_optional_project_id():
    parsed = tools_gee.GeeStatusInput(project_id="my-project")
    assert parsed.project_id == "my-project"


def test_gee_preview_layer_schema_rejects_bad_date():
    with pytest.raises(ValidationError):
        tools_gee.GeePreviewLayerInput(
            start_date="2026/01/01",
            end_date="2026-01-31",
        )


def test_gee_extract_timeseries_schema_rejects_bad_temporal_aggregation():
    with pytest.raises(ValidationError):
        tools_gee.GeeExtractTimeseriesInput(
            dataset_id="UCSB-CHC/CHIRPS/V3/DAILY_SAT",
            band="precipitation",
            start_date="2026-01-01",
            end_date="2026-01-31",
            temporal_aggregation="weekly",  # type: ignore[arg-type]
        )


def test_gee_preview_layer_pushes_map_layer(monkeypatch, tmp_path: Path):
    captured: dict = {}

    monkeypatch.setattr(
        tools_gee,
        "_get_session_geometry",
        lambda _sid: {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
    )
    monkeypatch.setattr(tools_gee, "_workspace_root", lambda _sid: tmp_path)
    monkeypatch.setattr(
        tools_gee,
        "gee_preview_layer_impl",
        lambda **_kwargs: {
            "ok": True,
            "type": "gee_tile_layer",
            "name": "CHIRPS precipitation",
            "dataset_id": "UCSB-CHC/CHIRPS/V3/DAILY_SAT",
            "band": "precipitation",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "tile_url_template": "https://tiles/{z}/{x}/{y}",
            "bounds_wgs84": [-100.0, 20.0, -90.0, 30.0],
            "provenance": {},
        },
    )

    def _capture_push_layer(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(tools_gee, "push_layer", _capture_push_layer)

    result = tools_gee.gee_preview_layer(
        session_id="session-a",
        dataset_id="UCSB-CHC/CHIRPS/V3/DAILY_SAT",
        band="precipitation",
        start_date="2026-01-01",
        end_date="2026-01-31",
        roi="current_map_basin",
        reducer="sum",
    )

    assert result["ok"] is True
    assert result["live_layer"]["type"] == "live_layer"
    assert result["live_layer"]["roi"]["type"] == "roi"
    assert result["provenance_record"]["type"] == "provenance_record"
    assert captured["layer_type"] == "gee_tile"
    assert "gee_tile_url_template" in captured["metadata"]
    assert Path(result["provenance_path"]).exists()


def test_gee_preview_layer_missing_basin_returns_clear_error():
    result = tools_gee.gee_preview_layer(
        session_id=None,
        dataset_id="UCSB-CHC/CHIRPS/V3/DAILY_SAT",
        band="precipitation",
        start_date="2026-01-01",
        end_date="2026-01-31",
        roi="current_map_basin",
        reducer="sum",
    )
    assert result.get("error") is True
    assert "No active basin geometry found. Draw or load a basin in the map first." in result.get("message", "")


def test_gee_preview_layer_resolves_dataset_preset_without_dataset_fields(monkeypatch, tmp_path: Path):
    adapter_kwargs: dict = {}

    monkeypatch.setattr(
        tools_gee,
        "_get_session_geometry",
        lambda _sid: {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
    )
    monkeypatch.setattr(tools_gee, "_workspace_root", lambda _sid: tmp_path)
    monkeypatch.setattr(tools_gee, "push_layer", lambda **_kwargs: True)

    def _mock_preview(**kwargs):
        adapter_kwargs.update(kwargs)
        return {
            "ok": True,
            "type": "gee_tile_layer",
            "name": "MODIS NDVI",
            "tile_url_template": "https://tiles/{z}/{x}/{y}",
            "bounds_wgs84": [-100.0, 20.0, -90.0, 30.0],
            "provenance": {},
        }

    monkeypatch.setattr(tools_gee, "gee_preview_layer_impl", _mock_preview)

    result = tools_gee.gee_preview_layer(
        session_id="session-a",
        dataset_preset="ndvi.modis",
        start_date="2026-01-01",
        end_date="2026-01-31",
        roi="current_map_basin",
        reducer="mean",
    )

    assert result["ok"] is True
    assert adapter_kwargs["dataset_id"] == "MODIS/061/MOD13Q1"
    assert adapter_kwargs["band"] == "NDVI"
    assert result["live_layer"]["dataset_preset_id"] == "ndvi.modis"


def test_gee_extract_timeseries_writes_csv(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        tools_gee,
        "_get_session_geometry",
        lambda _sid: {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
    )
    monkeypatch.setattr(tools_gee, "_workspace_root", lambda _sid: tmp_path)
    monkeypatch.setattr(
        tools_gee,
        "gee_extract_timeseries_impl",
        lambda **_kwargs: {
            "ok": True,
            "type": "gee_timeseries",
            "rows": [{"date": "2026-01-01", "value": 1.2}, {"date": "2026-01-02", "value": 2.3}],
            "provenance": {},
        },
    )

    result = tools_gee.gee_extract_timeseries(
        session_id="session-a",
        dataset_id="UCSB-CHC/CHIRPS/V3/DAILY_SAT",
        band="precipitation",
        start_date="2026-01-01",
        end_date="2026-01-31",
        roi="current_map_basin",
        spatial_reducer="mean",
        temporal_aggregation="daily",
        scale_m=5000.0,
    )

    assert result["ok"] is True
    assert result["analysis_artifact"]["type"] == "analysis_artifact"
    assert result["analysis_artifact"]["artifact_type"] == "csv"
    assert result["provenance_record"]["type"] == "provenance_record"
    assert result["row_count"] == 2
    assert Path(result["csv_path"]).exists()
    assert Path(result["summary_path"]).exists()
    assert Path(result["provenance_path"]).exists()


def test_gee_extract_timeseries_resolves_dataset_preset_without_dataset_fields(monkeypatch, tmp_path: Path):
    adapter_kwargs: dict = {}

    monkeypatch.setattr(
        tools_gee,
        "_get_session_geometry",
        lambda _sid: {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
    )
    monkeypatch.setattr(tools_gee, "_workspace_root", lambda _sid: tmp_path)

    def _mock_extract(**kwargs):
        adapter_kwargs.update(kwargs)
        return {
            "ok": True,
            "type": "gee_timeseries",
            "rows": [{"date": "2026-01", "value": 12.0}],
            "provenance": {},
        }

    monkeypatch.setattr(tools_gee, "gee_extract_timeseries_impl", _mock_extract)

    result = tools_gee.gee_extract_timeseries(
        session_id="session-a",
        dataset_preset="precip.chirps.daily",
        start_date="2026-01-01",
        end_date="2026-01-31",
        roi="current_map_basin",
        spatial_reducer="mean",
        temporal_aggregation="monthly",
        scale_m=5000.0,
    )

    assert result["ok"] is True
    assert adapter_kwargs["dataset_id"] == "UCSB-CHC/CHIRPS/V3/DAILY_SAT"
    assert adapter_kwargs["band"] == "precipitation"
    assert result["analysis_artifact"]["dataset_preset_id"] == "precip.chirps.daily"


def test_gee_extract_timeseries_validates_preset_semantics(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        tools_gee,
        "_get_session_geometry",
        lambda _sid: {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
    )
    monkeypatch.setattr(tools_gee, "_workspace_root", lambda _sid: tmp_path)

    result = tools_gee.gee_extract_timeseries(
        session_id="session-a",
        dataset_preset="landcover.nlcd",
        dataset_id="USGS/NLCD_RELEASES/2021_REL/NLCD",
        band="landcover",
        start_date="2026-01-01",
        end_date="2026-01-31",
        roi="current_map_basin",
        spatial_reducer="mean",
        temporal_aggregation="daily",
    )

    assert result.get("error") is True
    assert "Spatial reducer 'mean' is not allowed" in result.get("message", "")


def test_gee_status_scrubs_credentials_path(monkeypatch):
    monkeypatch.setattr(
        tools_gee,
        "gee_status_impl",
        lambda **_kwargs: {
            "ok": True,
            "type": "gee_status",
            "authenticated": True,
            "credentials_found": True,
            "runtime": {
                "credentials_path": "/Users/example/.config/earthengine/credentials",
                "python_executable": "/opt/python",
            },
            "message": "ok",
            "provenance": {},
        },
    )

    result = tools_gee.gee_status()

    assert result["ok"] is True
    assert result["authenticated"] is True
    assert "credentials_path" not in result["runtime"]
