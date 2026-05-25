"""
Analysis MCP tools (9 tools).

Watershed delineation, streamflow, signatures, geomorphic parameters,
TWI, curve number grid, forcing data, CAMELS-US attributes, and library reference.

Tool parameter conventions
--------------------------
session_id : str
    Research session identity — any string (slug, UUID, gauge ID used as a
    shorthand, or anything meaningful to the study). Keyed in HydroSession.
    Auto-generated "hydro-<8hex>" if not supplied.

gauge_id : str  (USGS-specific data tools only)
    8-digit USGS station number, e.g. '01031500'. Only required by tools that
    fetch data from USGS NWIS / NLDI (delineate_watershed, fetch_streamflow_data).
    After the first USGS call the gauge ID is stored in session.site_id so
    subsequent tools can resolve it automatically.

Source-agnostic analysis tools (extract_hydrological_signatures,
extract_geomorphic_parameters, compute_twi, create_cn_grid, fetch_forcing_data)
have NO gauge_id parameter — they work on session geometry and time-series data
regardless of where that data came from.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from ai_hydro.mcp.app import mcp, Context
from ai_hydro.mcp.enforcement import post_run as _post_run
from ai_hydro.mcp.helpers import (
    _cached_response,
    _canonical_workspace_path,
    _ensure_session,
    _get_session_geometry,
    _normalize_session_id,
    _result_to_dict,
    _session_store,
    _strip_forcing_arrays,
    _sync_reminder,
    _tool_error_to_dict,
    _validate_usgs_gauge_id,
    _workspace_write,
)


def _canonical_fname(session_id: str, prefix: str, ext: str = "json") -> str:
    """Return the session's canonical workspace filename, with a safe
    fallback to the legacy session-id-based name if resolution fails."""
    return _canonical_workspace_path(session_id, prefix, ext) or f"{prefix}_{session_id}.{ext}"


def _canonical_prefix(session_id: str, kind: str) -> str:
    """Return the bare canonical filename prefix (no extension), used as the
    ``output_prefix`` for analysis modules that build several files of their
    own (TWI raster + map + PNG; CN raster + NetCDF + map; ...).

    Equivalent to ``f"{kind}_{session.canonical_id}"`` when the session is
    loadable, with a safe fallback to the session-id form on failure.
    """
    full = _canonical_workspace_path(session_id, kind, "json")
    if full and full.endswith(".json"):
        return full[: -len(".json")]
    return f"{kind}_{session_id}"

log = logging.getLogger("ai_hydro.mcp")


def _bounds_to_wgs84(bounds: list, crs_str: str) -> list:
    """
    Convert [west, south, east, north] bounds to EPSG:4326 if needed.
    Falls back to returning bounds unchanged if pyproj is unavailable or
    CRS is already geographic.
    """
    try:
        from pyproj import CRS, Transformer
        src_crs = CRS.from_user_input(crs_str or "EPSG:4326")
        if src_crs.is_geographic:
            return bounds  # already lat/lon
        wgs84 = CRS.from_epsg(4326)
        transformer = Transformer.from_crs(src_crs, wgs84, always_xy=True)
        west, south = transformer.transform(bounds[0], bounds[1])
        east, north = transformer.transform(bounds[2], bounds[3])
        return [west, south, east, north]
    except Exception:
        return bounds  # best-effort fallback


def _resolve_usgs_gauge(session_id: str, gauge_id: str | None, session) -> str:
    """
    Resolve the 8-digit USGS station number for a session.

    Resolution order:
    1. ``gauge_id`` parameter (explicit)
    2. ``session.site_id`` (set by a previous USGS tool call)
    3. ``session_id`` itself, if it passes USGS format validation (backward compat)

    Raises ValueError with a clear recovery message if none of the above work.
    """
    if gauge_id:
        return _validate_usgs_gauge_id(gauge_id)
    if session.site_id:
        try:
            return _validate_usgs_gauge_id(session.site_id)
        except ValueError:
            pass
    # Backward compat: if the caller used the gauge ID as session_id
    try:
        return _validate_usgs_gauge_id(session_id)
    except ValueError:
        pass
    raise ValueError(
        f"No USGS gauge_id found for session '{session_id}'. "
        "Pass gauge_id='01031500' (8-digit USGS station number) explicitly. "
        "Find gauge IDs at https://waterdata.usgs.gov/"
    )


# ============================================================================
# Tool: Watershed Delineation
# ============================================================================

@mcp.tool()
def delineate_watershed(
    session_id: str,
    gauge_id: str | None = None,
    workspace_dir: str | None = None,
) -> dict:
    """
    Delineate USGS gauge watershed via NLDI + NWIS metadata. Stores polygon
    in session and sets session.site_id so downstream tools auto-resolve it.

    gauge_id: 8-digit USGS station (e.g. '01031500'). Optional if a previous
    call already set session.site_id, or if session_id itself is a USGS id.
    workspace_dir: pass once, remembered for all future calls.

    Returns area_km2, gauge_name/lat/lon, huc_02. Geometry stays in session.
    """
    try:
        session_id = _normalize_session_id(session_id)
        session = _ensure_session(session_id, workspace_dir)
        resolved_gauge_id = _resolve_usgs_gauge(session_id, gauge_id, session)

        # Cache hit — skip the expensive USGS API call
        if session.watershed is not None:
            ws_data = session.watershed["data"]
            compact = {k: v for k, v in ws_data.items() if k != "geometry_geojson"}
            files_saved: list[str] = []
            # Ensure workspace copy exists; try path-ref first, then legacy inline geojson
            geojson_path_on_disk = ws_data.get("geometry_geojson_path")
            geojson = None
            if geojson_path_on_disk and Path(geojson_path_on_disk).exists():
                with open(geojson_path_on_disk) as _f:
                    geojson = json.load(_f)
            else:
                geojson = ws_data.get("geometry_geojson")
            if geojson and session.workspace_dir:
                ws_geojson = (
                    Path(session.workspace_dir) / f"watershed_{resolved_gauge_id}.geojson"
                )
                if not ws_geojson.exists():
                    saved = _workspace_write(
                        session_id, f"watershed_{resolved_gauge_id}.geojson", geojson
                    )
                    if saved:
                        files_saved.append(saved)
            return {
                "data": compact,
                "meta": session.watershed.get("meta", {}),
                "_cached": True,
                "_workspace_dir": session.workspace_dir,
                "_files_saved": files_saved or None,
                "_note": (
                    "Watershed already in session — GeoJSON on disk, "
                    "downstream tools ready. Call clear_session to recompute."
                ),
            }

        from ai_hydro.analysis.watershed import delineate_watershed as _fn
        result = _fn(gauge_id=resolved_gauge_id)
        d = _result_to_dict(result)
        geojson = d["data"]["geometry_geojson"]
        files_saved = []

        # Always save geometry to ~/.aihydro/sessions/<session_id>.geojson so the
        # path is stable and independent of workspace_dir. This keeps the session
        # JSON lean (stores a path, not 200-800 KB of coordinates).
        from ai_hydro.session.store import _SESSIONS_DIR
        sessions_geojson = _SESSIONS_DIR / f"{session_id}.geojson"
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        with open(sessions_geojson, "w") as _f:
            json.dump(geojson, _f)
        files_saved.append(str(sessions_geojson))

        # Replace full geojson with path reference in session slot
        d_lean = dict(d)
        d_lean["data"] = {
            **{k: v for k, v in d["data"].items() if k != "geometry_geojson"},
            "geometry_geojson_path": str(sessions_geojson),
        }
        _session_store(session_id, "watershed", d_lean, tool_name="delineate_watershed")

        # Persist gauge metadata in session so downstream tools don't need gauge_id
        from ai_hydro.session import HydroSession
        _sess_upd = HydroSession.load(session_id)
        if not _sess_upd.site_id:
            _sess_upd.site_id = resolved_gauge_id
            _sess_upd.site_type = "usgs_gauge"
            _sess_upd.save()

        # Also write to workspace for user-visible copy
        ws = session.workspace_dir or workspace_dir
        if ws:
            saved = _workspace_write(
                session_id, f"watershed_{resolved_gauge_id}.geojson", geojson
            )
            if saved:
                files_saved.append(saved)
            # Watershed boundary map PNG
            from ai_hydro.analysis.plots import plot_watershed_map
            png = plot_watershed_map(
                geojson=geojson,
                gauge_lat=d["data"].get("gauge_lat", 0.0),
                gauge_lon=d["data"].get("gauge_lon", 0.0),
                gauge_name=d["data"].get("gauge_name", ""),
                output_dir=ws,
                gauge_id=resolved_gauge_id,
            )
            if png:
                files_saved.append(png)

        # Push watershed boundary to map panel (non-fatal if VS Code not open)
        from ai_hydro.mcp.map_events import push_layer, push_gauge_point
        geojson_for_map = geojson if geojson else {}
        push_layer(
            layer_id=f"watershed_{resolved_gauge_id}",
            name=f"Watershed: {d['data'].get('gauge_name', resolved_gauge_id)}",
            geojson=geojson_for_map,
            layer_type="polygon",
            style_preset="watershed",
            auto_zoom=True,
            open_map=True,
            metadata={
                "gauge_id": resolved_gauge_id,
                "area_km2": str(round(d["data"].get("area_km2", 0), 1)),
                "source": "USGS NLDI",
            },
        )
        lat = d["data"].get("gauge_lat")
        lon = d["data"].get("gauge_lon")
        if lat is not None and lon is not None:
            push_gauge_point(
                layer_id=f"gauge_{resolved_gauge_id}",
                name=f"Gauge: {d['data'].get('gauge_name', resolved_gauge_id)}",
                lat=lat,
                lon=lon,
                metadata={"gauge_id": resolved_gauge_id, "source": "USGS NWIS"},
            )

        # Strip geometry from agent response — it's large and already on disk
        compact = {k: v for k, v in d["data"].items() if k != "geometry_geojson"}
        resp: dict = {
            "data": compact,
            "meta": d.get("meta", {}),
            "_files_saved": files_saved,
            "_note": (
                f"geometry_geojson saved to file (not in session JSON). "
                f"gauge_id '{resolved_gauge_id}' stored in session.site_id. "
                "Downstream tools (signatures, geomorphic, twi, forcing) "
                "load it automatically — no need to pass gauge_id again. "
                "Watershed boundary and gauge point pushed to AI-Hydro map."
            ),
        }
        reminder = _sync_reminder(session_id)
        if reminder:
            resp["_sync_required"] = reminder
        return resp
    except Exception as e:
        log.error("delineate_watershed failed: %s", e)
        return _tool_error_to_dict(e)


def _watershed_slug(name: str | None, lat: float, lon: float) -> str:
    if name:
        slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip())
        slug = slug.strip("_")[:48] or "basin"
        return slug
    return f"basin_{abs(lat):.3f}_{abs(lon):.3f}".replace(".", "p")


def _store_point_watershed(
    session_id: str,
    d: dict,
    geojson: dict,
    slug: str,
    workspace_dir: str | None,
) -> list[str]:
    """Persist point-delineated watershed to session + workspace (mirrors gauge flow)."""
    files_saved: list[str] = []
    from ai_hydro.session.store import _SESSIONS_DIR

    sessions_geojson = _SESSIONS_DIR / f"{session_id}_{slug}.geojson"
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    with open(sessions_geojson, "w") as _f:
        json.dump(geojson, _f)
    files_saved.append(str(sessions_geojson))

    d_lean = dict(d)
    d_lean["data"] = {
        **{k: v for k, v in d["data"].items() if k != "geometry_geojson"},
        "geometry_geojson_path": str(sessions_geojson),
    }
    _session_store(session_id, "watershed", d_lean, tool_name="delineate_watershed_from_point")

    if workspace_dir:
        saved = _workspace_write(session_id, f"watershed_{slug}.geojson", geojson)
        if saved:
            files_saved.append(saved)
    return files_saved


@mcp.tool()
def merit_ensure_basin(
    lat: float,
    lon: float,
    download: bool = True,
) -> dict:
    """
    Ensure MERIT-Hydro vectors exist for the Pfaf basin at (lat, lon) WGS84.
    Auto-downloads if AIHYDRO_MERIT_BASE_URL is set. Returns readiness flags.
    """
    try:
        from ai_hydro.data.merit_manager import MeritDataManager

        mgr = MeritDataManager()
        status = mgr.ensure_basin(lat, lon, download=download)
        return {
            "data": {
                "pfaf_code": status.pfaf_code,
                "level2_ready": status.level2_ready,
                "rivers_ready": status.rivers_ready,
                "catchments_ready": status.catchments_ready,
                "flowdir_ready": status.flowdir_ready,
                "delineator_ready": mgr.delineator_ready(status.pfaf_code),
                "merit_root": str(mgr.root),
            },
            "message": status.message,
            "downloaded": status.downloaded,
            "_note": (
                "Install MERIT vectors from https://www.reachhydro.org/home/params/merit-basins "
                "or set AIHYDRO_MERIT_BASE_URL for automated downloads."
            ),
        }
    except Exception as e:
        log.error("merit_ensure_basin failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def merit_ensure_region(
    preset: str,
    lat: float | None = None,
    lon: float | None = None,
    download: bool = True,
) -> dict:
    """
    Ensure MERIT river vectors for all Pfaf basins in a named region preset.

    Presets: ``conus``, ``south_asia``. Requires level-2 index (install via
    ``merit_ensure_basin`` first if missing).
    """
    try:
        from ai_hydro.hydro_map_cli import cmd_merit_ensure_region
        import argparse

        args = argparse.Namespace(
            preset=preset,
            lat=lat,
            lon=lon,
            no_download=not download,
        )
        return cmd_merit_ensure_region(args)
    except Exception as e:
        log.error("merit_ensure_region failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def merit_add_map_layers(
    lat: float,
    lon: float,
    min_lon: float | None = None,
    min_lat: float | None = None,
    max_lon: float | None = None,
    max_lat: float | None = None,
    include_catchments: bool = False,
    push_to_map: bool = True,
) -> dict:
    """
    Build MERIT vector layers for the map viewport and optionally push to the map panel.

    Clips local shapefiles to the view bounds. Install vectors first with
    ``merit_ensure_basin`` or ``merit_ensure_region``.
    """
    try:
        from ai_hydro.data.merit_map_layers import merit_map_layers_for_view
        from ai_hydro.mcp.map_events import push_layer

        layers = merit_map_layers_for_view(
            lat=lat,
            lon=lon,
            min_lon=min_lon,
            min_lat=min_lat,
            max_lon=max_lon,
            max_lat=max_lat,
            include_catchments=include_catchments,
        )
        pushed: list[str] = []
        if push_to_map:
            for spec in layers:
                gj = spec["geojson"]
                geojson_str = json.dumps(gj) if isinstance(gj, dict) else str(gj)
                push_layer(
                    layer_id=spec["id"],
                    name=spec["name"],
                    geojson=geojson_str,
                    layer_type=spec.get("layer_type", "line"),
                    style_preset=spec.get("style_preset", "flowlines"),
                    auto_zoom=len(pushed) == 0,
                    open_map=len(pushed) == 0,
                    metadata=spec.get("metadata"),
                )
                pushed.append(spec["id"])

        return {
            "ok": len(layers) > 0,
            "data": {"layer_ids": [s["id"] for s in layers], "pushed": pushed},
            "message": f"Added {len(pushed)} MERIT layer(s) to map"
            if pushed
            else (f"{len(layers)} layer(s) built (not pushed)" if layers else "No layers — install MERIT vectors first"),
        }
    except Exception as e:
        log.error("merit_add_map_layers failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def delineate_watershed_from_point(
    session_id: str,
    lat: float,
    lon: float,
    workspace_dir: str | None = None,
    expected_area_km2: float | None = None,
    method: str = "auto",
    name: str | None = None,
) -> dict:
    """
    Delineate a watershed from a pour point (lat/lon, EPSG:4326). Tiered:
    cloud DEM + pysheds (fast), MERIT vector snap, MERIT-Basins via
    upstream-delineator. For USGS gauges use delineate_watershed instead.

    method: auto | fast | merit_basins
    expected_area_km2: enables validation + adaptive snapping
    Returns area_km2, method_used, pfaf_code, snap_distance_m.
    """
    try:
        session_id = _normalize_session_id(session_id)
        session = _ensure_session(session_id, workspace_dir)
        slug = _watershed_slug(name, lat, lon)
        method_norm = method.strip().lower()
        if method_norm not in ("auto", "fast", "merit_basins"):
            raise ValueError("method must be auto, fast, or merit_basins")

        from ai_hydro.analysis.delineation import delineate_from_point

        result = delineate_from_point(
            lat,
            lon,
            expected_area_km2=expected_area_km2,
            method=method_norm,  # type: ignore[arg-type]
            verbose=False,
            name=name,
        )
        d = _result_to_dict(result)
        geojson = d["data"]["geometry_geojson"]
        files_saved = _store_point_watershed(
            session_id, d, geojson, slug, session.workspace_dir or workspace_dir
        )

        from ai_hydro.session import HydroSession

        _sess = HydroSession.load(session_id)
        _sess.site_type = "pour_point"
        if name:
            _sess.site_name = name
        _sess.save()

        layer_label = name or f"Watershed ({slug})"
        from ai_hydro.mcp.map_events import push_layer, push_gauge_point

        push_layer(
            layer_id=f"watershed_{slug}",
            name=layer_label,
            geojson=geojson,
            layer_type="polygon",
            style_preset="watershed",
            auto_zoom=True,
            open_map=True,
            metadata={
                "area_km2": str(round(d["data"].get("area_km2", 0), 1)),
                "method_used": d["data"].get("method_used", method_norm),
                "source": "delineation",
                "pfaf_code": d["data"].get("pfaf_code") or "",
            },
        )
        push_gauge_point(
            layer_id=f"outlet_{slug}",
            name=f"Outlet: {layer_label}",
            lat=lat,
            lon=lon,
            metadata={"source": "pour_point"},
        )

        compact = {k: v for k, v in d["data"].items() if k != "geometry_geojson"}
        resp: dict = {
            "data": compact,
            "meta": d.get("meta", {}),
            "_files_saved": files_saved,
            "_note": (
                "Watershed stored in session; downstream tools (TWI, GEE, geomorphic) "
                "use session geometry. Map layer pushed."
            ),
        }
        reminder = _sync_reminder(session_id)
        if reminder:
            resp["_sync_required"] = reminder
        return resp
    except Exception as e:
        log.error("delineate_watershed_from_point failed: %s", e)
        return _tool_error_to_dict(e)


# ============================================================================
# Tool: Fetch Streamflow Data
# ============================================================================

@mcp.tool()
def fetch_streamflow_data(
    session_id: str,
    gauge_id: str | None = None,
    start_date: str = "",
    end_date: str = "",
    interval: str = "daily",
) -> dict:
    """
    Fetch USGS streamflow time series for a gauge.

    Daily/sub-daily discharge from USGS NWIS. JSON-serializable time series
    with FAIR provenance. gauge_id auto-resolved from session.site_id when
    omitted. Dates in YYYY-MM-DD. interval: daily (default) | hourly.
    Returns q_cms array + summary stats; full series saved to disk.
    """
    try:
        session_id = _normalize_session_id(session_id)
        from ai_hydro.session import HydroSession as _HS
        session = _HS.load(session_id)
        resolved_gauge_id = _resolve_usgs_gauge(session_id, gauge_id, session)

        # Cache-hit check — same gauge + date range + interval already computed
        if session.streamflow is not None:
            cached_params = session.streamflow.get("meta", {}).get("params", {})
            if (cached_params.get("start_date") == start_date
                    and cached_params.get("end_date") == end_date
                    and cached_params.get("interval", "daily") == interval):
                compact = _strip_forcing_arrays(session.streamflow.get("data", {}))
                return {
                    "data": compact,
                    "meta": session.streamflow.get("meta", {}),
                    "_cached": True,
                    "_note": "Streamflow already cached. Full time series on disk.",
                }

        from ai_hydro.data.streamflow import fetch_streamflow_data as _fn
        result = _fn(
            gauge_id=resolved_gauge_id,
            start_date=start_date,
            end_date=end_date,
            interval=interval,
        )
        d = _result_to_dict(result)

        # Persist gauge metadata if not already set
        if not session.site_id:
            session.site_id = resolved_gauge_id
            session.site_type = "usgs_gauge"
            session.save()

        files_saved: list[str] = []
        saved = _workspace_write(
            session_id, f"streamflow_{resolved_gauge_id}.json", d["data"]
        )
        if saved:
            files_saved.append(saved)

        # Record the data file path in the slot so downstream tools (FDC plot,
        # model training) can reload raw arrays without re-fetching from USGS.
        if saved:
            d["data"]["_data_file"] = saved
        _session_store(session_id, "streamflow", d, tool_name="fetch_streamflow_data")

        # Strip raw arrays from response — saved to disk, not needed in context
        data = d["data"]
        q_vals = data.get("q_cms", [])
        compact = {k: v for k, v in data.items()
                   if k not in ("dates", "q_cms") and not k.startswith("_")}
        if q_vals:
            valid = [v for v in q_vals if v is not None and isinstance(v, (int, float))]
            if valid:
                compact["q_mean_cms"] = round(sum(valid) / len(valid), 4)
                compact["q_max_cms"] = round(max(valid), 4)
                compact["q_min_cms"] = round(min(valid), 4)
                compact["n_missing"] = len(q_vals) - len(valid)
        if saved:
            compact["_data_file"] = saved

        # Hydrograph PNG
        ws = session.workspace_dir
        if ws and data.get("dates") and q_vals:
            from ai_hydro.analysis.plots import plot_hydrograph
            png = plot_hydrograph(
                dates=data["dates"],
                q_cms=q_vals,
                gauge_name=data.get("gauge_name", ""),
                gauge_id=resolved_gauge_id,
                output_dir=ws,
            )
            if png:
                files_saved.append(png)

        resp: dict = {
            "data": compact,
            "meta": d.get("meta", {}),
            "_files_saved": files_saved,
            "_note": (
                f"Full time series ({compact.get('n_days', '?')} records) saved to "
                f"{saved or 'session'}. Raw dates/q_cms arrays are NOT in the session JSON "
                "or this response — load from _data_file when needed."
            ),
        }
        reminder = _sync_reminder(session_id)
        if reminder:
            resp["_sync_required"] = reminder
        resp = _post_run("fetch_streamflow_data", session_id, resp)
        return resp
    except Exception as e:
        log.error("fetch_streamflow_data failed: %s", e)
        return _tool_error_to_dict(e)


# ============================================================================
# Tool: Hydrological Signatures
# ============================================================================

@mcp.tool()
def extract_hydrological_signatures(
    session_id: str,
    start_date: str = "1989-10-01",
    end_date: str = "2009-09-30",
) -> dict:
    """
    Extract 17 CAMELS-style hydrological signatures (flow stats, BFI,
    runoff ratio, elasticity, high/low flow events, FDC slope, timing).
    Requires delineate_watershed first. Defaults to CAMELS analysis period
    1989-10-01 to 2009-09-30.
    """
    try:
        session_id = _normalize_session_id(session_id)
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        if session.signatures is not None:
            return _cached_response("signatures", session)
        watershed_geojson = _get_session_geometry(session_id)
        area_km2 = session.watershed["data"]["area_km2"]
        # The underlying function fetches streamflow from USGS NWIS if not cached;
        # resolve gauge_id from session.site_id for USGS gauges.
        usgs_gauge_id = session.site_id or session_id
        from ai_hydro.analysis.signatures import extract_hydrological_signatures as _fn
        result = _fn(
            gauge_id=usgs_gauge_id,
            watershed_geojson=watershed_geojson,
            area_km2=area_km2,
            start_date=start_date,
            end_date=end_date,
        )
        d = _result_to_dict(result)
        _session_store(session_id, "signatures", d, tool_name="extract_hydrological_signatures")
        files_saved: list[str] = []
        saved = _workspace_write(
            session_id, _canonical_fname(session_id, "signatures", "json"), d["data"]
        )
        if saved:
            files_saved.append(saved)
        # FDC + signature summary PNG
        # q_cms may have been stripped from the session JSON (lean storage);
        # try reloading from the on-disk data file recorded during streamflow fetch.
        ws = session.workspace_dir
        if ws and session.streamflow:
            q_vals = session.streamflow.get("data", {}).get("q_cms")
            if not q_vals:
                data_file = session.streamflow.get("data", {}).get("_data_file")
                if data_file and Path(data_file).exists():
                    try:
                        import json as _json
                        with open(data_file) as _f:
                            q_vals = _json.load(_f).get("q_cms")
                    except Exception:
                        q_vals = None
            if q_vals:
                from ai_hydro.analysis.plots import plot_flow_duration_curve
                png = plot_flow_duration_curve(
                    q_cms=q_vals,
                    signatures=d["data"],
                    gauge_id=session_id,
                    output_dir=ws,
                )
                if png:
                    files_saved.append(png)
        d["_files_saved"] = files_saved
        reminder = _sync_reminder(session_id)
        if reminder:
            d["_sync_required"] = reminder
        d = _post_run("extract_hydrological_signatures", session_id, d)
        return d
    except Exception as e:
        log.error("extract_hydrological_signatures failed: %s", e)
        return _tool_error_to_dict(e)


# ============================================================================
# Tool: Geomorphic Parameters
# ============================================================================

@mcp.tool()
def extract_geomorphic_parameters(
    session_id: str,
    dem_resolution: int = 30,
) -> dict:
    """
    Extract 28 geomorphic parameters (morphometry, relief, drainage network,
    shape indices) from a 30m DEM via py3dep. Requires delineate_watershed
    first. Returns DA_km2, Lp_km, Lb_km, shape indices, hypsometric integral,
    drainage density, stream-order metrics, etc.
    """
    try:
        session_id = _normalize_session_id(session_id)
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        if session.geomorphic is not None:
            return _cached_response("geomorphic", session)
        watershed_geojson = _get_session_geometry(session_id)
        ws_data = session.watershed["data"]
        outlet_lat = ws_data["gauge_lat"]
        outlet_lon = ws_data["gauge_lon"]
        from ai_hydro.analysis.geomorphic import extract_geomorphic_parameters_result as _fn
        result = _fn(
            watershed_geojson=watershed_geojson,
            outlet_lat=outlet_lat,
            outlet_lon=outlet_lon,
            dem_resolution=dem_resolution,
        )
        d = _result_to_dict(result)
        _session_store(session_id, "geomorphic", d, tool_name="extract_geomorphic_parameters")
        saved = _workspace_write(
            session_id, _canonical_fname(session_id, "geomorphic", "json"), d["data"]
        )
        if saved:
            d["_file_saved"] = saved
        reminder = _sync_reminder(session_id)
        if reminder:
            d["_sync_required"] = reminder
        return d
    except Exception as e:
        log.error("extract_geomorphic_parameters failed: %s", e)
        return _tool_error_to_dict(e)


# ============================================================================
# Tool: Topographic Wetness Index
# ============================================================================

@mcp.tool()
async def compute_twi(
    session_id: str,
    resolution: int = 30,
    create_map: bool = True,
    ctx: Context | None = None,
) -> dict:
    """
    Topographic Wetness Index TWI = ln(a / tan(beta)). Used for soil
    moisture mapping, saturated zone ID, runoff generation. Requires
    delineate_watershed first. Saves GeoTIFF + PNG + HTML map when
    workspace_dir is set and create_map=True.
    """
    try:
        session_id = _normalize_session_id(session_id)
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        if session.twi is not None:
            return _cached_response("twi", session)
        watershed_geojson = _get_session_geometry(session_id)
        workspace = session.workspace_dir
        viz_failed: str | None = None

        if ctx:
            await ctx.report_progress(progress=0, total=10)

        # Try full visualization path if workspace is known and create_map requested
        if create_map and workspace:
            try:
                from shapely.geometry import shape as _shape
                watershed_shapely = _shape(watershed_geojson)
                from ai_hydro.analysis.twi import compute_twi as _fn_full

                twi_prefix = _canonical_prefix(session_id, "twi")
                result = await asyncio.to_thread(
                    _fn_full,
                    watershed_shapely,
                    resolution=resolution,
                    save_outputs=True,
                    output_dir=workspace,
                    output_prefix=twi_prefix,
                    create_visualizations=True,
                )

                if ctx:
                    await ctx.report_progress(progress=10, total=10)

                files = result.get("files_saved", [])
                _EXCLUDE = {"twi_array", "well_drained_mask", "moderate_mask", "saturated_mask"}
                stats = {k: v for k, v in result.items() if k not in _EXCLUDE}
                d = {
                    "data": {**stats, "files_saved": files},
                    "meta": {
                        "tool": "ai_hydro.analysis.twi.compute_twi",
                        "params": {"resolution": resolution, "create_map": create_map},
                    },
                }
                _session_store(session_id, "twi", d, tool_name="compute_twi")
                d["_files_saved"] = files

                # Push raster tile to map panel if TWI array + bounds are available
                try:
                    from ai_hydro.mcp.map_events import push_raster_layer
                    from ai_hydro.analysis.plots import plot_raster_tile
                    twi_arr = result.get("twi_array")
                    raw_bounds = result.get("bounds")  # native CRS bounds from rioxarray
                    # Convert bounds to WGS84 if needed
                    raw_crs = result.get("crs", "")
                    if twi_arr is not None and raw_bounds is not None:
                        bounds_wgs84 = _bounds_to_wgs84(raw_bounds, raw_crs)
                        tile_result = plot_raster_tile(
                            array=twi_arr,
                            bounds_wgs84=bounds_wgs84,
                            output_dir=workspace,
                            name=twi_prefix,
                            colormap="viridis_r",
                        )
                        if tile_result:
                            tile_path, tile_bounds = tile_result
                            push_raster_layer(
                                layer_id=twi_prefix,
                                name=f"TWI: {session_id}",
                                png_path=tile_path,
                                bounds_wgs84=tile_bounds,
                                colormap="viridis_r",
                                opacity=0.70,
                                auto_zoom=False,
                                metadata={"session_id": session_id, "source": "pysheds + py3dep"},
                            )
                except Exception as _map_err:
                    log.debug("TWI map push failed (non-fatal): %s", _map_err)

                reminder = _sync_reminder(session_id)
                if reminder:
                    d["_sync_required"] = reminder
                return d
            except Exception as viz_err:
                log.warning(
                    "TWI full computation failed, falling back to stats only: %s", viz_err
                )
                viz_failed = str(viz_err)

        # Fallback: statistics only (workspace missing, create_map=False,
        # or full computation raised a fatal error)
        from ai_hydro.analysis.twi import compute_twi_result as _fn
        result = await asyncio.to_thread(
            _fn, watershed_geojson=watershed_geojson, resolution=resolution
        )
        d = _result_to_dict(result)
        _session_store(session_id, "twi", d, tool_name="compute_twi")
        saved = _workspace_write(session_id, _canonical_fname(session_id, "twi", "json"), d["data"])
        if saved:
            d["_file_saved"] = saved
        if not workspace:
            d["_note"] = (
                "No workspace directory set — statistics only, no map files saved. "
                "Call delineate_watershed(session_id, workspace_dir=<path>) to enable file output."
            )
        elif viz_failed:
            d["_visualization_warning"] = (
                f"Full TWI computation failed ({viz_failed[:200]}); "
                "statistics computed via fallback. No map files generated."
            )

        reminder = _sync_reminder(session_id)
        if reminder:
            d["_sync_required"] = reminder

        if ctx:
            await ctx.report_progress(progress=10, total=10)

        return d
    except Exception as e:
        log.error("compute_twi failed: %s", e)
        return _tool_error_to_dict(e)


# ============================================================================
# Tool: Curve Number Grid
# ============================================================================

@mcp.tool()
async def create_cn_grid(
    session_id: str,
    year: int = 2019,
    resolution: int = 30,
    create_map: bool = True,
    ctx: Context | None = None,
) -> dict:
    """
    NRCS Curve Number grid: NLCD land cover × Polaris soil → distributed CN.
    Requires delineate_watershed first. Returns CN stats, zone percentages,
    LULC + soil breakdowns. Saves GeoTIFF / NetCDF / PNG / HTML.
    year: NLCD year (default 2019).
    """
    try:
        session_id = _normalize_session_id(session_id)
        session = _ensure_session(session_id)

        # Cache hit
        if session.cn is not None:
            cached = session.cn
            return {
                "data": cached.get("data", {}),
                "meta": cached.get("meta", {}),
                "_cached": True,
                "_workspace_dir": session.workspace_dir,
            }

        watershed_geojson = _get_session_geometry(session_id)
        workspace = session.workspace_dir or str(Path.home() / ".aihydro" / "cache")

        if ctx:
            await ctx.report_progress(progress=0, total=7)

        from shapely.geometry import shape as _shape
        from ai_hydro.analysis.curve_number import (
            create_curve_number_grid_from_geometry as _fn,
        )

        watershed_shapely = _shape(watershed_geojson)
        cn_prefix = _canonical_prefix(session_id, "cn")
        # Subfolder name mirrors the canonical prefix for tidiness
        cn_folder = cn_prefix.replace("cn_", "cn_grid_", 1)
        output_dir = str(Path(workspace) / cn_folder)

        result = await asyncio.to_thread(
            _fn,
            geometry=watershed_shapely,
            year=year,
            resolution=resolution,
            save_outputs=True,
            output_dir=output_dir,
            create_visualizations=create_map,
            output_prefix=cn_prefix,
        )

        if ctx:
            await ctx.report_progress(progress=7, total=7)

        stats = result.get("statistics", {})
        zones = result.get("cn_zones", {})
        lulc = result.get("lulc_stats", {})
        soil = result.get("soil_stats", {})
        file_paths = result.get("file_paths", {})
        ws_info = result.get("watershed_info", {})

        data = {
            **stats,
            **zones,
            "lulc_classes": lulc.get("classes", []),
            "soil_group_percentages": soil.get("soil_group_percentages", {}),
            "area_km2": ws_info.get("area_km2"),
            "files_saved": list(file_paths.values()),
        }

        d = {
            "data": data,
            "meta": {
                "tool": "ai_hydro.analysis.curve_number.create_curve_number_grid_from_geometry",
                "params": {"year": year, "resolution": resolution, "create_map": create_map},
            },
        }
        _session_store(session_id, "cn", d, tool_name="create_cn_grid")
        d["_files_saved"] = list(file_paths.values())

        # Push CN raster tile to map (non-fatal)
        try:
            cn_array = result.get("cn_array")
            cn_bounds = result.get("bounds")
            cn_crs = result.get("crs", "")
            if cn_array is not None and cn_bounds is not None:
                from ai_hydro.mcp.map_events import push_raster_layer
                from ai_hydro.analysis.plots import plot_raster_tile
                bounds_wgs84 = _bounds_to_wgs84(
                    list(cn_bounds) if not isinstance(cn_bounds, list) else cn_bounds,
                    cn_crs,
                )
                tile_result = plot_raster_tile(
                    array=cn_array,
                    bounds_wgs84=bounds_wgs84,
                    output_dir=output_dir,
                    name=cn_prefix,
                    colormap="YlOrRd",
                )
                if tile_result:
                    tile_path, tile_bounds = tile_result
                    push_raster_layer(
                        layer_id=cn_prefix,
                        name=f"Curve Number: {session.canonical_id if hasattr(session, 'canonical_id') else session_id}",
                        png_path=tile_path,
                        bounds_wgs84=tile_bounds,
                        colormap="YlOrRd",
                        opacity=0.70,
                        auto_zoom=False,
                        metadata={"session_id": session_id, "source": "NLCD + POLARIS"},
                    )
        except Exception as _map_err:
            log.debug("CN grid map push failed (non-fatal): %s", _map_err)

        reminder = _sync_reminder(session_id)
        if reminder:
            d["_sync_required"] = reminder
        return d

    except Exception as e:
        log.error("create_cn_grid failed: %s", e)
        return _tool_error_to_dict(e)


# ============================================================================
# Tool: Forcing Data
# ============================================================================

@mcp.tool()
async def fetch_forcing_data(
    session_id: str,
    start_date: str,
    end_date: str,
    variables: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """
    Basin-averaged daily GridMET forcing (CONUS only): precip, temp, wind,
    humidity, solar radiation. Requires delineate_watershed first.
    variables: subset of [pr, tmmx, tmmn, srad, vs, rmax, rmin, pet, erc]
    (default all). Returns daily arrays saved to disk.
    """
    try:
        session_id = _normalize_session_id(session_id)
        from ai_hydro.session import HydroSession as _HS2
        session = _HS2.load(session_id)

        # Cache-hit check — same session + date range already computed
        if session.forcing is not None:
            cached_params = session.forcing.get("meta", {}).get("params", {})
            if (cached_params.get("start_date") == start_date
                    and cached_params.get("end_date") == end_date):
                compact = _strip_forcing_arrays(session.forcing.get("data", {}))
                return {
                    "data": compact,
                    "meta": session.forcing.get("meta", {}),
                    "_cached": True,
                    "_note": "Forcing data already cached. Full daily arrays on disk.",
                }

        watershed_geojson = _get_session_geometry(session_id)
        from ai_hydro.data.forcing import fetch_forcing_data_result as _fn

        if ctx:
            await ctx.report_progress(progress=0, total=2)

        result = await asyncio.to_thread(
            _fn,
            watershed_geojson=watershed_geojson,
            start_date=start_date,
            end_date=end_date,
            variables=variables,
        )

        if ctx:
            await ctx.report_progress(progress=2, total=2)

        d = _result_to_dict(result)
        saved = _workspace_write(session_id, _canonical_fname(session_id, "forcing", "json"), d["data"])
        # Record data file path in slot so train_hydro_model can reload arrays
        if saved:
            d["data"]["_data_file"] = saved
        _session_store(session_id, "forcing", d, tool_name="fetch_forcing_data")
        compact = _strip_forcing_arrays(d["data"])
        if saved:
            compact["_data_file"] = saved
        resp: dict = {
            "data": compact,
            "meta": d.get("meta", {}),
            "_file_saved": saved,
            "_note": (
                f"Forcing data ({compact.get('n_days', '?')} records, "
                f"{compact.get('n_variables', '?')} variables) saved to "
                f"{saved or 'session'}. Raw daily arrays are NOT in the session JSON "
                "or this response — load from _data_file when needed."
            ),
        }
        reminder = _sync_reminder(session_id)
        if reminder:
            resp["_sync_required"] = reminder
        return resp
    except Exception as e:
        log.error("fetch_forcing_data failed: %s", e)
        return _tool_error_to_dict(e)


# ============================================================================
# Tool: CAMELS-US Catchment Attributes
# ============================================================================

@mcp.tool()
def fetch_camels_us(
    session_id: str,
    gauge_id: str | None = None,
    gauge_ids: list[str] | None = None,
) -> dict:
    """
    CAMELS-US static catchment attributes (671 minimally-disturbed CONUS
    gauges). Single gauge via ``gauge_id`` (cached in session) OR bulk via
    ``gauge_ids`` (list, [] = all 671; saved to workspace).
    Returns ~60 attributes grouped by topography/climate/hydrology/soil/
    vegetation/geology. Citation: Addor et al. (2017), HESS 21.
    """
    try:
        import math

        session_id = _normalize_session_id(session_id)
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)

        try:
            import pygeohydro as gh
        except ImportError:
            return {
                "error": True,
                "code": "DEPENDENCY_ERROR",
                "message": "pygeohydro is required to fetch CAMELS-US attributes.",
                "recovery": "pip install aihydro-tools[data]",
            }

        _META = {
            "tool":     "fetch_camels_us",
            "source":   "CAMELS-US via pygeohydro.get_camels()",
            "citation": (
                "Addor, N., Newman, A. J., Mizukami, N., & Clark, M. P. (2017). "
                "The CAMELS data set: catchment attributes and meteorology for "
                "large-sample studies. Hydrology and Earth System Sciences, 21."
            ),
        }

        GROUPS: dict[str, list[str]] = {
            "topography": ["elev_mean", "slope_mean", "area_gages2", "area_geospa_fabric"],
            "climate":    ["p_mean", "pet_mean", "aridity", "frac_snow", "p_seasonality",
                          "high_prec_freq", "high_prec_dur", "low_prec_freq", "low_prec_dur"],
            "hydrology":  ["q_mean", "runoff_ratio", "stream_elas", "slope_fdc",
                          "baseflow_index", "hfd_mean", "q5", "q95"],
            "soil":       ["soil_depth_pelletier", "soil_depth_statsgo", "soil_porosity",
                          "soil_conductivity", "max_water_content",
                          "sand_frac", "silt_frac", "clay_frac"],
            "vegetation": ["frac_forest", "lai_max", "lai_diff", "gvf_max", "gvf_diff"],
            "geology":    ["geol_1st_class", "glim_1st_class_frac", "geol_2nd_class",
                          "carbonate_rocks_frac", "geol_porostiy", "geol_permeability"],
        }

        def _row_to_attrs(row) -> dict:
            out: dict = {}
            for col, val in row.items():
                if col == "geometry":
                    continue
                try:
                    v = float(val)
                    out[col] = None if math.isnan(v) else round(v, 6)
                except (TypeError, ValueError):
                    out[col] = str(val) if val is not None else None
            return out

        def _group(attrs: dict) -> dict:
            return {
                grp: {k: attrs[k] for k in keys if k in attrs}
                for grp, keys in GROUPS.items()
            }

        # ── Fetch the full dataset once ────────────────────────────────
        attr_df, _ = gh.get_camels()
        idx_list = [str(i).zfill(8) for i in attr_df.index]

        # ── MULTI-GAUGE MODE ───────────────────────────────────────────
        if gauge_ids is not None:
            targets = (
                idx_list if len(gauge_ids) == 0          # [] → all 671
                else [g.strip().zfill(8) for g in gauge_ids]
            )
            gauges_out: dict = {}
            not_found: list[str] = []
            for gid in targets:
                if gid not in idx_list:
                    not_found.append(gid)
                    gauges_out[gid] = {"in_camels": False, "attributes": {}, "attribute_groups": {}}
                else:
                    row = attr_df.iloc[idx_list.index(gid)]
                    attrs = _row_to_attrs(row)
                    gauges_out[gid] = {
                        "in_camels":        True,
                        "n_attributes":     len(attrs),
                        "attributes":       attrs,
                        "attribute_groups": _group(attrs),
                    }

            data = {
                "mode":          "multi",
                "n_requested":   len(targets),
                "n_found":       len(targets) - len(not_found),
                "not_in_camels": not_found,
                "gauges":        gauges_out,
            }
            saved = _workspace_write(session_id, "camels_multi.json", data)
            resp: dict = {
                "data": data,
                "meta": _META,
                "_file_saved": saved,
                "_note": (
                    f"CAMELS-US: {data['n_found']}/{data['n_requested']} gauges found. "
                    f"Attributes saved to workspace as camels_multi.json."
                    + (f" Not in CAMELS: {not_found}" if not_found else "")
                ),
            }
            reminder = _sync_reminder(session_id)
            if reminder:
                resp["_sync_required"] = reminder
            return resp

        # ── SINGLE-GAUGE MODE ──────────────────────────────────────────
        if session.camels is not None:
            return {
                "data": session.camels.get("data", {}),
                "meta": session.camels.get("meta", {}),
                "_cached": True,
            }

        usgs_gauge_id = _resolve_usgs_gauge(session_id, gauge_id, session)
        gauge_norm = usgs_gauge_id.zfill(8)

        if gauge_norm not in idx_list:
            return {
                "data": {
                    "gauge_id":        usgs_gauge_id,
                    "in_camels":       False,
                    "attributes":      {},
                    "n_camels_gauges": len(idx_list),
                },
                "meta": _META,
                "_note": (
                    f"Gauge {usgs_gauge_id} is not in the CAMELS-671 benchmark set. "
                    "Use extract_geomorphic_parameters + extract_hydrological_signatures "
                    "to derive equivalent attributes from DEM and streamflow."
                ),
            }

        row = attr_df.iloc[idx_list.index(gauge_norm)]
        attrs = _row_to_attrs(row)
        data = {
            "gauge_id":         usgs_gauge_id,
            "in_camels":        True,
            "n_attributes":     len(attrs),
            "attributes":       attrs,
            "attribute_groups": _group(attrs),
        }
        d = {"data": data, "meta": _META}
        _session_store(session_id, "camels", d, tool_name="fetch_camels_us")
        saved = _workspace_write(session_id, f"camels_{usgs_gauge_id}.json", data)
        d["_file_saved"] = saved
        d["_note"] = (
            f"CAMELS-US: {len(attrs)} attributes for gauge {usgs_gauge_id} "
            "(topography, climate, hydrology, soil, vegetation, geology). "
            "Cached in session slot 'camels'. Saved to workspace."
        )
        reminder = _sync_reminder(session_id)
        if reminder:
            d["_sync_required"] = reminder
        return d

    except Exception as e:
        log.error("fetch_camels_us failed: %s", e)
        return _tool_error_to_dict(e)


# ============================================================================
# Tool: Library Reference (gotchas, field mappings, code patterns)
# ============================================================================


@mcp.tool()
def separate_baseflow(
    session_id: str,
    method: str = "lyne_hollick",
    alpha: float = 0.925,
    n_passes: int = 3,
) -> dict:
    """
    Separate baseflow from streamflow via digital filter. Writes daily series
    + BFI to session.baseflow. Requires fetch_streamflow_data first.
    method: lyne_hollick (default, recursive filter, alpha 0.9-0.95) |
    ukih (UK Institute of Hydrology 5-day, non-parametric, no alpha).
    """
    try:
        from ai_hydro.session import HydroSession
        from ai_hydro.analysis.baseflow import lyne_hollick, ukih, compute_bfi
        import numpy as np

        session = HydroSession.load(session_id)
        if not session.streamflow:
            return {
                "error": True,
                "code": "MISSING_PREREQUISITES",
                "message": "No streamflow data in session. Run fetch_streamflow_data first.",
                "recovery": "fetch_streamflow_data(session_id, gauge_id)",
            }

        # Extract streamflow array from session
        sf = session.streamflow
        data = sf.get("data", sf) if isinstance(sf, dict) else sf
        # Try common keys for flow array
        q = None
        for key in ("discharge_cms", "flow_cms", "streamflow", "q"):
            if key in data and data[key] is not None:
                q = np.asarray(data[key], dtype=float)
                break
        if q is None:
            # Fallback: take first numeric array value
            for v in data.values():
                if hasattr(v, "__len__") and len(v) > 1:
                    q = np.asarray(v, dtype=float)
                    break
        if q is None or len(q) < 10:
            return {
                "error": True,
                "code": "INVALID_STREAMFLOW",
                "message": "Could not extract a flow array from session.streamflow.",
                "recovery": "Ensure fetch_streamflow_data ran successfully and returned a daily series.",
            }

        method = method.lower()
        if method == "lyne_hollick":
            bf = lyne_hollick(q, alpha=alpha, n_passes=n_passes)
        elif method == "ukih":
            bf = ukih(q)
        else:
            return {
                "error": True,
                "code": "UNKNOWN_METHOD",
                "message": f"Unknown method '{method}'. Choose 'lyne_hollick' or 'ukih'.",
            }

        bfi = compute_bfi(q, bf)
        result = {
            "method": method,
            "alpha": alpha if method == "lyne_hollick" else None,
            "n_passes": n_passes if method == "lyne_hollick" else None,
            "n_days": int(len(q)),
            "bfi": round(float(bfi), 4),
            "baseflow_mean_cms": round(float(np.nanmean(bf)), 4),
            "total_flow_mean_cms": round(float(np.nanmean(q)), 4),
            "baseflow_series_length": len(bf),
            "_note": "Full baseflow series stored in session.baseflow.baseflow_series (not returned inline — context-window protection).",
        }
        session.set("baseflow", {
            "data": {
                "bfi": bfi,
                "method": method,
                "baseflow_series": bf.tolist(),
                "total_flow_series": q.tolist(),
                "n_days": len(q),
            },
            "meta": {
                "tool": "separate_baseflow",
                "method": method,
                "alpha": alpha,
                "n_passes": n_passes,
            },
        })
        session.save()

        return result
    except Exception as e:
        log.error("separate_baseflow failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def get_library_reference(library: str | None = None) -> dict:
    """
    Look up field-name gotchas, API quirks, and copy-paste patterns for a
    Python library used in hydrological workflows.

    Delegates to the MCP resource layer (aihydro://knowledge/library/{name})
    so responses include version drift warnings when the installed library
    version is outside the card's tested range.

    With no argument, returns a catalog of all available library references.

    M3 façade decision (2.0, §7.6): this tool is retained because MCP client
    resource support (list_resources / read_resource) has not uniformly
    matured across target clients (Claude Code, Cline, Claude Desktop).
    The façade will be re-evaluated in a future minor release once resource
    support is stable.

    Parameters
    ----------
    library : str, optional
        Library name (case-insensitive). Omit to list all available references.

    Returns
    -------
    dict with library card data (gotchas, field_mappings, common_patterns,
    version_compatible, and stale/stale_reason if drift detected),
    or a catalog dict when called with no argument.
    """
    try:
        from ai_hydro.mcp.resources import _load_card, _list_all_library_names
        if library is None:
            names = _list_all_library_names()
            return {
                "available_libraries": names,
                "n_available": len(names),
                "usage": "Call get_library_reference(library='pynhd') to load a specific card.",
                "resource_uri_pattern": "aihydro://knowledge/library/{name}",
            }
        card = _load_card(library.lower())
        if card is None:
            return {
                "error": True,
                "code": "NOT_FOUND",
                "message": f"No reference available for '{library}'.",
                "available_refs": _list_all_library_names(),
            }
        return card
    except Exception as e:
        log.error("get_library_reference failed: %s", e)
        return _tool_error_to_dict(e)


# ============================================================================
# Tool: show_on_map
# ============================================================================

@mcp.tool()
def show_on_map(
    geojson: str,
    name: str = "Layer",
    layer_id: str | None = None,
    layer_type: str = "polygon",
    style_preset: str = "default",
    fill_color: str | None = None,
    stroke_color: str | None = None,
    fill_opacity: float | None = None,
    auto_zoom: bool = True,
) -> dict:
    """
    Push any GeoJSON geometry directly onto the AI-Hydro map panel.

    Use this to visualize custom geometries, study area boundaries,
    analysis outputs, or any spatial data the agent generates.
    The map panel opens automatically if it is not already visible.

    Parameters
    ----------
    geojson : str
        GeoJSON FeatureCollection, Feature, or Geometry as a JSON string.
        Must be valid GeoJSON in EPSG:4326 (longitude/latitude degrees).
    name : str
        Display name shown in the Layers panel (default: 'Layer').
    layer_id : str, optional
        Unique layer key. Re-sending the same ID replaces the existing
        layer. Auto-generated if not provided.
    layer_type : str
        Geometry type hint: 'polygon', 'line', 'point', or 'raster'.
        Controls the icon in the Layers panel (default: 'polygon').
    style_preset : str
        Colour theme: 'watershed' (blue), 'flowlines' (light blue),
        'gauge' (orange point), or 'default' (mid blue).
    fill_color : str, optional
        Hex fill colour override, e.g. '#FF5733'. Overrides preset.
    stroke_color : str, optional
        Hex outline colour override. Overrides preset.
    fill_opacity : float, optional
        Fill opacity 0.0–1.0. Overrides preset.
    auto_zoom : bool
        Fit the map to this layer's bounding box (default: True).

    Returns
    -------
    dict:
        ok     : True if the layer event was queued for the map.
        layer_id : The layer ID used (auto-generated or provided).
        message  : Human-readable status.

    Examples
    --------
    >>> show_on_map(watershed_geojson_string, name='My AOI')
    >>> show_on_map(river_geojson, name='Main Stem', layer_type='line',
    ...             style_preset='flowlines')
    """
    try:
        import uuid as _uuid
        from ai_hydro.mcp.map_events import push_layer

        # Validate JSON before pushing
        try:
            json.loads(geojson)
        except json.JSONDecodeError as jde:
            return {"ok": False, "error": f"Invalid GeoJSON: {jde}"}

        lid = layer_id or f"layer_{_uuid.uuid4().hex[:8]}"

        style_override: dict = {}
        if fill_color:
            style_override["fillColor"] = fill_color
        if stroke_color:
            style_override.update({"color": stroke_color, "strokeColor": stroke_color})
        if fill_opacity is not None:
            style_override["fillOpacity"] = fill_opacity

        ok = push_layer(
            layer_id=lid,
            name=name,
            geojson=geojson,
            layer_type=layer_type,
            style_preset=style_preset,
            style_override=style_override or None,
            auto_zoom=auto_zoom,
            open_map=True,
        )

        return {
            "ok": ok,
            "layer_id": lid,
            "message": (
                f"Layer '{name}' queued for map display."
                if ok else
                "Map event could not be written — VS Code extension may not be running."
            ),
        }
    except Exception as e:
        log.error("show_on_map failed: %s", e)
        return _tool_error_to_dict(e)
