"""
Google Earth Engine MCP tools for AI-Hydro agent chat.

These tools are MCP-callable from chat (use_mcp_tool) and reuse the existing
Python->map event bridge so results appear in the map panel without exposing
credentials to the webview.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from ai_hydro.gee.contracts import (
    AnalysisArtifact,
    LiveLayer,
    MISSING_BASIN_MESSAGE,
    ProvenanceRecord,
    ROIContract,
    scrub_secrets,
    stable_json_hash,
)
from ai_hydro.gee.presets import find_preset, get_preset
from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _get_session_geometry, _normalize_session_id, _tool_error_to_dict
from ai_hydro.mcp.map_events import push_layer

try:
    from ai_hydro.gee.auth import status as gee_status_impl
    from ai_hydro.gee.map_layers import preview_layer as gee_preview_layer_impl
    from ai_hydro.gee.timeseries import extract_timeseries as gee_extract_timeseries_impl
except Exception as _gee_import_error:
    def gee_status_impl(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "type": "gee_status",
            "authenticated": False,
            "ee_available": False,
            "message": f"GEE adapter unavailable: {_gee_import_error}",
            "provenance": {"adapter": "ai_hydro.mcp.tools_gee", "operation": "status"},
        }

    def gee_preview_layer_impl(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "type": "gee_tile_layer",
            "message": f"GEE adapter unavailable: {_gee_import_error}",
            "provenance": {"adapter": "ai_hydro.mcp.tools_gee", "operation": "preview_layer"},
        }

    def gee_extract_timeseries_impl(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "type": "gee_timeseries",
            "rows": [],
            "message": f"GEE adapter unavailable: {_gee_import_error}",
            "provenance": {"adapter": "ai_hydro.mcp.tools_gee", "operation": "extract_timeseries"},
        }

log = logging.getLogger("ai_hydro.mcp")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MISSING_BASIN_MSG = MISSING_BASIN_MESSAGE
_DEFAULT_PREVIEW_DATASET_ID = "UCSB-CHC/CHIRPS/V3/DAILY_SAT"
_DEFAULT_PREVIEW_BAND = "precipitation"


class GeeStatusInput(BaseModel):
    project_id: str | None = None


class GeeVisualizationInput(BaseModel):
    min: float | None = None
    max: float | None = None
    palette: list[str] | None = None


class GeePreviewLayerInput(BaseModel):
    session_id: str | None = None
    project_id: str | None = None
    dataset_preset: str | None = None
    dataset_id: str = ""
    band: str = ""
    start_date: str
    end_date: str
    roi: str | dict[str, Any] = "current_map_basin"
    reducer: Literal["sum", "mean", "median", "min", "max"] = "sum"
    visualization: GeeVisualizationInput | None = None

    @field_validator("start_date", "end_date")
    @classmethod
    def _validate_date(cls, value: str) -> str:
        if not _DATE_RE.match(value):
            raise ValueError("Dates must be YYYY-MM-DD")
        return value


class GeeExtractTimeseriesInput(BaseModel):
    session_id: str | None = None
    project_id: str | None = None
    dataset_preset: str | None = None
    dataset_id: str = ""
    band: str = ""
    start_date: str
    end_date: str
    roi: str | dict[str, Any] = "current_map_basin"
    spatial_reducer: Literal["mean", "sum", "median", "min", "max"] = "mean"
    temporal_aggregation: Literal["daily", "monthly", "yearly"] = "daily"
    scale_m: float = Field(default=5000.0, gt=0.0)
    output_name: str | None = None

    @field_validator("start_date", "end_date")
    @classmethod
    def _validate_date(cls, value: str) -> str:
        if not _DATE_RE.match(value):
            raise ValueError("Dates must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def _validate_output_name(self) -> GeeExtractTimeseriesInput:
        if self.output_name and ("/" in self.output_name or "\\" in self.output_name):
            raise ValueError("output_name must be a file name, not a path")
        return self


def _workspace_root(session_id: str | None) -> Path:
    if session_id:
        try:
            from ai_hydro.session import HydroSession
            session = HydroSession.load(session_id)
            if session.workspace_dir:
                return Path(session.workspace_dir)
        except Exception:
            pass
    return Path.cwd()


def _write_provenance(
    *,
    workspace_root: Path,
    tool_name: str,
    payload: dict[str, Any],
    result: dict[str, Any],
) -> str:
    out_dir = workspace_root / "outputs" / "gee"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{tool_name.replace('.', '_')}_{stamp}.json"
    provenance = scrub_secrets({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_name": tool_name,
        "workspace_root": str(workspace_root),
        "payload": payload,
        "result": result,
    })
    out_path.write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return str(out_path)


def _resolve_roi_contract(roi: str | dict[str, Any], session_id: str | None) -> ROIContract:
    if isinstance(roi, str):
        if roi == "current_map_basin":
            if not session_id:
                raise ValueError(_MISSING_BASIN_MSG)
            try:
                return ROIContract.from_geojson(
                    _get_session_geometry(session_id),
                    source="hydro_session",
                    name=f"HydroSession {session_id} basin",
                )
            except Exception:
                raise ValueError(_MISSING_BASIN_MSG)
        try:
            parsed = json.loads(roi)
            if isinstance(parsed, dict):
                return ROIContract.from_geojson(parsed, source="geojson")
        except Exception:
            pass
        raise ValueError("roi must be 'current_map_basin' or a GeoJSON object/string")
    if isinstance(roi, dict):
        return ROIContract.from_geojson(roi, source="geojson")
    raise ValueError("roi must be 'current_map_basin' or a GeoJSON object/string")


def _resolve_dataset(
    *,
    dataset_preset: str | None,
    dataset_id: str,
    band: str,
    allow_default_preview: bool = False,
):
    dataset_id = dataset_id.strip()
    band = band.strip()
    if dataset_preset:
        preset = get_preset(dataset_preset)
        if not dataset_id:
            dataset_id = preset.dataset_id
        if not band:
            band = preset.bands[0]
        if dataset_id != preset.dataset_id:
            raise ValueError(
                f"Dataset preset '{dataset_preset}' expects dataset_id '{preset.dataset_id}', got '{dataset_id}'"
            )
        preset.validate_band(band)
        return preset, dataset_id, band

    if allow_default_preview:
        dataset_id = dataset_id or _DEFAULT_PREVIEW_DATASET_ID
        band = band or _DEFAULT_PREVIEW_BAND
    if not dataset_id or not band:
        raise ValueError("dataset_id and band are required unless dataset_preset is provided")

    return find_preset(dataset_id, band), dataset_id, band


def _emit_gee_tile_layer(layer: LiveLayer, *, roi_source: str) -> None:
    tile_url = layer.tile_url_template
    if not tile_url:
        return

    metadata = {
        "source": "gee",
        "gee_dataset_id": layer.dataset_id,
        "gee_tile_url_template": tile_url,
        "gee_bounds": json.dumps(layer.bounds_wgs84),
        "gee_band": layer.band or "",
        "gee_roi_source": roi_source,
        "gee_provenance_path": layer.provenance_path or "",
    }
    metadata.update({str(k): str(v) for k, v in layer.metadata.items() if v is not None})

    push_layer(
        layer_id=layer.layer_id,
        name=layer.name,
        geojson="",
        layer_type="gee_tile",
        style_preset="default",
        auto_zoom=True,
        open_map=True,
        metadata=metadata,
    )


def _legacy_emit_gee_tile_layer(result: dict[str, Any], *, provenance_path: str, roi_source: str) -> None:
    tile_url = result.get("tile_url_template") or result.get("tile_url")
    if not tile_url:
        return

    bounds = result.get("bounds_wgs84") or [-180.0, -60.0, 180.0, 84.0]
    dataset = str(result.get("dataset_id", "gee"))
    layer_id = f"gee_{dataset.replace('/', '_').replace(':', '_')}_{int(datetime.now(timezone.utc).timestamp())}"

    push_layer(
        layer_id=layer_id,
        name=str(result.get("name", "GEE layer")),
        geojson="",
        layer_type="gee_tile",
        style_preset="default",
        auto_zoom=True,
        open_map=True,
        metadata={
            "source": "gee",
            "gee_dataset_id": dataset,
            "gee_tile_url_template": str(tile_url),
            "gee_bounds": json.dumps(bounds),
            "gee_start_date": str(result.get("start_date", "")),
            "gee_end_date": str(result.get("end_date", "")),
            "gee_band": str(result.get("band", "")),
            "gee_reducer": str(result.get("reducer", "")),
            "gee_roi_source": roi_source,
            "gee_provenance_path": provenance_path,
        },
    )


@mcp.tool(name="gee.status")
def gee_status(project_id: str | None = None) -> dict:
    """
    Check Earth Engine availability/authentication status.
    """
    try:
        parsed = GeeStatusInput(project_id=project_id)
        result = scrub_secrets(gee_status_impl(project_id=parsed.project_id))
        if result.get("ok"):
            prov_path = _write_provenance(
                workspace_root=Path.cwd(),
                tool_name="gee.status",
                payload={"project_id": parsed.project_id},
                result=result,
            )
            result["provenance_path"] = prov_path
        return result
    except ValidationError as exc:
        return _tool_error_to_dict(ValueError(str(exc)))
    except Exception as exc:
        log.error("gee.status failed: %s", exc)
        return _tool_error_to_dict(exc)


@mcp.tool(name="gee.preview_layer")
def gee_preview_layer(
    session_id: str | None = None,
    project_id: str | None = None,
    dataset_preset: str | None = None,
    dataset_id: str = "",
    band: str = "",
    start_date: str = "",
    end_date: str = "",
    roi: str = "current_map_basin",
    reducer: str = "sum",
    visualization: dict[str, Any] | None = None,
) -> dict:
    """
    Create a GEE tile layer and add it to the AI-Hydro map panel.
    """
    try:
        normalized_session_id = _normalize_session_id(session_id) if session_id else None
        parsed = GeePreviewLayerInput(
            session_id=normalized_session_id,
            project_id=project_id,
            dataset_preset=dataset_preset,
            dataset_id=dataset_id,
            band=band,
            start_date=start_date,
            end_date=end_date,
            roi=roi,
            reducer=reducer,  # type: ignore[arg-type]
            visualization=visualization,
        )
        roi_contract = _resolve_roi_contract(parsed.roi, parsed.session_id)
        preset, resolved_dataset_id, resolved_band = _resolve_dataset(
            dataset_preset=parsed.dataset_preset,
            dataset_id=parsed.dataset_id,
            band=parsed.band,
            allow_default_preview=True,
        )
        if preset:
            preset.validate_spatial_reducer(parsed.reducer)

        result = gee_preview_layer_impl(
            dataset_id=resolved_dataset_id,
            band=resolved_band,
            start_date=parsed.start_date,
            end_date=parsed.end_date,
            roi_geojson=roi_contract.geometry,
            reducer=parsed.reducer,
            visualization=(
                parsed.visualization.model_dump()
                if parsed.visualization
                else (preset.default_visualization if preset else None)
            ),
            project_id=parsed.project_id,
        )
        result = scrub_secrets(result)
        if not result.get("ok"):
            return result

        workspace_root = _workspace_root(parsed.session_id)
        prov_payload = {
            "dataset_id": resolved_dataset_id,
            "band": resolved_band,
            "start_date": parsed.start_date,
            "end_date": parsed.end_date,
            "reducer": parsed.reducer,
            "roi": roi_contract.to_dict(),
            "roi_source": roi_contract.source,
            "scale_m": None,
            "output_path": None,
        }
        prov_record = ProvenanceRecord(
            provenance_id=f"prov_{stable_json_hash(prov_payload)[:16]}",
            tool_name="gee.preview_layer",
            operation="preview_layer",
            dataset_preset_id=preset.preset_id if preset else None,
            dataset_id=resolved_dataset_id,
            band=resolved_band,
            date_range={"start_date": parsed.start_date, "end_date": parsed.end_date},
            reducer=parsed.reducer,
            roi=roi_contract.to_dict(),
            input_parameters=prov_payload,
            runtime=scrub_secrets(result.get("runtime", {})),
        )
        prov_path = _write_provenance(
            workspace_root=workspace_root,
            tool_name="gee.preview_layer",
            payload=prov_record.to_dict(),
            result=result,
        )
        layer = LiveLayer(
            layer_id=f"gee_{stable_json_hash(result)[:16]}",
            layer_type="gee_tile",
            name=str(result.get("name", "GEE layer")),
            dataset_preset_id=preset.preset_id if preset else None,
            dataset_id=resolved_dataset_id,
            band=resolved_band,
            tile_url_template=str(result.get("tile_url_template") or result.get("tile_url") or ""),
            bounds_wgs84=list(result.get("bounds_wgs84") or [-180.0, -60.0, 180.0, 84.0]),
            legend={
                "visualization": (
                    parsed.visualization.model_dump()
                    if parsed.visualization
                    else (preset.default_visualization if preset else {})
                ),
                "units": preset.units if preset else None,
            },
            roi=roi_contract.to_dict(),
            provenance_path=prov_path,
            metadata={
                "start_date": parsed.start_date,
                "end_date": parsed.end_date,
                "reducer": parsed.reducer,
            },
        )
        _emit_gee_tile_layer(layer, roi_source=roi_contract.source)
        result["live_layer"] = layer.to_dict()
        result["provenance_record"] = prov_record.to_dict()
        result["provenance_path"] = prov_path
        return result
    except ValidationError as exc:
        return _tool_error_to_dict(ValueError(str(exc)))
    except Exception as exc:
        log.error("gee.preview_layer failed: %s", exc)
        return _tool_error_to_dict(exc)


@mcp.tool(name="gee.extract_timeseries")
def gee_extract_timeseries(
    session_id: str | None = None,
    project_id: str | None = None,
    dataset_preset: str | None = None,
    dataset_id: str = "",
    band: str = "",
    start_date: str = "",
    end_date: str = "",
    roi: str = "current_map_basin",
    spatial_reducer: str = "mean",
    temporal_aggregation: str = "daily",
    scale_m: float = 5000.0,
    output_name: str | None = None,
) -> dict:
    """
    Extract basin time series from a GEE ImageCollection and save CSV.
    """
    try:
        normalized_session_id = _normalize_session_id(session_id) if session_id else None
        parsed = GeeExtractTimeseriesInput(
            session_id=normalized_session_id,
            project_id=project_id,
            dataset_preset=dataset_preset,
            dataset_id=dataset_id,
            band=band,
            start_date=start_date,
            end_date=end_date,
            roi=roi,
            spatial_reducer=spatial_reducer,  # type: ignore[arg-type]
            temporal_aggregation=temporal_aggregation,  # type: ignore[arg-type]
            scale_m=scale_m,
            output_name=output_name,
        )
        roi_contract = _resolve_roi_contract(parsed.roi, parsed.session_id)
        preset, resolved_dataset_id, resolved_band = _resolve_dataset(
            dataset_preset=parsed.dataset_preset,
            dataset_id=parsed.dataset_id,
            band=parsed.band,
        )
        if preset:
            preset.validate_spatial_reducer(parsed.spatial_reducer)
            preset.validate_temporal_aggregation(parsed.temporal_aggregation)
        result = gee_extract_timeseries_impl(
            dataset_id=resolved_dataset_id,
            band=resolved_band,
            start_date=parsed.start_date,
            end_date=parsed.end_date,
            roi_geojson=roi_contract.geometry,
            spatial_reducer=parsed.spatial_reducer,
            temporal_aggregation=parsed.temporal_aggregation,
            scale_m=parsed.scale_m,
            project_id=parsed.project_id,
        )
        result = scrub_secrets(result)
        if not result.get("ok"):
            return result

        rows = result.get("rows", [])
        if not isinstance(rows, list):
            rows = []

        workspace_root = _workspace_root(parsed.session_id)
        out_dir = workspace_root / "outputs" / "gee"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        file_name = parsed.output_name or f"gee_timeseries_{stamp}.csv"
        if not file_name.endswith(".csv"):
            file_name = f"{file_name}.csv"
        csv_path = out_dir / file_name

        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["date", "value"])
            writer.writeheader()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                writer.writerow({"date": row.get("date", ""), "value": row.get("value", "")})

        row_count = len(rows)
        prov_payload = {
            "dataset_id": resolved_dataset_id,
            "band": resolved_band,
            "start_date": parsed.start_date,
            "end_date": parsed.end_date,
            "reducer": parsed.spatial_reducer,
            "roi": roi_contract.to_dict(),
            "roi_source": roi_contract.source,
            "scale_m": parsed.scale_m,
            "output_path": str(csv_path),
            "temporal_aggregation": parsed.temporal_aggregation,
        }
        summary = {
            "row_count": row_count,
            "start_date": parsed.start_date,
            "end_date": parsed.end_date,
            "spatial_reducer": parsed.spatial_reducer,
            "temporal_aggregation": parsed.temporal_aggregation,
        }
        if rows:
            values = [r.get("value") for r in rows if isinstance(r, dict) and isinstance(r.get("value"), (int, float))]
            if values:
                summary.update({
                    "min": min(values),
                    "max": max(values),
                    "mean": sum(values) / len(values),
                })
        summary_path = out_dir / f"{csv_path.stem}_summary.json"
        summary_path.write_text(json.dumps(scrub_secrets(summary), indent=2), encoding="utf-8")

        prov_record = ProvenanceRecord(
            provenance_id=f"prov_{stable_json_hash(prov_payload)[:16]}",
            tool_name="gee.extract_timeseries",
            operation="extract_timeseries",
            dataset_preset_id=preset.preset_id if preset else None,
            dataset_id=resolved_dataset_id,
            band=resolved_band,
            date_range={"start_date": parsed.start_date, "end_date": parsed.end_date},
            reducer=parsed.spatial_reducer,
            temporal_aggregation=parsed.temporal_aggregation,
            roi=roi_contract.to_dict(),
            output_paths=[str(csv_path), str(summary_path)],
            input_parameters=prov_payload,
            runtime=scrub_secrets(result.get("runtime", {})),
        )
        prov_path = _write_provenance(
            workspace_root=workspace_root,
            tool_name="gee.extract_timeseries",
            payload=prov_record.to_dict(),
            result=result,
        )
        artifact = AnalysisArtifact(
            artifact_id=f"artifact_{stable_json_hash(str(csv_path))[:16]}",
            artifact_type="csv",
            path=str(csv_path),
            name=csv_path.name,
            dataset_preset_id=preset.preset_id if preset else None,
            dataset_id=resolved_dataset_id,
            band=resolved_band,
            row_count=row_count,
            summary=summary,
            provenance_path=prov_path,
            content_hash=stable_json_hash(rows),
        )
        return {
            "ok": True,
            "type": "gee_timeseries",
            "analysis_artifact": artifact.to_dict(),
            "csv_path": str(csv_path),
            "summary_path": str(summary_path),
            "summary": summary,
            "row_count": row_count,
            "provenance": {
                **(result.get("provenance") or {}),
                "tool_name": "gee.extract_timeseries",
                "provenance_path": prov_path,
            },
            "provenance_record": prov_record.to_dict(),
            "provenance_path": prov_path,
        }
    except ValidationError as exc:
        return _tool_error_to_dict(ValueError(str(exc)))
    except Exception as exc:
        log.error("gee.extract_timeseries failed: %s", exc)
        return _tool_error_to_dict(exc)
