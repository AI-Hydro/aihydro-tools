# Flood Inundation — RALP Status

> Updated by the autonomous lead loop after each iteration.

| Field | Value |
|-------|-------|
| **Current phase** | **4 complete** — maintenance / optional polish |
| **Last iteration** | 2026-06-17 — Iteration 15 |
| **Next todo** | Cognaterra globe (optional); SWE-GNN graph surrogate (deferred) |
| **Reference basin** | USGS `01031500` (Penobscot) |
| **Blockers** | Full SFINCS mesh automation deferred; proxy benchmark when solver absent |

## Phase exit checklist

| Phase | Gate | Status |
|-------|------|--------|
| 0 | HAND spike + knowledge | **PASS** |
| 1 | map tool + HRB B-061–B-067 + scrubber | **PASS** |
| 2 | Hydrograph animation + GFM live + exposure + time slider | **PASS** (GEE optional) |
| 3 | 2D physics validate-tier job + B-070–B-072 + surrogate B-078–B-079 | **PASS** (proxy when SFINCS absent) |
| 4 | 3D mesh + Terrarium + cinematic playback (B-073–B-077) | **PASS** |

## Iteration 15 (2026-06-17)
- **`train_inundation_surrogate`**: async morphology-baseline job (tuned dilation vs physics masks).
- **`get_inundation_surrogate_result`**: reads `surrogate_model.json` metrics.
- **`inundation_surrogate_runner.py`**: validate-tier subprocess; caches to session.
- **HRB B-079** morphology train contract (CSI gain on synthetic fixture).

## Iteration 14 (2026-06-17)
- **`inundation_surrogate.py`**: RLE mask codec + dataset JSON export for HAND → physics/proxy pairs.
- **`compute_physics_validation_artifacts`**: shared mask path for jobs and export.
- **`validation_masks.npz`**: saved on physics validation job completion.
- **`export_inundation_surrogate_dataset`**: MCP tool (physics job, artifact dir, or synthetic).
- **HRB B-078** surrogate dataset contract.
- **Docs**: `SURROGATE.md`.

## Iteration 13 (2026-06-17)
- **`terrain_vertical_metadata`**: EGM96-lite geoid offset for CONUS 3DEP orthometric mesh vs Terrarium ellipsoid.
- **Manifest fields**: `mesh_vertical_datum`, `terrain_vertical_offset_m`, `terrain_vertical_note`.
- **MapView**: applies offset to water mesh Z when Terrarium terrain toggle is on.
- **HRB B-077** vertical offset contract.

## Iteration 12 (2026-06-17)
- **D8 upstream fix**: `_upstream_predecessors` uses `(row-dr, col-dc)` (was inverted).
- **`merit_flowline` camera**: `primary_flowline_coords` + arc-length sampling when stack has `flowline_geojson`.
- **`try_attach_merit_flowline`**: auto-attach clipped MERIT rivers before 3D manifest write.
- **`camera_path_source`**: `merit_flowline` | `flowdir_main_stem` | `bounds_major_axis`.
- **HRB B-076** MERIT flowline camera bench.

## Iteration 11 (2026-06-17)
- **`trace_main_stem_cells`**: D8 upstream trace from HAND stack → cinematic camera path.
- **`camera_path_source`**: `flowdir_main_stem` vs `bounds_major_axis` in manifest.
- **`WORKFLOW.md`** + **workflow v2** (hydrograph, physics, 3D optional steps).
- **Lead skill** tools list updated for Phase 2–4 tools.
- **HRB B-075** stem camera path bench.

## Iteration 10 (2026-06-17)
- **`build_camera_path`**: manifest keyframes per hydrograph frame (major-axis traverse).
- **Play/Pause hydrograph** in 🌊 panel with deck.gl camera transitions.
- **Cinematic camera toggle** follows embedded `camera_path` or client fallback.
- **HRB B-074** camera path contract.

## Iteration 9 (2026-06-17)
- **Terrarium `TerrainLayer`** + Esri hillshade when 3D terrain toggle on.
- **Hydrograph frame sync**: extent polygons + shared scrubber in 🌊 and 🕐 panels.
- **Camera**: fit AOI + pitch 45° on 3D enable.
- Manifest `terrain_hint: terrarium_hillshade_client`.

## Iteration 8 (2026-06-17)
- **`inundation_3d.py`**: decimated water-surface mesh + manifest writer.
- **`push_inundation_3d_manifest`**: map event type `inundation_3d`.
- **`MapEventWatcher`**: inlines manifest + frame meshes for webview CSP.
- **MapView**: optional 3D toggle (pitch + `SimpleMeshLayer`), hides flat HAND rasters when on.
- **`Inundation3DControls`**: 🌊 ribbon section.
- **HRB B-073** mesh export contract.

## Iteration 7 (2026-06-17)
- **`inundation_physics.py`**: backend probe, HAND vs physics/proxy benchmark, shared report contract.
- **`inundation_physics_runner.py`**: async validate-tier subprocess (caches to `session.inundation_physics`).
- **`tools_inundation_physics.py`**: `run_inundation_physics_validation` + `get_inundation_physics_result`.
- **`docs/flood_inundation/PHYSICS.md`**: benchmark tier documentation.
- **HRB B-070–B-072** physics validation benches.

## Iteration 6 (2026-06-17)
- **`aihydro_data/flood/gfm_stac.py`**: live EODC STAC search + `ensemble_flood_extent` COG → GeoJSON (empty AOI = valid live result).
- **`inundation_exposure.py`**: zonal population raster + optional WorldPop GEE (`use_worldpop=True` on tool).
- **`map_flood_inundation_hydrograph`**: optional per-frame extent polygons (`push_extent_polygons=True`).
- **HRB B-069** zonal population bench.
