# Map ROI workspace boundary — 2026-09-09

## Reproduced code boundary
`ai_hydro/mcp/helpers.py::_resolve_active_roi_geojson` reads global `~/.aihydro/map_session.json` and returns active ROI without comparing its workspaceRoot to the requested HydroSession.workspace_dir. Thus a different open workspace can silently determine an analysis basin. Explicit working-geometry/workspace-pointer failures are also swallowed before falling through to another geometry. `tools_map.map_get_state(session_id)` combines globally retained map display state with a session-specific resolution without an explicit ownership status.

## Authorized implementation batch (Sol)
Preserve existing session/workspace geometry priority and canonical storage; require positively matching normalized workspace ownership before using a global host ROI. Unknown or conflicting ownership cannot supply a study basin. Distinguish a selected-but-unreadable/malformed explicit geometry from absence: fail explicitly rather than silently substitute another basin. Expose ownership status in map_get_state when a study is requested without claiming global layers/events are session-owned. Do not introduce a new store or infer session identity from basin names.

During implementation, tracing showed that GEE `current_map_basin` bypassed this
resolver and always read the cached session watershed. This batch therefore also
routes that selector through the shared boundary. The legacy coarse
`ROIContract.source` remains compatible, while additive `selection_source`
records the exact resolver source without claiming that a host ROI was drawn.

## Acceptance
Temporary-file tests cover matching workspace, different workspace, missing ownership, explicit selected geometry failure, and safe fallback to the requested session watershed when no explicit selection exists. Existing map/tools tests pass. Never read/write the user's actual global map state in tests; mock the home/persistence boundaries. Unit tests prove routing/ownership, not geometry scientific adequacy. Record limitations: host workspace switching, asynchronous save/load and global persistence ordering require a separate host slice.

## Outcome

Implemented normalized, positive workspace ownership for host ROI resolution;
relative or missing ownership is unknown and cannot select a basin. Explicit
working geometry and workspace pointers now fail on missing, unreadable,
malformed, or non-GeoJSON targets instead of falling through. Absolute paths and
symlinks remain valid. `map_get_state(session_id)` labels global display ROI,
catalog, and events separately from requested-session resolution and reports
`matching`, `conflicting`, `unknown`, or `not_requested` ownership.

GEE `current_map_basin` now uses the same resolver. Its ROI provenance retains
the exact `selection_source`; host state uses the neutral legacy source
`geojson`, not the unproven `map_drawn`. The six focused Map/GEE test modules
pass: 52 tests. Scientific geometry-type/topology adequacy remains downstream.
Host workspace switching, in-flight save/load ordering, and global persistence
serialization remain outside this tools-side boundary.
