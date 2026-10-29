"""
Map orchestration MCP tools — read/write host map session via file bridge.

Agents use these tools to inspect map state, set ROI, open the map panel,
and persist ROI to the workspace without holding a gRPC connection.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import (
    _ensure_session,
    _map_session_workspace_ownership,
    _normalize_session_id,
    _resolve_active_roi_geojson,
    _tool_error_to_dict,
    _workspace_write,
)
from ai_hydro.mcp.map_commands import (
    push_add_layer_from_run,
    push_fit_extent,
    push_fit_layer,
    push_fly_to,
    push_remove_layer,
    push_set_basemap,
    push_set_layer_visibility,
    push_set_roi,
    push_set_time_range,
    push_show_map,
    push_update_layer,
)
from ai_hydro.mcp.map_events import STYLES
from ai_hydro.mcp.map_layer_catalog import (
    compute_graduated_metadata,
    find_catalog_layer,
    list_layer_ids,
    load_geojson_for_layer,
    read_layer_catalog,
)
from ai_hydro.registry.paths import aihydro_home, home_path

log = logging.getLogger("ai_hydro.mcp")

_MAP_SESSION_FILE = home_path("map_session.json")
_MAP_LAYER_CATALOG_FILE = home_path("map_layer_catalog.json")
_MAP_EVENTS_OUTBOUND = home_path("map_events", "outbound")


def _read_map_session_file() -> dict[str, Any]:
    try:
        return json.loads(_MAP_SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _recent_outbound_events(limit: int = 10) -> list[dict[str, Any]]:
    if not _MAP_EVENTS_OUTBOUND.is_dir():
        return []
    files = sorted(_MAP_EVENTS_OUTBOUND.glob("*.json"), key=lambda p: p.stat().st_mtime)[-limit:]
    events: list[dict[str, Any]] = []
    for fp in files:
        try:
            events.append(json.loads(fp.read_text(encoding="utf-8")))
        except Exception:
            continue
    return events


@mcp.tool()
def map_get_state(
    session_id: str | None = None,
    event_limit: int = 10,
    scope: str = "chat",
    chat_id: str | None = None,
    workspace: str | None = None,
) -> dict:
    """
    Map session snapshot from ~/.aihydro/map_session.json: basemap, view,
    visible layers, workspace root, active ROI, recent events. The host fields are
    global to the map panel (``display_state_scope: global_host``).

    ``items`` lists the map items that belong to ``scope`` with the provenance each was stamped
    with (``study_id``, ``run_id``, ``tool``, ``chat_id``, ``source``, ``retrieved_at``) and the
    ``claim_ids`` linked to it. ``scope="chat"`` (default) is this chat's items, ``"project"`` every
    item in this workspace. ``chat_id`` and ``workspace`` default to the call's context. A claim's
    status is not part of an item: read the claim.
    """
    try:
        from ai_hydro.mcp.map_items import SCOPES, collect

        if scope not in SCOPES:
            return {
                "error": True,
                "code": "INVALID_SCOPE",
                "message": f"scope={scope!r} is not one of {list(SCOPES)}.",
                "recovery": 'Pass scope="chat" or scope="project".',
                "next_tools": ["map_get_state"],
            }
        from ai_hydro.mcp.app import ACTIVE_CHAT_ID, ACTIVE_WORKSPACE

        session_id = _normalize_session_id(session_id) if session_id else None
        persisted = _read_map_session_file()
        requested_workspace = None
        if session_id:
            try:
                from ai_hydro.session import HydroSession

                requested_workspace = HydroSession.load(session_id).workspace_dir
            except Exception:
                requested_workspace = None
        host_ownership = _map_session_workspace_ownership(
            persisted,
            requested_workspace if session_id else None,
            requested=bool(session_id),
        )
        active_roi = persisted.get("activeRoi") or persisted.get("active_roi")
        roi_summary = None
        if isinstance(active_roi, dict):
            roi_summary = {
                "id": active_roi.get("id"),
                "name": active_roi.get("name"),
                "source": active_roi.get("source"),
                "workspace_path": active_roi.get("workspacePath") or active_roi.get("workspace_path"),
                "area_ha": active_roi.get("areaHa") or active_roi.get("area_ha"),
                "has_geometry": bool(active_roi.get("geojson")),
            }
        elif active_roi:
            roi_summary = {"malformed": True, "has_geometry": False}
        resolved = None
        if session_id:
            try:
                geojson, source = _resolve_active_roi_geojson(session_id)
                resolved = {
                    "scope": "requested_session",
                    "session_id": session_id,
                    "source": source,
                    "geometry_type": geojson.get("type"),
                }
            except Exception as exc:
                resolved = {
                    "scope": "requested_session",
                    "session_id": session_id,
                    "error": str(exc),
                }

        basemap_id = persisted.get("basemapId") or persisted.get("basemap_id")
        basemap_name = persisted.get("basemapName") or persisted.get("basemap_name")

        catalog = read_layer_catalog()
        scoped = collect(
            scope,
            chat_id=chat_id or ACTIVE_CHAT_ID.get(),
            workspace=workspace or ACTIVE_WORKSPACE.get() or persisted.get("workspaceRoot")
            or persisted.get("workspace_root"),
        )
        scoped.pop("scope", None)

        return {
            "display_state_scope": "global_host",
            "active_roi": roi_summary,
            "active_roi_scope": "global_host",
            "host_workspace_ownership": {
                **host_ownership,
                "active_roi_eligible_for_session": bool(
                    session_id
                    and isinstance(active_roi, dict)
                    and host_ownership["status"] == "matching"
                ),
            },
            "basemap": (
                {"id": basemap_id, "name": basemap_name or basemap_id}
                if basemap_id
                else None
            ),
            "view": persisted.get("view"),
            "visible_layer_ids": persisted.get("visibleLayerIds")
            or persisted.get("visible_layer_ids"),
            "workspace_root": persisted.get("workspaceRoot") or persisted.get("workspace_root"),
            "updated_at_ms": persisted.get("updatedAtMs") or persisted.get("updated_at_ms"),
            "layer_order": catalog.get("layer_order") or [],
            "layers": catalog.get("layers") or [],
            "layer_catalog_scope": "global_host",
            "resolved_roi_for_session": resolved,
            "recent_events": _recent_outbound_events(max(1, min(event_limit, 50))),
            "recent_events_scope": "global_host",
            "scope": scope,
            **scoped,
        }
    except Exception as e:
        return _tool_error_to_dict(e)


@mcp.tool()
def map_set_roi(
    session_id: str,
    geojson: str | dict,
    name: str = "Agent ROI",
    area_ha: float = 0,
) -> dict:
    """
    Set the active map ROI (study basin polygon). The map panel updates via
    the host command watcher. Pass GeoJSON Feature, FeatureCollection, or JSON string.
    """
    try:
        session_id = _normalize_session_id(session_id)
        _ensure_session(session_id, None)
        ok = push_set_roi(
            geojson=geojson,
            name=name,
            source="agent",
            area_ha=area_ha,
        )
        return {"ok": ok, "message": "ROI command queued for map host" if ok else "Failed to queue ROI"}
    except Exception as e:
        return _tool_error_to_dict(e)


@mcp.tool()
def map_show() -> dict:
    """Open the AI-Hydro map panel if it is not already visible."""
    ok = push_show_map(open_map=True)
    return {"ok": ok, "message": "Map open requested" if ok else "Failed to open map"}


@mcp.tool()
def map_set_working_geometry(session_id: str, workspace_path: str) -> dict:
    """
    Set which workspace GeoJSON file is the active study geometry for this session
    (used by current_map_basin / GEE). Path is relative to workspace, e.g.
    vectors/my_basin.geojson or watershed_01031500.geojson.
    """
    try:
        session_id = _normalize_session_id(session_id)
        session = _ensure_session(session_id, None)
        rel = workspace_path.strip().lstrip("/")
        if not rel:
            return {"ok": False, "message": "workspace_path is required"}
        if session.workspace_dir:
            full = Path(session.workspace_dir) / rel
            if not full.exists():
                return {"ok": False, "message": f"File not found: {rel}"}
        session.working_geometry_path = rel
        session.save()
        return {"ok": True, "working_geometry_path": rel}
    except Exception as e:
        return _tool_error_to_dict(e)


@mcp.tool()
def map_fit_extent() -> dict:
    """Ask the map to fit the viewport to the active ROI or visible layers."""
    ok = push_fit_extent()
    return {"ok": ok, "message": "Fit extent requested" if ok else "Failed to queue fit"}


@mcp.tool()
def map_list_layers() -> dict:
    """List map layer ids and catalog entries from the host (~/.aihydro/map_layer_catalog.json)."""
    try:
        catalog = read_layer_catalog()
        return {
            "ok": True,
            "layer_order": catalog.get("layer_order") or [],
            "layers": catalog.get("layers") or [],
            "layer_ids": list_layer_ids(),
        }
    except Exception as e:
        return _tool_error_to_dict(e)


def _layer_not_found(layer_id: str) -> dict:
    ids = list_layer_ids()
    return {
        "ok": False,
        "message": f"Unknown layer_id '{layer_id}'. Known layers: {ids or '(none — open map and load layers first)'}",
        "layer_ids": ids,
    }


@mcp.tool()
def map_update_layer(
    layer_id: str,
    style_preset: str | None = None,
    fill_color: str | None = None,
    stroke_color: str | None = None,
    fill_opacity: float | None = None,
    stroke_width: int | None = None,
    visible: bool | None = None,
    display_name: str | None = None,
    raster_colormap: str | None = None,
    raster_opacity: float | None = None,
    clear_graduated: bool = False,
    graduated_attribute: str | None = None,
    graduated_method: str = "quantile",
    graduated_classes: int = 5,
    color_ramp: str = "viridis",
) -> dict:
    """
    Update layer style/visibility in-place (don't rewrite GeoJSON for styling).
    For choropleth, set graduated_attribute (+ method/classes/ramp).
    """
    try:
        entry = find_catalog_layer(layer_id)
        if not entry:
            return _layer_not_found(layer_id)

        style: dict[str, Any] = {}
        metadata: dict[str, str] = {}

        if style_preset:
            preset = STYLES.get(style_preset, STYLES["default"])
            style = {k: v for k, v in preset.items() if isinstance(v, (str, int, float))}
        if fill_color:
            style["fillColor"] = fill_color
        if stroke_color:
            style["strokeColor"] = stroke_color
            style["color"] = stroke_color
        if fill_opacity is not None:
            style["fillOpacity"] = fill_opacity
        if stroke_width is not None:
            style["strokeWidth"] = stroke_width
            style["weight"] = stroke_width

        if raster_colormap:
            metadata["raster_colormap"] = raster_colormap
        if raster_opacity is not None:
            metadata["raster_opacity"] = str(raster_opacity)

        if graduated_attribute:
            persisted = _read_map_session_file()
            workspace_root = persisted.get("workspaceRoot") or persisted.get("workspace_root")
            geojson = load_geojson_for_layer(entry, workspace_root)
            metadata.update(
                compute_graduated_metadata(
                    geojson,
                    attribute=graduated_attribute,
                    method=graduated_method,
                    num_classes=graduated_classes,
                    color_ramp=color_ramp,
                )
            )
            clear_graduated = False

        ok = push_update_layer(
            layer_id=layer_id,
            style=style or None,
            metadata=metadata or None,
            visible=visible,
            display_name=display_name,
            clear_graduated=clear_graduated,
        )
        if visible is not None:
            push_set_layer_visibility(layer_id, visible)
        return {
            "ok": ok,
            "layer_id": layer_id,
            "message": "Layer update queued for map host" if ok else "Failed to queue update",
        }
    except Exception as e:
        return _tool_error_to_dict(e)


@mcp.tool()
def map_apply_symbology(
    layer_id: str,
    attribute: str,
    method: str = "quantile",
    num_classes: int = 5,
    color_ramp: str = "viridis",
) -> dict:
    """Apply graduated (choropleth) symbology to an existing vector layer on the map."""
    return map_update_layer(
        layer_id=layer_id,
        graduated_attribute=attribute,
        graduated_method=method,
        graduated_classes=num_classes,
        color_ramp=color_ramp,
    )


@mcp.tool()
def map_remove_layer(layer_id: str) -> dict:
    """Remove a layer from the map by id."""
    if not find_catalog_layer(layer_id):
        return _layer_not_found(layer_id)
    ok = push_remove_layer(layer_id)
    return {"ok": ok, "layer_id": layer_id}


@mcp.tool()
def map_set_basemap(basemap_id: str, basemap_name: str | None = None) -> dict:
    """Set the map basemap (e.g. esri-imagery, usgs-topo)."""
    ok = push_set_basemap(basemap_id, basemap_name)
    return {"ok": ok, "basemap_id": basemap_id}


@mcp.tool()
def map_fit_layer(layer_id: str) -> dict:
    """Zoom the map viewport to a layer's extent."""
    if not find_catalog_layer(layer_id):
        return _layer_not_found(layer_id)
    ok = push_fit_layer(layer_id)
    return {"ok": ok, "layer_id": layer_id}


@mcp.tool()
def map_fly_to(
    longitude: float,
    latitude: float,
    zoom: float = 9.0,
    duration_ms: int = 2000,
) -> dict:
    """
    Smoothly navigate the map camera to a point.

    The map panel animates to the given longitude/latitude/zoom over duration_ms
    milliseconds (deck.gl linear transition). Useful after computing a basin
    centroid — fly the user's attention to the study area without forcing a fit.
    """
    ok = push_fly_to(longitude=longitude, latitude=latitude, zoom=zoom, duration_ms=duration_ms)
    return {
        "ok": ok,
        "longitude": longitude,
        "latitude": latitude,
        "zoom": zoom,
        "duration_ms": duration_ms,
        "message": "Fly-to command queued for map host" if ok else "Failed to queue fly_to",
    }


_GEOJSON_TYPES = frozenset({
    "Feature", "FeatureCollection", "Point", "LineString", "Polygon", "MultiPolygon",
    "MultiPoint", "MultiLineString", "GeometryCollection",
})
_GEOMETRY_SUFFIXES = (".geojson", ".json")


def _run_error(code: str, message: str, recovery: str, **extra: Any) -> dict:
    return {"ok": False, "error": True, "code": code, "message": message, "recovery": recovery,
            "next_tools": ["map_get_state", "map_list_layers"], **extra}


def _geometry_artifacts(session: Any, run_id: str) -> list[dict[str, Any]]:
    """Manifest entries the study recorder stored for ``run_id`` that can hold geometry."""
    out = []
    for entry in (session.artifact_manifest or {}).values():
        if not isinstance(entry, dict) or entry.get("run_id") != run_id or not entry.get("path"):
            continue
        if entry.get("type") == "workspace_file" or entry.get("type") == "map_layer":
            if str(entry["path"]).lower().endswith(_GEOMETRY_SUFFIXES):
                out.append(entry)
    return out


@mcp.tool()
def map_add_layer_from_run(
    session_id: str,
    run_id: str,
    layer_name: str | None = None,
    auto_zoom: bool = True,
    geojson_key: str | None = None,
) -> dict:
    """
    Add the geometry a tool run produced to the map as a new layer.

    The run row is read through the session store (the row may exist only in the run-log
    database) and the geometry is read from the run's recorded artifact: a GeoJSON file the
    study recorder stored for that run. Nothing is taken from the row's summary values. The layer
    carries the run's provenance (``_run_id``, ``study_id``, ``tool``) so chips link back.
    When the run has no GeoJSON artifact the call fails with ``NO_GEOMETRY_ARTIFACT``.

    ``geojson_key`` picks one artifact (its ``artifact_id`` such as ``file:watershed_X.geojson``, or
    its workspace-relative path) when the run recorded several. ``auto_zoom=True`` (default) zooms to it.
    """
    try:
        import os

        from ai_hydro.session import HydroSession, run_records, store

        run_id = (run_id or "").strip()
        if not (session_id or "").strip() or not run_id:
            return _run_error("RUN_REQUIRED", "session_id and run_id are required",
                              "Pass the study id and a run id returned by a tool as _run_id.")
        session_id = _normalize_session_id(session_id)
        if not run_records._session_exists(session_id):
            return _run_error("STUDY_NOT_FOUND", f"Study '{session_id}' was not found.",
                              "Call aihydro_chat_status or list_sessions for the studies you can use.")

        row = store._run_log_read_one(session_id, run_id)
        if not isinstance(row, dict):
            recent = list((HydroSession.load(session_id).get("_run_log") or {}).keys())[-10:]
            return _run_error("RUN_NOT_FOUND", f"run_id '{run_id}' is not in study '{session_id}'.",
                              "Use a run id from a tool result's _run_id.", recent_run_ids=recent)

        session = HydroSession.load(session_id)
        artifacts = _geometry_artifacts(session, run_id)
        if geojson_key:
            artifacts = [a for a in artifacts
                         if geojson_key in (a.get("artifact_id"), a.get("path"), a.get("layer_id"))]
        if not artifacts:
            return _run_error(
                "NO_GEOMETRY_ARTIFACT",
                f"Run '{run_id}' recorded no GeoJSON artifact"
                + (f" matching '{geojson_key}'" if geojson_key else "")
                + ". Runs from before the study recorder, and runs that wrote no geometry file, have none.",
                "Re-run the tool so its output is recorded, or add the file with map_set_working_geometry.",
                run_id=run_id, study_id=session_id)

        chosen = artifacts[0]
        ws = session.workspace_dir
        if not ws:
            return _run_error("ARTIFACT_MISSING", "The study has no workspace, so its artifact cannot be located.",
                              "Re-run the tool in a workspace.", run_id=run_id, artifact_id=chosen.get("artifact_id"))
        # The manifest path is data: it must resolve to a file inside the workspace.
        ws_real = os.path.realpath(ws)
        path = os.path.realpath(os.path.join(ws_real, str(chosen["path"])))
        try:
            inside = os.path.commonpath([ws_real, path]) == ws_real
        except ValueError:
            inside = False
        if not inside:
            return _run_error("ARTIFACT_OUTSIDE_WORKSPACE",
                              f"The recorded artifact path '{chosen['path']}' resolves outside the study workspace.",
                              "Re-run the tool so its output is recorded inside the workspace.",
                              run_id=run_id, artifact_id=chosen.get("artifact_id"))
        if not os.path.isfile(path):
            return _run_error("ARTIFACT_MISSING",
                              f"The recorded artifact '{chosen['path']}' is not on disk.",
                              "Restore the file or re-run the tool.", run_id=run_id, artifact_id=chosen.get("artifact_id"))
        # Read once: the bytes that are hashed are the bytes that are parsed.
        try:
            raw_bytes = Path(path).read_bytes()
        except OSError:
            return _run_error("ARTIFACT_MISSING", f"The recorded artifact '{chosen['path']}' could not be read.",
                              "Restore the file or re-run the tool.", run_id=run_id, artifact_id=chosen.get("artifact_id"))
        recorded = chosen.get("sha256")
        hash_checked = bool(recorded)
        if recorded:
            import hashlib
            if hashlib.sha256(raw_bytes).hexdigest() != recorded:
                return _run_error(
                    "ARTIFACT_MODIFIED",
                    f"'{chosen['path']}' changed after run '{run_id}' recorded it, so it is no longer that run's output.",
                    "Re-run the tool, or push the edited file as a new layer.",
                    run_id=run_id, artifact_id=chosen.get("artifact_id"))
        try:
            geojson = json.loads(raw_bytes.decode("utf-8"))
        except ValueError:
            geojson = None
        if not (isinstance(geojson, dict) and geojson.get("type") in _GEOJSON_TYPES):
            return _run_error("ARTIFACT_NOT_GEOJSON", f"'{chosen['path']}' is not GeoJSON.",
                              "Pass geojson_key=<artifact_id> to choose another artifact.",
                              run_id=run_id, artifact_id=chosen.get("artifact_id"))

        tool_name = str(row.get("tool_name") or (row.get("record") or {}).get("tool") or "unknown_tool")
        name = layer_name or f"{tool_name} / {run_id[:12]}"
        lid = f"run_{run_id.replace('.', '_').replace('-', '_')[:32]}"
        from ai_hydro.mcp.app import ACTIVE_CHAT_ID

        provenance = {
            "_run_id": run_id, "study_id": session_id, "tool": tool_name,
            "source": f"aihydro:{tool_name}", "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
        if ACTIVE_CHAT_ID.get():
            provenance["chat_id"] = ACTIVE_CHAT_ID.get()
        ok = push_add_layer_from_run(
            layer_id=lid,
            layer_name=name,
            geojson=json.dumps(geojson),
            run_id=run_id,
            session_id=session_id,
            auto_zoom=auto_zoom,
            provenance=provenance,
        )
        return {
            "ok": ok,
            "layer_id": lid,
            "layer_name": name,
            "artifact_id": chosen.get("artifact_id"),
            "run_id": run_id,
            "provenance": provenance,
            "hash_checked": hash_checked,
            "message": "Layer add command queued for map host" if ok else "Failed to queue add_layer_from_run",
        }
    except Exception as e:
        return _tool_error_to_dict(e)


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$")


def _strict_iso(value: str, which: str) -> tuple[str, "datetime"]:
    """``(normalised, instant)`` for a strict ISO-8601 date (``YYYY-MM-DD``) or date-time with
    ``Z`` or a numeric offset. A date-time is normalised to UTC (``...Z``); a date is kept.
    Raises ``ValueError`` naming the argument otherwise (no zone-less date-times, no free text)."""
    from datetime import datetime, timezone

    v = (value or "").strip()
    if _ISO_DATE.match(v):
        try:
            return v, datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"{which}={value!r} is not a valid calendar date") from None
    if _ISO_DATETIME.match(v):
        t = v.replace(" ", "T").replace("Z", "+00:00")
        if re.search(r"[+-]\d{4}$", t):
            t = t[:-2] + ":" + t[-2:]
        try:
            dt = datetime.fromisoformat(t).astimezone(timezone.utc)
        except ValueError:
            raise ValueError(f"{which}={value!r} is not a valid ISO-8601 date-time") from None
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), dt
    raise ValueError(f"{which}={value!r} is not strict ISO-8601: use YYYY-MM-DD, or a date-time "
                     "with a zone (2020-01-01T00:00:00Z or 2020-01-01T00:00:00+02:00)")


