"""
Spectral index MCP tools — v0.2.0 (TorchGeo cherry-pick Day 5).

Exposes ``compute_spectral_index`` as a tier-2 MCP tool for ad-hoc and chained
spectral index computation workflows.  Built on top of:

  - ``aihydro_data.transforms.indices`` — 10 numpy-native index formulas +
    ``INDEX_REGISTRY`` + ``SENSOR_BAND_MAPS``
  - ``aihydro_data.transforms.cloud_mask`` — sensor-appropriate cloud masking
  - ``aihydro_data.fetch`` — cache-aware multi-source data retrieval
  - ``ai_hydro.analysis.plots.plot_raster_tile`` — colormap-registry-aware
    visualization (auto-tile-pyramid for > 8 M pixel rasters)
  - ``ai_hydro.mcp.map_events.push_raster_layer`` — map overlay

Single-composite mode (``frequency=None``)
  Fetches all required bands for ``[start, end]``, optionally masks clouds,
  computes a median composite, evaluates the index, and saves GeoTIFF + PNG.

Time-series mode (``frequency="monthly"`` or ``"yearly"``)
  Fetches bands per period, stacks into a temporal DataArray, computes the
  index per timestep, derives per-timestep statistics, and returns a Mann-
  Kendall trend slope and p-value for change detection.

Both modes register the result in the session under the ``"index_<NAME>"``
slot so subsequent tools can access it without re-fetching.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import numpy as np

from ai_hydro.registry.paths import aihydro_home
from ai_hydro.mcp.app import mcp, Context

log = logging.getLogger(__name__)


@mcp.tool()
async def compute_spectral_index(
    index_name: str,
    session_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    sensor: str = "sentinel2",
    frequency: str | None = None,
    mask_clouds: bool = True,
    create_map: bool = True,
    native_resolution: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Compute a spectral index (NDWI, NDVI, NDBI, NBR, MNDWI, ...) for the study watershed.

    Fetches bands from `sensor`, cloud-masks them (mask_clouds) and returns a median composite
    over [start, end]. With frequency="monthly"/"yearly" it returns a temporal stack with
    per-period stats and a Mann-Kendall-compatible trend slope.

    Parameters
    ----------
    index_name : str
        Case-insensitive, e.g. "NDWI", "ndvi". list_spectral_indices() lists all and their bands.
    session_id : str, optional
        Auto-resolved from the chat context when omitted.
    start, end : str, optional
        ISO dates. end defaults to today; start to one year before end.
    sensor : str
        "sentinel2" (default), "landsat8", "landsat9", "modis_mod09".
    frequency : str or None
        None = one composite; "monthly" or "yearly" = time series.
    mask_clouds : bool
        Default True; False only for already cloud-free input.
    create_map : bool
        Push the raster to the map panel (default True).
    native_resolution : bool
        True keeps the native 10-30 m by tiling large basins (extra round-trips); False
        auto-coarsens big basins to stay under the ~48 MB GEE download cap.

    Returns
    -------
    dict: index_name, data {mean, median, std, p10, p25, p75, p90, valid_px}, colormap, citation,
    use_case, threshold_hint, _files_saved, _map_layer, next_steps. Time-series mode adds
    time_axis, period_means, trend_slope_per_year (index units/YEAR, Theil-Sen), p_value, trend,
    n_periods_requested/served, periods_failed (a failed period is a gap).
    """
    from ai_hydro.mcp.helpers import _resolve_session, _maybe_set_workspace
    from ai_hydro.session import HydroSession

    try:
        session_id = _resolve_session(session_id, None)
        session = HydroSession.load(session_id)
        _maybe_set_workspace(session)
        workspace = session.workspace_dir

        if not workspace:
            _fallback_ws = aihydro_home() / "sessions" / session_id / "outputs"
            _fallback_ws.mkdir(parents=True, exist_ok=True)
            workspace = str(_fallback_ws)

        if ctx:
            await ctx.report_progress(progress=0, total=10)

        # ── Resolve index metadata from INDEX_REGISTRY ──────────────────
        from aihydro_data.transforms.indices import INDEX_REGISTRY, SENSOR_BAND_MAPS
        idx_key = index_name.upper()
        if idx_key not in INDEX_REGISTRY:
            available = ", ".join(sorted(INDEX_REGISTRY.keys()))
            return {
                "error": f"Unknown index '{index_name}'. Available: {available}",
                "code": "UNKNOWN_INDEX",
                "recovery": "Call list_spectral_indices() for a list of supported indices.",
                "next_tools": ["list_spectral_indices"],
            }
        reg_entry = INDEX_REGISTRY[idx_key]
        colormap = reg_entry.get("colormap", "viridis")
        citation = reg_entry.get("citation", "")
        use_case = reg_entry.get("use_case", "")
        threshold_hint = reg_entry.get("threshold_hint", None)
        required_bands = reg_entry.get("bands", [])  # e.g. ["green", "nir"]

        # ── Check session cache ──────────────────────────────────────────
        slot_name = f"index_{idx_key.lower()}"
        cached = session.get(slot_name)
        if cached is not None and frequency is None:
            log.debug("compute_spectral_index: returning cached %s for session %s",
                      idx_key, session_id)
            _req = cached.get("requested")
            _now = {"sensor": sensor, "start": start, "end": end}
            _diff = {k: {"cached": _req.get(k), "requested": v} for k, v in _now.items()
                     if isinstance(_req, dict) and v is not None and _req.get(k) is not None and _req.get(k) != v}
            if _diff:
                # Never return the stale index as the answer to a different request.
                return {
                    "error": True,
                    "code": "CACHED_INDEX_PARAMS_MISMATCH",
                    "message": f"The session's cached {idx_key} was computed with different parameters: {_diff}. "
                               "It was not returned.",
                    "recovery": f"Call clear_session('{session_id}', ['index_{idx_key.lower()}']) and call "
                                "compute_spectral_index again.",
                    "next_tools": ["clear_session", "compute_spectral_index"],
                    "cache_mismatch": _diff,
                }
            cached["_cached"] = True
            return cached

        # ── Date range ───────────────────────────────────────────────────
        from datetime import date, timedelta
        today = date.today()
        if end is None:
            end = today.isoformat()
        if start is None:
            start = (today.replace(year=today.year - 1)).isoformat()

        if ctx:
            await ctx.report_progress(progress=2, total=10)

        # ── Resolve geometry ─────────────────────────────────────────────
        from ai_hydro.mcp.tools_analysis import _resolve_session_geometry
        from shapely.geometry import shape as _shape
        watershed_geojson = _resolve_session_geometry(session_id, None)
        watershed_shapely = _shape(watershed_geojson)

        # ── Fetch data ───────────────────────────────────────────────────
        # Strategy: try fetching the index directly as a variable
        # (fast path for NDVI / NDWI which have products registered).
        # On error / missing bands, fall back to band-by-band fetch.
        result = await asyncio.to_thread(
            _fetch_and_compute_index,
            idx_key=idx_key,
            sensor=sensor,
            required_bands=required_bands,
            watershed=watershed_shapely,
            start=start,
            end=end,
            frequency=frequency,
            apply_cloud_mask=mask_clouds,
            reg_entry=reg_entry,
            native_resolution=native_resolution,
        )

        if ctx:
            await ctx.report_progress(progress=8, total=10)

        if "error" in result:
            return result

        index_da = result["index_da"]
        time_axis = result.get("time_axis")
        period_stats = result.get("period_stats", [])

        # ── Statistics ───────────────────────────────────────────────────
        valid = index_da.values[np.isfinite(index_da.values)]
        if valid.size == 0:
            return {
                "error": "All index pixels are NaN — likely cloud cover or no valid data.",
                "code": "NO_VALID_PIXELS",
                "recovery": "Try a different date range or set mask_clouds=False.",
                "next_tools": ["compute_spectral_index"],
            }
        stats = {
            "mean": float(np.mean(valid)),
            "median": float(np.median(valid)),
            "std": float(np.std(valid)),
            "p10": float(np.percentile(valid, 10)),
            "p25": float(np.percentile(valid, 25)),
            "p75": float(np.percentile(valid, 75)),
            "p90": float(np.percentile(valid, 90)),
            "valid_px": int(valid.size),
        }

        # ── Save GeoTIFF + PNG ────────────────────────────────────────────
        from ai_hydro.mcp.helpers import _canonical_prefix as _cp
        prefix = _cp(session_id, f"index_{idx_key.lower()}")
        files_saved: list[str] = []
        map_layer_id: str | None = None

        try:
            # GeoTIFF
            tif_path = Path(workspace) / f"{prefix}.tif"
            try:
                index_da.rio.to_raster(str(tif_path), driver="GTiff", compress="lzw")
                files_saved.append(str(tif_path))
            except Exception as _te:
                log.warning("GeoTIFF save failed: %s", _te)

            # Bounds for map overlay
            try:
                from rasterio.warp import transform_bounds
                src_crs = index_da.rio.crs
                raw_bounds = index_da.rio.bounds()
                if src_crs and str(src_crs) not in ("EPSG:4326", "4326"):
                    wgs84_bounds = list(
                        transform_bounds(src_crs, "EPSG:4326", *raw_bounds)
                    )
                else:
                    wgs84_bounds = list(raw_bounds)
            except Exception:
                x = index_da.x.values
                y = index_da.y.values
                wgs84_bounds = [float(x.min()), float(y.min()),
                                float(x.max()), float(y.max())]

            # PNG tile (auto-pyramid if large)
            if create_map:
                from ai_hydro.analysis.plots import plot_raster_tile
                tile_result = plot_raster_tile(
                    index_da.values,
                    bounds_wgs84=wgs84_bounds,
                    output_dir=workspace,
                    name=prefix,
                    colormap=colormap,
                    index_name=idx_key,
                    watershed=watershed_shapely,
                )
                if tile_result is not None:
                    png_path, _ = tile_result
                    files_saved.append(png_path)

                    # Push to map panel
                    from ai_hydro.mcp.map_events import push_raster_layer
                    layer_id = f"index_{idx_key.lower()}_{session_id}"
                    push_raster_layer(
                        layer_id=layer_id,
                        name=f"{idx_key} — {session_id}",
                        png_path=png_path,
                        bounds_wgs84=wgs84_bounds,
                        colormap=colormap,
                        metadata={
                            "index": idx_key,
                            "sensor": sensor,
                            "start": start,
                            "end": end,
                            "citation": citation,
                            "use_case": use_case,
                            "threshold_hint": str(threshold_hint) if threshold_hint else "",
                        },
                    )
                    map_layer_id = layer_id

        except Exception as _ve:
            log.warning("Visualization failed (non-fatal): %s", _ve)

        # ── Trend (time-series mode) ─────────────────────────────────────
        # Fitted on the real time axis (period midpoints in decimal years), so a
        # period that failed is a gap, not a neighbour. Units: index units / year.
        trend = None
        period_failures = result.get("period_failures", [])
        if time_axis and period_stats:
            trend = _trend_per_year(period_stats)

        if ctx:
            await ctx.report_progress(progress=10, total=10)

        # ── Assemble response ────────────────────────────────────────────
        d: dict = {
            "index_name": idx_key,
            "data": stats,
            "colormap": colormap,
            "citation": citation,
            "use_case": use_case,
            "threshold_hint": threshold_hint,
            "_files_saved": files_saved,
            "_map_layer": map_layer_id,
            "next_steps": [
                "Threshold the index raster (values > threshold_hint = positive class).",
                "Chain with compute_spectral_index for another index to compare.",
                "Use export_session to save results.",
            ],
        }
        d["requested"] = {"sensor": sensor, "start": start, "end": end, "frequency": frequency}
        if time_axis:
            d["time_axis"] = time_axis
            d["period_means"] = [s.get("mean") for s in period_stats]
            d["trend_slope_per_year"] = trend["slope_per_year"] if trend else None
            d["p_value"] = trend["p_value"] if trend else None
            d["trend"] = trend
            d["n_periods_requested"] = result.get("n_periods_requested", len(time_axis))
            d["n_periods_served"] = len(period_stats)
            d["periods_failed"] = period_failures

        # ── Store in session ─────────────────────────────────────────────
        try:
            from ai_hydro.mcp.helpers import _session_store
            _session_store(session_id, slot_name, d, tool_name="compute_spectral_index")
        except Exception as _se:
            log.debug("session store failed (non-fatal): %s", _se)

        return d

    except Exception as exc:
        log.exception("compute_spectral_index failed: %s", exc)
        return {
            "error": str(exc),
            "code": "COMPUTATION_ERROR",
            "recovery": (
                "Verify the watershed is delineated, dates are valid "
                "(YYYY-MM-DD), and the sensor is one of: sentinel2, landsat8, "
                "landsat9, modis_mod09."
            ),
            "next_tools": ["delineate_watershed_from_point", "list_spectral_indices"],
        }


