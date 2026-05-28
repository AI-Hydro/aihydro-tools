# GEE Backend Implementation Plan

Date: 2026-05-20

## Strategy

`aihydro-tools` is the canonical backend for GEE computation, validation, presets, provenance, and MCP tool contracts.

The AI-Hydro VS Code extension should remain a UI and rendering layer. HTML Preview should consume saved artifacts and report bundles, not call GEE directly.

## Implemented In This Phase

- Added formal GEE contracts in `ai_hydro/gee/contracts.py`.
- Added hydrology-aware dataset presets in `ai_hydro/gee/presets.py`.
- Updated `gee.status` to scrub secret-adjacent fields before returning/writing provenance.
- Updated `gee.preview_layer` to resolve ROI to `ROIContract`, validate presets, return `LiveLayer`, and write provenance.
- Updated `gee.extract_timeseries` to resolve ROI to `ROIContract`, validate presets, write CSV and summary JSON, return `AnalysisArtifact`, and write provenance.
- Deferred `geetools` dependency adoption.

## Next Backend Steps

1. Add land-cover categorical fraction implementation for NLCD and ESA WorldCover.
2. Add static DEM summary workflow for SRTM.
3. Add export task tools: `gee.export_raster`, `gee.list_tasks`, `gee.task_status`, `gee.cancel_task`, and `gee.export_manifest`.
4. Add report bundle generation for HTML Preview templates.
5. Gradually remove duplicated scientific behavior from the VS Code extension once it calls these backend contracts directly.

## Non-Goals

- Do not add `geetools`, `geemap`, `eemont`, `wxee`, `geedim`, `hydra-floods`, or `taskee` as dependencies in this phase.
- Do not make HTML Preview execute GEE operations.
- Do not expose GEE credentials, tokens, OAuth files, or credential paths.
