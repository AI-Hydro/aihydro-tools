# Global watershed delineation (pour points)

AI-Hydro delineates watersheds from latitude/longitude via the MCP tools in **aihydro-tools** (not the stale `AI-Hydro/python/` tree).

## Tools

| Tool | When to use |
|------|-------------|
| `delineate_watershed` | USGS 8-digit gauge → NLDI / NHDPlus |
| `delineate_watershed_from_point` | Any global pour point (lat/lon) |
| `merit_ensure_basin` | Prefetch MERIT vector/raster data for a basin |
| `merit_ensure_routing_region` | Check/stage flowdir-first MERIT regional routing cache |
| `merit_ensure_basins_region` | Check/stage regional MERIT-Basins vectors for large-basin hybrid overflow |
| `delineation_doctor` | Check GEE, `pyflwdir`, and MERIT runtime readiness |

## Install

```bash
cd aihydro-tools
# Fast tier (cloud DEM + pysheds) — works on Python 3.13
pip install -e ".[delineation]"

# Accurate MERIT-Basins tier (adds upstream-delineator from GitHub)
pip install -e ".[delineation-full]"
```

On Python 3.13, the main `analysis` extra no longer pulls `xrspatial` (no wheel).
Use `analysis-legacy` on Python 3.9–3.12 if you need TWI (`compute_twi`).

Restart the MCP server after installing.

## Methods (`delineate_watershed_from_point`)

- **`auto`** (default): **CONUS** — NLDI indexed basin first. **Global** — tiny GEE MERIT `upa/wth` snap-reference first; cached regional MERIT flowdir + local `pyflwdir` when available; otherwise current GEE MERIT + `pyflwdir` for small one-off requests. If GEE/MERIT fails, AI-Hydro falls back to explicitly flagged raw DEM routing.
- **`nldi`**: CONUS NLDI only.
- **`merit_gee`**: Global small/medium one-off tier. Fetches MERIT Hydro `dir`, `upa`, and `wth` bands from Google Earth Engine, snaps outlet on MERIT drainage evidence, then delineates locally with `pyflwdir`.
- **`local_merit`**: Uses a cached regional MERIT flow-direction raster with `pyflwdir`. Published accumulation rasters are optional/expert-only and are not required for normal local routing. Interactive adaptive-window routing is capped by the current measured safe envelope.
- **`merit_basins`**: Uses the new MERIT-Basins hybrid overflow route when staged regional catchments are available: vector topology assembles upstream catchments, and local MERIT flowdir refines only the terminal outlet catchment. Falls back to the older expert upstream-delineator adapter only when the hybrid route is unavailable.
- **`dem_raw_fallback`**: Raw DEM + pysheds fallback. Kept for experimental/offline recovery, not the research-grade global default.
- **`fast`**: Backward-compatible alias for `dem_raw_fallback`.

## MERIT data layout

Root for local MERIT data: `AIHYDRO_MERIT_DIR` or `~/.aihydro/merit/`

```
merit/
  shp/basins_level2/merit_hydro_vect_level2.shp
  shp/merit_rivers/riv_pfaf_##_MERIT_Hydro_v07_Basins_v01.shp
  shp/merit_catchments/cat_pfaf_##_...
  raster/flowdir_basins/flowdir##.tif
  raster/upstream_area_local/upa_local_##.tif   # optional derived cache
  raster/accum_basins/accum##.tif              # optional published/expert compatibility
```