@mcp.tool()
def list_spectral_indices() -> dict:
    """List all available spectral indices, their required bands, colormaps,
    citations, and recommended use cases.

    Returns a compact discovery manifest that agents can use to choose the
    right index without trial-and-error.  Each entry also carries a
    ``threshold_hint`` — the typical threshold value for binary classification
    (e.g. NDWI > 0.3 → open water).
    """
    try:
        from aihydro_data.transforms.indices import INDEX_REGISTRY, SENSOR_BAND_MAPS
        indices = {}
        for name, entry in INDEX_REGISTRY.items():
            indices[name] = {
                "bands": entry.get("bands", []),
                "colormap": entry.get("colormap", "viridis"),
                "range": list(entry.get("range", (-1, 1))),
                "citation": entry.get("citation", ""),
                "use_case": entry.get("use_case", ""),
                "threshold_hint": entry.get("threshold_hint", None),
            }
        return {
            "indices": indices,
            "sensors": list(SENSOR_BAND_MAPS.keys()),
            "n_indices": len(indices),
        }
    except ImportError:
        return {
            "error": "aihydro-data not installed.",
            "recovery": "pip install aihydro-data",
            "indices": {},
        }


# ---------------------------------------------------------------------------
# Internal fetch + compute helpers
# ---------------------------------------------------------------------------

