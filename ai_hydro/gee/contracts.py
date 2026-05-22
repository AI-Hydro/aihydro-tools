from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


SCHEMA_VERSION = "gee-contracts.v1"
MISSING_BASIN_MESSAGE = "No active basin geometry found. Draw or load a basin in the map first."

_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "authorization",
    "password",
    "refresh",
)
_SENSITIVE_EXACT_KEYS = {
    "credentials_path",
    "credential_path",
    "credentials_file",
    "credential_file",
    "oauth_file",
    "auth_file",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def scrub_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _SENSITIVE_EXACT_KEYS or any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                continue
            clean[str(key)] = scrub_secrets(item)
        return clean
    if isinstance(value, list):
        return [scrub_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [scrub_secrets(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def geometry_bbox(geometry: dict[str, Any]) -> list[float]:
    coords: list[tuple[float, float]] = []

    def walk(node: Any) -> None:
        if (
            isinstance(node, list)
            and len(node) >= 2
            and isinstance(node[0], (int, float))
            and isinstance(node[1], (int, float))
        ):
            coords.append((float(node[0]), float(node[1])))
            return
        if isinstance(node, list):
            for child in node:
                walk(child)

    if geometry.get("type") == "Feature":
        geometry = geometry.get("geometry") or {}
    if geometry.get("type") == "FeatureCollection":
        for feature in geometry.get("features", []):
            walk((feature.get("geometry") or {}).get("coordinates"))
    else:
        walk(geometry.get("coordinates"))

    if not coords:
        raise ValueError("ROI geometry has no coordinates")
    xs = [x for x, _ in coords]
    ys = [y for _, y in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


class AIHydroContract(BaseModel):
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return scrub_secrets(self.model_dump(mode="json", exclude_none=True))


class ROIContract(AIHydroContract):
    type: Literal["roi"] = "roi"
    roi_id: str
    source: Literal["map_drawn", "loaded_geojson", "hydro_session", "delineated_basin", "geojson"]
    name: str = "Selected basin"
    geometry: dict[str, Any]
    crs: str = "EPSG:4326"
    bbox: list[float]
    area_km2: float | None = None
    geometry_hash: str
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @classmethod
    def from_geojson(
        cls,
        geometry: dict[str, Any],
        *,
        source: Literal["map_drawn", "loaded_geojson", "hydro_session", "delineated_basin", "geojson"],
        name: str = "Selected basin",
        area_km2: float | None = None,
    ) -> "ROIContract":
        bbox = geometry_bbox(geometry)
        geometry_hash = stable_json_hash(geometry)
        return cls(
            roi_id=f"roi_{geometry_hash[:16]}",
            source=source,
            name=name,
            geometry=geometry,
            bbox=bbox,
            area_km2=area_km2,
            geometry_hash=geometry_hash,
        )


class DatasetPreset(AIHydroContract):
    type: Literal["dataset_preset"] = "dataset_preset"
    preset_id: str
    dataset_id: str
    bands: list[str]
    variable_type: str
    allowed_spatial_reducers: list[str]
    allowed_temporal_aggregations: list[str]
    default_visualization: dict[str, Any] = Field(default_factory=dict)
    scale_m: float
    units: str
    output_units: dict[str, str] = Field(default_factory=dict)
    citation: str
    hydrologic_use_notes: str
    default_workflows: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    temporal: bool = True
    categorical: bool = False

    def validate_band(self, band: str) -> None:
        if band not in self.bands:
            raise ValueError(f"Band '{band}' is not allowed for preset '{self.preset_id}'.")

    def validate_spatial_reducer(self, reducer: str) -> None:
        if reducer not in self.allowed_spatial_reducers:
            raise ValueError(f"Spatial reducer '{reducer}' is not allowed for preset '{self.preset_id}'.")

    def validate_temporal_aggregation(self, aggregation: str) -> None:
        if aggregation not in self.allowed_temporal_aggregations:
            raise ValueError(f"Temporal aggregation '{aggregation}' is not allowed for preset '{self.preset_id}'.")


class ProvenanceRecord(AIHydroContract):
    type: Literal["provenance_record"] = "provenance_record"
    provenance_id: str
    tool_name: str
    operation: str
    dataset_preset_id: str | None = None
    dataset_id: str | None = None
    band: str | None = None
    date_range: dict[str, str] | None = None
    reducer: str | None = None
    temporal_aggregation: str | None = None
    roi: dict[str, Any] | None = None
    output_paths: list[str] = Field(default_factory=list)
    input_parameters: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _sanitize(self) -> "ProvenanceRecord":
        self.input_parameters = scrub_secrets(self.input_parameters)
        self.runtime = scrub_secrets(self.runtime)
        return self


class LiveLayer(AIHydroContract):
    type: Literal["live_layer"] = "live_layer"
    layer_id: str
    layer_type: Literal["gee_tile", "geojson", "raster"]
    name: str
    dataset_preset_id: str | None = None
    dataset_id: str
    band: str | None = None
    tile_url_template: str | None = None
    bounds_wgs84: list[float]
    legend: dict[str, Any] = Field(default_factory=dict)
    opacity: float = 0.75
    roi: dict[str, Any] | None = None
    provenance_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnalysisArtifact(AIHydroContract):
    type: Literal["analysis_artifact"] = "analysis_artifact"
    artifact_id: str
    artifact_type: Literal["csv", "geotiff", "json", "png", "summary"]
    path: str
    name: str
    dataset_preset_id: str | None = None
    dataset_id: str | None = None
    band: str | None = None
    row_count: int | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    provenance_path: str | None = None
    content_hash: str | None = None


class ReportBundle(AIHydroContract):
    type: Literal["report_bundle"] = "report_bundle"
    report_id: str
    title: str
    roi: dict[str, Any] | None = None
    layers: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    provenance_paths: list[str] = Field(default_factory=list)


class ExportTaskRecord(AIHydroContract):
    type: Literal["export_task_record"] = "export_task_record"
    task_id: str
    task_type: str
    status: str
    output_target: str | None = None
    submitted_parameters: dict[str, Any] = Field(default_factory=dict)
    provenance_path: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _sanitize(self) -> "ExportTaskRecord":
        self.submitted_parameters = scrub_secrets(self.submitted_parameters)
        return self


class WorkflowRun(AIHydroContract):
    type: Literal["workflow_run"] = "workflow_run"
    workflow_id: str
    name: str
    status: str
    roi: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    layers: list[dict[str, Any]] = Field(default_factory=list)
    provenance_paths: list[str] = Field(default_factory=list)
    started_at: str = Field(default_factory=utc_now)
    completed_at: str | None = None