Download from [reachhydro MERIT-Basins](https://www.reachhydro.org/home/params/merit-basins) or set `AIHYDRO_MERIT_BASE_URL` to a lab mirror.

Flowdir-first regional cache check:

```bash
# MCP tool: merit_ensure_routing_region(lat, lon, acquisition_policy="check_only")
# check_only reports readiness and staging need without triggering a large download.
```

Hybrid MERIT-Basins vector cache check:

```bash
# MCP tool: merit_ensure_basins_region(pfaf_region, acquisition_policy="check_only")
# Used only for large-basin overflow; normal local routing still stages flowdir only.
```

Minimal install (level-2 index + river flowlines for your basin):

```bash
python scripts/install_merit_minimal.py --lat 40.44 --lon -86.83
# or: --pfaf 77,78,73,74
```

```bash
python scripts/merit-download-basin.py --lat 40.44 --lon -86.83
```

## Map integration

- Successful delineation pushes a watershed layer via `map_events` (same as gauge delineation).
- In the map UI, click a point → **Delineate watershed** in the inspector → emits `delineation.requested` for the agent to run `delineate_watershed_from_point`.

## Accuracy expectations

| Basin size | Typical global-tier expectation |
|------------|-------------------------|
| Medium/large basins | MERIT GEE + `pyflwdir`; compare polygon area against outlet `upa` and expected area when available |
| Small/headwater basins | Flagged lower confidence when below MERIT effective resolution |
| Large / dammed / complex | Use quality flags; try `local_merit` or `merit_basins` if local data is installed |

MERIT Hydro / MERIT-Basins outputs carry citation and license notes. AI-Hydro treats MERIT as a research default; downstream reports should preserve the returned `citation`, `license`, `routing_dataset`, `routing_data_source`, `area_validation`, and `quality_flags` fields.

Global Watershed Delineation v1 is stable as of 2026-05-26. The stable production order is: NLDI for valid CONUS/gauge cases, tiny GEE MERIT `upa/wth` snapping, cached regional MERIT flowdir adaptive routing inside the safe envelope, MERIT-Basins hybrid overflow for large cases with staged vectors, and raw DEM only as an experimental fallback.

Important current limitation: the default GEE path uses synchronous raster downloads. Small/medium basins can be fast after cache, but some basins exceed Earth Engine `getPixels` memory limits during adaptive window expansion. Those cases should use cached local flowdir or MERIT-Basins hybrid overflow, not raw DEM.

Quality flags are part of the scientific contract:

| Flag | Meaning |
|------|---------|
| `AREA_DIFFERS_FROM_EXPECTED` | Delineated area differs from the supplied/reference area by more than the tolerance. |
| `MERIT_UPA_AREA_MISMATCH` | Polygon area disagrees with MERIT upstream-area at the snapped outlet. |
| `OUTLET_SNAP_FAR` | Outlet had to move more than 5 km to match the drainage/area evidence; treat as lower confidence. |
| `BASIN_TOUCHES_WINDOW_EDGE` | Fetched raster window may truncate the basin. |
| `GEE_VALIDATION_UNAVAILABLE` | Cached local flowdir ran without official GEE `upa/wth` validation. |
| `REGIONAL_FLOWDIR_STAGING_REQUIRED` | A regional flowdir cache is needed before reliable local MERIT routing. |
| `RAW_DEM_EXPERIMENTAL_FALLBACK` | Raw DEM routing was used; this is lower-confidence recovery, not the production global method. |
| `ADAPTIVE_WINDOW_EXPANDED` | Local staged-flowdir routing needed a larger window than the initial estimate. |
| `BASIN_TOUCHES_ROUTING_WINDOW_BOUNDARY` | Basin still reaches the local routing window boundary; do not treat as clean. |
| `ADAPTIVE_WINDOW_LIMIT_REACHED` | Adaptive local routing exceeded the configured size/iteration envelope. |
| `LOCAL_ROUTING_MEMORY_RISK` | Estimated/measured memory use is near or above the safe raster-only policy. |
| `HYBRID_ROUTING_RECOMMENDED` | Raster-window routing is not the right execution mode; use future MERIT-Basins hybrid traversal. |
| `HYBRID_ROUTING_REQUIRED` | The adaptive raster window is outside the configured safe envelope; do not continue raster expansion. |
| `HYBRID_ROUTING_USED` | MERIT-Basins topology + terminal raster refinement produced the result. |
| `MERIT_BASINS_NOT_STAGED` | Regional MERIT-Basins vectors are missing for the requested Pfaf region. |
| `TERMINAL_CATCHMENT_REFINEMENT_FAILED` | Hybrid vector assembly succeeded but terminal local raster refinement could not be applied. |
| `HYBRID_AREA_VALIDATION_MISMATCH` | Hybrid polygon area disagrees with official MERIT upstream-area validation. |

Local staged-flowdir results also report adaptive-window provenance:
`execution_mode`, `regional_flowdir_file_size_bytes`,
`window_expansion_iterations`, `final_window_bounds`,
`final_window_cell_count`, `basin_touched_window_boundary`,
`window_complete`, `peak_memory_mb`, `runtime_seconds`, and
`fallback_history`.

Safe-envelope provenance is reported as
`safe_envelope_version="benchmark_2026-05-26_v1"`. Current provisional
thresholds are 60M cells / 600 MB RSS delta for ordinary interactive local
raster routing and 120M cells / 1.5 GB RSS delta for explicit scientific
non-interactive runs. Results above the interactive threshold should carry
`HYBRID_ROUTING_RECOMMENDED`; results above the scientific threshold or with
an incomplete window must use hybrid routing or return `HYBRID_ROUTING_REQUIRED`.

Hybrid outputs add: `method_used="merit_basins_hybrid"`,
`execution_mode="local_vector_topology_terminal_raster_refinement"`,
`terminal_catchment_id`, `upstream_catchment_count`,
`terminal_refinement_used`, `vector_assembly_area_km2`,
`refined_polygon_area_km2`, and `safe_envelope_version`.

## V1 live validation gate (2026-05-26)

Pfaf 74 and 77 MERIT-Basins vectors were staged from the ReachHydro Google Drive MERIT_Hydro_v07_Basins_v01 source. Manifests:

- `/Users/mgalib/.aihydro/merit/metadata/basins_74.json`
- `/Users/mgalib/.aihydro/merit/metadata/basins_77.json`

Live gate results:

| Case | Route | Area error vs NLDI | IoU vs NLDI | MERIT UPA error | Runtime | RSS delta | Notes |
|------|-------|-------------------:|------------:|----------------:|--------:|----------:|-------|
| Nebraska / Pfaf 74 | hybrid | 0.257% | 0.978 | 0.145% | 11.8 s | 38 MB | Hybrid terminal refinement exactly matched adaptive local (`IoU ~ 1.0`) and corrected vector-only area from 126.6 to 114.0 km². |
| Sacramento / Pfaf 77 | hybrid | 2.299% | 0.832 | 0.131% | 9.6 s | 568 MB | Interactive adaptive local correctly stopped at 116M cells; scientific adaptive took 66.5 s and matched hybrid geometry (`IoU 0.99988`). |

Acceptance status: v1 complete. Hybrid terminal refinement succeeded in both staged regions, Pfaf 77 demonstrated overflow benefit, and vector/raster provenance is recorded independently in result fields and local manifests.

Do **not** import the removed `hydrocatch` package — use MCP tools above.

## Validation summary (US basins, May 2026)

Run: `python scripts/validate_delineation_basins.py`

| Method | When to use | Typical area error vs published drainage |
|--------|-------------|----------------------------------------|
| `delineate_watershed(gauge_id)` | US gauge known | **Best** — often &lt; 5% (e.g. Lopez 4%, Elwha 0.1%) |
| `delineate_watershed_from_point` `auto` (CONUS) | Lat/lon only | NLDI first; MERIT GEE global tier if NLDI is unavailable or fails validation |
| Map **Quick delineate** | Lat/lon only | Uses `method=auto`; global default is MERIT Hydro via GEE + `pyflwdir` |
| `method=merit_gee` | Global | Pass `expected_area_km2` for area-targeted outlet snapping and validation |
| `method=dem_raw_fallback` | Recovery/experiment | Raw DEM + pysheds; not the production global default |

### Why errors happen

1. **Wrong COMID** — `comid_byloc` often lands on a tributary (e.g. Mattawamkeag 5 km² vs 774 km²). Mitigation: `expected_area_km2` triggers network search for a better COMID.
2. **DEM ≠ NHD** — raw DEM routing can disagree with conditioned hydrography. Mitigation: global default now uses MERIT Hydro flow direction; prefer NLDI in CONUS when available.
3. **Large DEM nodata** — median fill over voids can break local drainage. Check logs for nodata warnings.
4. **Some NWIS sites** — occasional NLDI `get_basins(site_id)` parse failures; gauge tool falls back to COMID at station coordinates.

Run experiments: `python scripts/experiment_delineation_snap.py`

CAMELS-US batch (stratified sample, reference ``area_gages2``):

```bash
python scripts/validate_delineation_camels.py --n 25
python scripts/validate_delineation_camels.py --n 25 --experiment-worst 3
```

**Agent rule:** If the user gives a USGS gauge ID, always call `delineate_watershed` first — do not substitute pour-point DEM or MERIT delineation for gauge polygons.