def _fetch_and_compute_index(
    idx_key: str,
    sensor: str,
    required_bands: list[str],
    watershed,
    start: str,
    end: str,
    frequency: str | None,
    apply_cloud_mask: bool,
    reg_entry: dict,
    native_resolution: bool = False,
) -> dict:
    """Fetch required bands, optionally mask clouds, compute the index.

    Returns dict with ``index_da`` (xr.DataArray) and optionally
    ``time_axis`` + ``period_stats`` for time-series mode.
    """
    import xarray as xr
    from aihydro_data.transforms.indices import compute_index, INDEX_REGISTRY

    # ── Period list ──────────────────────────────────────────────────────
    if frequency == "monthly":
        periods = _monthly_periods(start, end)
    elif frequency == "yearly":
        periods = _yearly_periods(start, end)
    else:
        periods = [(start, end)]

    # ── Sensor → optical-reflectance product ─────────────────────────────
    # We fetch the *raw multi-band reflectance composite* (variable="optical")
    # through aihydro-data's routing + fallback + cache machinery, then compute
    # ANY index locally from the bands. This is the band-by-band path that lets
    # NDWI / MNDWI / NBR / NDBI / … work — they have no pre-computed product.
    # Pinning a sensor uses mode="manual"; the policy fallback chain
    # (sentinel2 → landsat9 → landsat8) still applies on failure.
    _SENSOR_PRODUCT = {
        "sentinel2": "SENTINEL2_SR",
        "landsat8": "LANDSAT8_SR",
        "landsat9": "LANDSAT9_SR",
    }
    pinned_product = _SENSOR_PRODUCT.get(sensor.lower())

    # ── Geometry for fetch ────────────────────────────────────────────────
    # Pass the ACTUAL watershed polygon (not its bounding box) so GEE clips
    # the composite to the true basin boundary and masks every pixel outside
    # it. (Rasters are always rectangular, but .clip(polygon) sets out-of-basin
    # pixels to nodata → NaN, which is what statistics and the map overlay
    # expect.) The download-size budget still uses the bbox extent internally,
    # since the exported grid spans the bounding box regardless.
    try:
        from shapely.geometry import mapping as _mapping
        geometry_arg = _mapping(watershed)
    except Exception:
        geometry_arg = watershed

    period_das = []
    period_stats = []
    period_failures: list[dict] = []
    time_axis = [] if frequency else None

    for p_start, p_end in periods:
        try:
            import aihydro_data
            # Fetch the raw multi-band reflectance composite. The composite is
            # already cloud-masked server-side (per-pixel SCL / QA_PIXEL), so
            # we compute the index with mask_clouds_first=False.
            # Pass `index=idx_key` so GEE computes the index server-side and
            # returns a single-band DataArray (~N× more area/resolution
            # headroom). On the STAC fallback (no server-side compute) the
            # pipeline returns the raw multi-band Dataset and we compute the
            # index locally below — both paths handled by the type switch.
            if pinned_product:
                fetch_result = aihydro_data.fetch(
                    variable="optical",
                    geometry=geometry_arg,
                    start=p_start,
                    end=p_end,
                    mode="manual",
                    product=pinned_product,
                    index=idx_key,
                    native_resolution=native_resolution,
                    cache=False,   # xr.Dataset isn't disk-cache friendly; session caches the result
                )
            else:
                fetch_result = aihydro_data.fetch(
                    variable="optical",
                    geometry=geometry_arg,
                    start=p_start,
                    end=p_end,
                    index=idx_key,
                    native_resolution=native_resolution,
                    cache=False,
                )
            ds_or_da = getattr(fetch_result, "data", fetch_result)
            # The actual sensor that served the data (may differ from the
            # requested one if the fallback chain kicked in).
            served_sensor = sensor
            if isinstance(ds_or_da, xr.Dataset):
                served_sensor = ds_or_da.attrs.get("sensor", sensor)

            # Handle Dataset (raw bands) vs DataArray (already an index)
            if isinstance(ds_or_da, xr.Dataset):
                ds = ds_or_da
                ds.attrs.setdefault("sensor", served_sensor)
                idx_da = compute_index(
                    idx_key, ds=ds, sensor=served_sensor,
                    mask_clouds_first=False,   # composite already cloud-masked
                )
            elif isinstance(ds_or_da, xr.DataArray):
                idx_da = ds_or_da
            else:
                log.warning("Unexpected fetch result type %s — skipping period %s",
                            type(ds_or_da).__name__, p_start)
                continue

            # Median composite over any time dimension
            if "time" in idx_da.dims:
                idx_da = idx_da.median(dim="time")

            period_das.append(idx_da)
            if time_axis is not None:
                time_axis.append(p_start)
                valid = idx_da.values[np.isfinite(idx_da.values)]
                period_stats.append({
                    "period": p_start,
                    "period_end": p_end,
                    "mean": float(np.mean(valid)) if valid.size > 0 else None,
                    "n_pixels": int(valid.size),
                })

        except Exception as exc:
            log.warning("Fetch failed for period %s–%s: %s", p_start, p_end, exc)
            if frequency is None:
                # Single-period mode — propagate the error
                return {
                    "error": (
                        f"Could not retrieve {idx_key} data from {sensor} "
                        f"for {p_start} to {p_end}. Cause: {exc}"
                    ),
                    "code": "FETCH_ERROR",
                    "recovery": (
                        "Check that the sensor has coverage for the given "
                        "date range and that aihydro-data backends are "
                        "configured (pip install aihydro-data[gee])."
                    ),
                    "next_tools": ["list_spectral_indices", "data_list_products"],
                }
            # Time-series mode: a failed period is a GAP. It is recorded (never
            # silently dropped) and the trend is fitted on the real time axis.
            period_failures.append({"period": p_start, "period_end": p_end,
                                    "reason": str(exc)[:200]})

    if not period_das:
        return {
            "error": f"No valid {idx_key} data found for any period in [{start}, {end}].",
            "code": "NO_DATA",
            "recovery": "Widen the date range or try a different sensor.",
            "next_tools": ["list_spectral_indices"],
        }

    if len(period_das) == 1:
        final_da = period_das[0]
    else:
        # Stack periods — simple concatenation on a 'time' axis
        try:
            stacked = xr.concat(period_das, dim="time")
            final_da = stacked.median(dim="time")  # overall composite for single-value stats
        except Exception:
            final_da = period_das[-1]  # fall back to most recent

    return {
        "index_da": final_da,
        "time_axis": time_axis,
        "period_stats": period_stats,
        "period_failures": period_failures,
        "n_periods_requested": len(periods),
    }


