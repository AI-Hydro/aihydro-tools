# GEE Backend Audit

Date: 2026-05-20
Repository: `/Users/mgalib/Documents/AI-Hydro/MCP/aihydro-tools`
Branch: `feature/gee-backend-contracts`

## Summary

`aihydro-tools` is already the right home for canonical GEE computation. The repo has MCP tool registration, session/provenance helpers, map event publishing, and an initial `ai_hydro.gee` adapter. The main risk is duplicated GEE behavior between this backend and the VS Code extension adapter.

## Repository Findings

- MCP server entrypoint: `ai_hydro/mcp/__init__.py` imports tool modules for registration, and `ai_hydro/mcp/app.py` owns the shared FastMCP instance and tool tier registry.
- Community plugin system: `ai_hydro/mcp/registry.py` discovers tools from the `aihydro.tools` entry point group.
- Current GEE MCP tools: `ai_hydro/mcp/tools_gee.py` registers `gee.status`, `gee.preview_layer`, and `gee.extract_timeseries`.
- Current GEE adapter modules: `ai_hydro/gee/auth.py`, `ai_hydro/gee/cli.py`, `ai_hydro/gee/map_layers.py`, and `ai_hydro/gee/timeseries.py`.
- Existing session geometry helper: `ai_hydro/mcp/helpers.py::_get_session_geometry`.
- Existing map bridge: `ai_hydro/mcp/map_events.py` writes map events for the VS Code extension watcher.
- Existing tests: `tests/test_mcp_gee_tools.py`, `tests/test_gee_cli.py`, `tests/test_mcp_integration.py`.
- Current optional dependency group: `pyproject.toml` has `[project.optional-dependencies].gee = ["earthengine-api>=0.1.390"]`.

## Duplication Risk

The VS Code extension has a local GEE adapter path for toolbar commands and webview routing. That path is useful as UI glue, but scientific behavior must converge here:

- dataset selection
- hydrology-aware reducer validation
- ROI validation
- provenance
- timeseries extraction semantics
- export/task contracts

The extension should call/consume backend contracts instead of developing separate scientific rules.

## Security Findings

GEE status responses previously included runtime metadata that could contain local credential paths. New backend contract outputs and provenance must scrub token, authorization, password, refresh, and credential file/path fields.

## Current Gaps

- No formal contract classes for ROI, live layers, artifacts, reports, provenance, export tasks, or workflow runs.
- `current_map_basin` resolves directly to GeoJSON rather than an explicit ROI contract.
- Dataset semantics are generic; precipitation, land cover, DEM, NDVI, and temperature need different validation rules.
- `gee.extract_timeseries` writes CSV but does not yet return a formal artifact contract.
- Export/task monitoring is not implemented yet.
