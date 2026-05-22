from __future__ import annotations

import json

import pytest

from ai_hydro.gee.contracts import (
    AnalysisArtifact,
    ExportTaskRecord,
    LiveLayer,
    ProvenanceRecord,
    ROIContract,
    ReportBundle,
    WorkflowRun,
    scrub_secrets,
)
from ai_hydro.gee.presets import get_preset, list_presets


def _roi() -> ROIContract:
    return ROIContract.from_geojson(
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
        source="geojson",
    )


def test_gee_contracts_are_json_serializable():
    roi = _roi()
    provenance = ProvenanceRecord(
        provenance_id="prov_test",
        tool_name="gee.preview_layer",
        operation="preview_layer",
        roi=roi.to_dict(),
    )
    contracts = [
        roi,
        get_preset("precip.chirps.daily"),
        LiveLayer(
            layer_id="layer_test",
            layer_type="gee_tile",
            name="CHIRPS",
            dataset_id="UCSB-CHC/CHIRPS/V3/DAILY_SAT",
            band="precipitation",
            bounds_wgs84=[0, 0, 1, 1],
        ),
        AnalysisArtifact(
            artifact_id="artifact_test",
            artifact_type="csv",
            path="outputs/gee/test.csv",
            name="test.csv",
        ),
        ReportBundle(report_id="report_test", title="Report"),
        ExportTaskRecord(task_id="task_test", task_type="image_export", status="submitted"),
        WorkflowRun(workflow_id="workflow_test", name="Workflow", status="completed"),
        provenance,
    ]
    for contract in contracts:
        json.dumps(contract.to_dict())
        assert contract.to_dict()["schema_version"] == "gee-contracts.v1"


def test_dataset_presets_validate_hydrology_semantics():
    chirps = get_preset("precip.chirps.daily")
    chirps.validate_spatial_reducer("mean")
    chirps.validate_temporal_aggregation("monthly_sum")
    with pytest.raises(ValueError):
        chirps.validate_temporal_aggregation("monthly_median")

    landcover = get_preset("landcover.nlcd")
    landcover.validate_spatial_reducer("fractions")
    with pytest.raises(ValueError):
        landcover.validate_spatial_reducer("mean")


def test_initial_preset_registry_contains_required_groups():
    preset_ids = {preset.preset_id for preset in list_presets()}
    assert {
        "precip.chirps.daily",
        "dem.srtm",
        "ndvi.modis",
        "landcover.nlcd",
        "landcover.esa_worldcover",
        "climate.era5_land",
    }.issubset(preset_ids)


def test_scrub_secrets_preserves_status_but_removes_secret_paths():
    cleaned = scrub_secrets(
        {
            "authenticated": True,
            "credentials_found": True,
            "runtime": {
                "credentials_path": "/Users/example/.config/earthengine/credentials",
                "python_executable": "/opt/python",
            },
            "refresh_token": "secret",
        }
    )
    assert cleaned["authenticated"] is True
    assert cleaned["credentials_found"] is True
    assert "credentials_path" not in cleaned["runtime"]
    assert "refresh_token" not in cleaned
