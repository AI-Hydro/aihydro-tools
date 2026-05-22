# Global watershed delineation (pour points)

AI-Hydro delineates watersheds from latitude/longitude via the MCP tools in **aihydro-tools** (not the stale `AI-Hydro/python/` tree).

## Tools

| Tool | When to use |
|------|-------------|
| `delineate_watershed` | USGS 8-digit gauge → NLDI / NHDPlus |
| `delineate_watershed_from_point` | Any global pour point (lat/lon) |
| `merit_ensure_basin` | Prefetch MERIT vector/raster data for a basin |

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

- **`auto`** (default): **CONUS** — fast NLDI indexed basin (~2–5 s) with mainstem COMID when no `expected_area_km2`; falls back to cloud DEM if NLDI area is out of range. **Global** — cloud DEM + pysheds; escalates to MERIT-Basins if checks fail. With `expected_area_km2`, NLDI searches the network for a matching COMID.
- **`fast`**: DEM tier only (~30s typical).
- **`merit_basins`**: Requires local MERIT-Basins files + `upstream-delineator`.

## MERIT data layout

Root: `AIHYDRO_MERIT_DIR` or `~/.aihydro/merit/`

```
merit/
  shp/basins_level2/merit_hydro_vect_level2.shp
  shp/merit_rivers/riv_pfaf_##_MERIT_Hydro_v07_Basins_v01.shp
  shp/merit_catchments/cat_pfaf_##_...
  raster/flowdir_basins/*pfaf_##*.tif
  raster/accum_basins/*pfaf_##*.tif
```

Download from [reachhydro MERIT-Basins](https://www.reachhydro.org/home/params/merit-basins) or set `AIHYDRO_MERIT_BASE_URL` to a lab mirror.

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

| Basin size | Typical fast-tier error |
|------------|-------------------------|
| &lt; 5,000 km² | Often &lt; 5% vs published area |
| Large / dammed | Use `merit_basins` or nudge outlet |

Do **not** import the removed `hydrocatch` package — use MCP tools above.

## Validation summary (US basins, May 2026)

Run: `python scripts/validate_delineation_basins.py`

| Method | When to use | Typical area error vs published drainage |
|--------|-------------|----------------------------------------|
| `delineate_watershed(gauge_id)` | US gauge known | **Best** — often &lt; 5% (e.g. Lopez 4%, Elwha 0.1%) |
| `delineate_watershed_from_point` `auto` (CONUS) | Lat/lon only | Cloud DEM + MERIT snap (NLDI only if `expected_area_km2` set) |
| Map **Quick delineate** | Lat/lon only | Uses `method=auto` (NLDI in CONUS, ~seconds; DEM fallback ~25–90 s) |
| `method=fast` (DEM + snaps) | Global; CONUS uses NLDI when area matches | Pass `expected_area_km2` for COMID disambiguation + area-target DEM snap; USGS gauges → `delineate_watershed(site_id)` |

### Why errors happen

1. **Wrong COMID** — `comid_byloc` often lands on a tributary (e.g. Mattawamkeag 5 km² vs 774 km²). Mitigation: `expected_area_km2` triggers network search for a better COMID.
2. **DEM ≠ NHD** — NASADEM routing disagrees with NHDPlus (typical ~15–25% area difference). Mitigation: area-target accumulation snap; prefer NLDI in CONUS when area matches.
3. **Large DEM nodata** — median fill over voids can break local drainage. Check logs for nodata warnings.
4. **Some NWIS sites** — occasional NLDI `get_basins(site_id)` parse failures; gauge tool falls back to COMID at station coordinates.

Run experiments: `python scripts/experiment_delineation_snap.py`

CAMELS-US batch (stratified sample, reference ``area_gages2``):

```bash
python scripts/validate_delineation_camels.py --n 25
python scripts/validate_delineation_camels.py --n 25 --experiment-worst 3
```

**Agent rule:** If the user gives a USGS gauge ID, always call `delineate_watershed` first — do not substitute pour-point DEM delineation for gauge polygons.
