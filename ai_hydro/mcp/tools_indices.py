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
    """Compute a spectral index (NDWI, NDVI, NDBI, NBR, MNDWI, …) for the
    current study watershed.

    Fetches the required optical bands from ``sensor``, applies cloud masking
    (if ``mask_clouds=True``), and computes a cloud-free median composite over
    ``[start, end]``.

    With ``frequency="monthly"`` or ``"yearly"``, returns a temporal stack with
    per-period statistics and a Mann-Kendall trend slope (useful for detecting
    drought, water-body expansion, or urbanisation trends).

    Parameters
    ----------
    index_name : str
        Index to compute.  Call ``list_spectral_indices()`` to discover
        available indices and their required bands.  Case-insensitive.
        Examples: ``"NDWI"``, ``"ndvi"``, ``"NBR"``, ``"MNDWI"``.
    session_id : str, optional
        Research session identifier.  Auto-resolved from chat context if
        omitted (same resolution as all other analysis tools).
    start : str, optional
        Start date ISO-8601 (``"YYYY-MM-DD"``).  Defaults to one year before
        ``end`` (or one year before today when both are omitted).
    end : str, optional
        End date ISO-8601.  Defaults to today.
    sensor : str
        Source sensor.  Supported: ``"sentinel2"`` (default), ``"landsat8"``,
        ``"landsat9"``, ``"modis_mod09"``.  The required bands are resolved
        from ``SENSOR_BAND_MAPS`` automatically.
    frequency : str or None
        ``None`` (default) — single median composite over the full date range.
        ``"monthly"`` — one composite per month; returns time-series stats.
        ``"yearly"`` — one composite per year; returns time-series stats.
    mask_clouds : bool
        Apply sensor-appropriate cloud masking before computing the index
        (default ``True``).  Set ``False`` only if the input data is already
        cloud-free.
    create_map : bool
        Push the index raster to the map panel (default ``True``).
    native_resolution : bool
        Force the sensor's native resolution (10–20 m for Sentinel-2, 30 m for
        Landsat) regardless of basin size.  By default (``False``) very large
        basins are auto-coarsened so the single GEE request stays under the
        ~48 MB download cap.  With ``True`` the basin is downloaded as a grid
        of full-resolution tiles and mosaicked — no coarsening, at the cost of
        several extra round-trips (slower for large basins).

    Returns
    -------
    dict with:

    - ``index_name``   — normalised index name (upper-case)
    - ``data``         — ``{mean, median, std, p10, p25, p75, p90, valid_px}``
    - ``colormap``     — matplotlib colormap from ``INDEX_REGISTRY``
    - ``citation``     — original paper citation string
    - ``use_case``     — one-line use-case description
    - ``threshold_hint`` — typical threshold for binary mapping
    - ``_files_saved`` — list of saved file paths (GeoTIFF, PNG)
    - ``_map_layer``   — map layer id pushed (or ``None``)
    - ``next_steps``   — suggested follow-on tools

    In **time-series mode**, additional keys are present:

    - ``time_axis``          — list of ISO-8601 period strings
    - ``period_means``       — per-period mean values
    - ``trend_slope_per_year`` — Mann-Kendall-compatible linear slope
    - ``p_value``            — Mann-Kendall p-value (None if pymannkendall unavailable)
    """
    from ai_hydro.mcp.helpers import _resolve_session, _maybe_set_workspace
    from ai_hydro.session import HydroSession

    try:
        session_id = _resolve_session(session_id, None)
        session = HydroSession.load(session_id)
        _maybe_set_workspace(session)
        workspace = session.workspace_dir

        if not workspace:
            _fallback_ws = Path.home() / ".aihydro" / "sessions" / session_id / "outputs"
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

        # ── Mann-Kendall trend (time-series mode) ────────────────────────
        trend_slope = None
        trend_p = None
        if time_axis and period_stats:
            try:
                means = [s["mean"] for s in period_stats if s["mean"] is not None]
                if len(means) >= 4:
                    trend_slope, trend_p = _mann_kendall_slope(means)
            except Exception as _mke:
                log.debug("Mann-Kendall failed (non-fatal): %s", _mke)

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
        if time_axis:
            d["time_axis"] = time_axis
            d["period_means"] = [s.get("mean") for s in period_stats]
            d["trend_slope_per_year"] = trend_slope
            d["p_value"] = trend_p

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
            # Time-series mode: skip failed periods silently

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


def _mann_kendall_slope(series: list[float]) -> tuple[float | None, float | None]:
    """Compute Mann-Kendall trend slope (Theil-Sen) and p-value.

    Uses ``pymannkendall`` if available, otherwise falls back to a simple
    linear-regression slope as a proxy.
    """
    try:
        import pymannkendall as mk
        result = mk.original_test(series)
        return float(result.slope), float(result.p)
    except ImportError:
        pass
    # Linear regression fallback
    try:
        n = len(series)
        x = np.arange(n, dtype=float)
        slope = float(np.polyfit(x, series, 1)[0])
        return slope, None
    except Exception:
        return None, None