@mcp.tool()
def map_set_time_range(start_iso: str, end_iso: str) -> dict:
    """
    Set the map time slider to a specific date range.

    Both arguments are strict ISO 8601: a date ("2020-01-01") or a date-time with a zone
    ("2020-01-01T00:00:00Z", offsets allowed; normalised to UTC). Anything else, and an end
    before the start, is refused (the map host rejects non-ISO values silently).
    The time slider (Phase 3.4) will filter layers to those whose `time_start` /
    `time_end` metadata overlaps this range. Layers with no time metadata remain
    visible. Combine with `map_add_layer_from_run` to focus the map on the
    study period of a completed analysis run.
    """
    if not start_iso or not end_iso:
        return {"ok": False, "error": True, "code": "TIME_RANGE_REQUIRED",
                "message": "start_iso and end_iso are required",
                "recovery": "Pass both as YYYY-MM-DD or a zoned ISO-8601 date-time."}
    try:
        start_n, start_t = _strict_iso(start_iso, "start_iso")
        end_n, end_t = _strict_iso(end_iso, "end_iso")
        if end_t < start_t:
            raise ValueError(f"end_iso {end_n} is before start_iso {start_n}")
    except ValueError as exc:
        return {"ok": False, "error": True, "code": "INVALID_TIME_RANGE", "message": str(exc),
                "recovery": "Pass start_iso <= end_iso, each as YYYY-MM-DD or a zoned ISO-8601 date-time.",
                "next_tools": ["map_set_time_range"]}
    ok = push_set_time_range(start_iso=start_n, end_iso=end_n)
    return {
        "ok": ok,
        "start_iso": start_n,
        "end_iso": end_n,
        "message": "Time range command queued for map host" if ok else "Failed to queue set_time_range",
    }


