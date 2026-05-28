# GEE Contracts Design

Date: 2026-05-20

## Principle

GEE tools return AI-Hydro contracts, not raw Earth Engine objects or package-specific helper objects.

## Contracts

- `ROIContract`: explicit analysis geometry with source, CRS, bbox, area, geometry hash, and timestamps.
- `DatasetPreset`: hydrology-aware dataset metadata, valid reducers, valid temporal aggregations, units, citations, and limitations.
- `LiveLayer`: map-ready layer metadata for AI-Hydro Map.
- `AnalysisArtifact`: durable output metadata for CSV, GeoTIFF, JSON, PNG, or summaries.
- `ReportBundle`: structured input for HTML Preview templates.
- `ProvenanceRecord`: reproducibility record for tool calls, inputs, ROI, outputs, and runtime context.
- `ExportTaskRecord`: durable GEE export task metadata for future asynchronous exports.
- `WorkflowRun`: multi-step workflow envelope for future curated workflows.

## Hard ROI Rule

No GEE preview or extraction may run with ambiguous ROI.

If `roi="current_map_basin"` cannot resolve to a valid `ROIContract`, return:

`No active basin geometry found. Draw or load a basin in the map first.`

## Security Rule

Contract output and provenance must not contain OAuth tokens, refresh tokens, authorization headers, passwords, credential file paths, or private auth files.

## Output Rule

Successful GEE operations write under `outputs/gee/`:

- provenance JSON,
- CSV for timeseries,
- summary JSON where relevant,
- future export task records.

## Compatibility

Existing MCP tool names remain stable:

- `gee.status`
- `gee.preview_layer`
- `gee.extract_timeseries`

These tools may keep legacy top-level fields for compatibility, but the canonical payloads are `live_layer`, `analysis_artifact`, and `provenance_record`.