def _monthly_periods(start: str, end: str) -> list[tuple[str, str]]:
    """Return list of (period_start, period_end) for each month."""
    from datetime import date, timedelta
    import calendar
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    periods = []
    cur = date(s.year, s.month, 1)
    while cur <= e:
        last = date(cur.year, cur.month,
                    calendar.monthrange(cur.year, cur.month)[1])
        p_start = max(cur, s)
        p_end = min(last, e)
        periods.append((p_start.isoformat(), p_end.isoformat()))
        # Advance to next month
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return periods


def _yearly_periods(start: str, end: str) -> list[tuple[str, str]]:
    """Return list of (period_start, period_end) for each year."""
    from datetime import date
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    periods = []
    for year in range(s.year, e.year + 1):
        p_start = max(date(year, 1, 1), s)
        p_end = min(date(year, 12, 31), e)
        periods.append((p_start.isoformat(), p_end.isoformat()))
    return periods


def _trend_per_year(period_stats: list[dict]) -> dict | None:
    """Theil-Sen slope and Mann-Kendall test of the period means, in index units PER YEAR.

    The abscissa is the period midpoint in decimal years, so months that failed to
    fetch leave a gap instead of shifting later months one step closer, and a
    monthly series is not mislabelled (a slope per period is 12x a slope per year).

    Estimator (always the same, never a fallback): ``scipy.stats.theilslopes``
    (median of pairwise slopes) and ``scipy.stats.kendalltau`` of value against
    time (the Mann-Kendall test; tie-corrected normal approximation). Neither
    removes the seasonal cycle nor corrects for serial correlation; both are
    stated in the result. Returns None when fewer than 4 periods have a mean.
    """
    from datetime import date as _date
    from scipy import stats as _st

    t, y = [], []
    for r in period_stats:
        m = r.get("mean")
        if m is None or not np.isfinite(m):
            continue
        a = _date.fromisoformat(r["period"])
        b = _date.fromisoformat(r.get("period_end") or r["period"])
        mid = a.toordinal() + (b.toordinal() - a.toordinal()) / 2.0
        t.append(mid / 365.2425)
        y.append(float(m))
    if len(y) < 4:
        return None
    slope = float(_st.theilslopes(y, t)[0])
    p = float(_st.kendalltau(t, y).pvalue)
    note = ("Seasonal cycle not removed" if len(y) > 12 else "Short series")
    return {
        "slope_per_year": slope,
        "p_value": p,
        "unit": "index units per year",
        "estimator": "Theil-Sen (scipy.stats.theilslopes) on period midpoints in decimal years",
        "test": "Mann-Kendall via scipy.stats.kendalltau (tie-corrected, no serial-correlation correction)",
        "n_periods_used": len(y),
        "seasonality_removed": False,
        "caveat": f"{note}; the p-value assumes independent observations.",
    }