@mcp.tool()
def map_save_roi(
    session_id: str,
    name: str,
    workspace_dir: str | None = None,
) -> dict:
    """
    Save the host map session active ROI to workspace roi/<slug>.geojson
    and update roi/active.json. Requires an active ROI on the map session file.
    """
    try:
        session_id = _normalize_session_id(session_id)
        session = _ensure_session(session_id, workspace_dir)
        persisted = _read_map_session_file()
        active_roi = persisted.get("activeRoi") or persisted.get("active_roi")
        if not active_roi or not active_roi.get("geojson"):
            return {
                "ok": False,
                "message": "No active ROI in map session. Use map_set_roi or draw on the map first.",
            }
        geojson_raw = active_roi.get("geojson")
        if isinstance(geojson_raw, str):
            geojson = json.loads(geojson_raw)
        else:
            geojson = geojson_raw

        slug = (
            name.lower()
            .replace(" ", "_")
            .replace("-", "_")
        )
        for ch in slug:
            if not (ch.isalnum() or ch == "_"):
                slug = slug.replace(ch, "_")
        slug = slug.strip("_")[:64] or "basin"
        rel_geo = f"roi/{slug}.geojson"
        saved = _workspace_write(session_id, rel_geo, geojson)
        if not saved:
            return {"ok": False, "message": "Workspace write failed — set workspace_dir on session first"}

        pointer = {
            "path": rel_geo,
            "name": active_roi.get("name") or name,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        _workspace_write(session_id, "roi/active.json", pointer)
        push_set_roi(
            geojson=geojson_raw if isinstance(geojson_raw, str) else json.dumps(geojson),
            name=pointer["name"],
            source="workspace",
            workspace_path=rel_geo,
        )
        return {
            "ok": True,
            "workspace_path": rel_geo,
            "active_pointer": "roi/active.json",
            "saved_to": saved,
        }
    except Exception as e:
        return _tool_error_to_dict(e)
