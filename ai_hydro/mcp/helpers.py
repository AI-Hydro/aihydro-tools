"""
Shared helper functions for MCP tool implementations.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ai_hydro.mcp")


# ---------------------------------------------------------------------------
# Chat ↔ Study session resolution (Wave 3)
# ---------------------------------------------------------------------------

class SessionResolutionError(RuntimeError):
    """
    Raised when a tool cannot resolve which study to operate on.

    Carries a ``recovery`` hint and ``next_tools`` list so the agent
    can self-recover without guessing.
    """

    def __init__(
        self,
        message: str,
        *,
        recovery: str = "",
        next_tools: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.recovery = recovery
        self.next_tools = next_tools or []

    def to_dict(self) -> dict:
        d: dict = {
            "error": True,
            "code": "SESSION_RESOLUTION_FAILED",
            "message": str(self),
        }
        if self.recovery:
            d["recovery"] = self.recovery
        if self.next_tools:
            d["next_tools"] = self.next_tools
        return d


def _read_active_workspace_file() -> str | None:
    """
    Read ~/.aihydro/active_workspace.json written by the VS Code extension on
    activation.  Returns the workspace path, or None if the file is absent or
    the path no longer exists on disk.
    """
    try:
        import json as _json
        ws_file = Path.home() / ".aihydro" / "active_workspace.json"
        if not ws_file.exists():
            return None
        data = _json.loads(ws_file.read_text())
        ws = data.get("workspace")
        if ws and Path(ws).is_dir():
            return ws
    except Exception:
        pass
    return None


def _maybe_set_workspace(session: "Any") -> None:
    """
    If the session has no workspace_dir yet, resolve it from (in priority order):
      1. ACTIVE_WORKSPACE ContextVar   — set per-call by UseMcpToolHandler.ts injection
      2. ~/.aihydro/active_workspace.json — written on every extension activation

    Having a file-based fallback means the workspace is available even when the
    extension has not been reloaded after a VSIX install (no window-reload ceremony).
    """
    try:
        if session.workspace_dir:
            return  # already set — don't override
        from ai_hydro.mcp.app import ACTIVE_WORKSPACE
        ws = ACTIVE_WORKSPACE.get() or _read_active_workspace_file()
        if ws and Path(ws).is_dir():
            session.workspace_dir = ws
            session.save()
            log.debug("Set workspace_dir=%s on session %s (source=%s)",
                      ws, session.session_id,
                      "injection" if ACTIVE_WORKSPACE.get() else "activation_file")
    except Exception as exc:
        log.debug("_maybe_set_workspace failed (non-fatal): %s", exc)


def _extract_chat_id(args: dict) -> tuple[str | None, dict]:
    """
    Pop ``_chat_id`` from *args* and return ``(chat_id, remaining_args)``.

    The ``_chat_id`` field is injected by the TypeScript extension's
    ``UseMcpToolHandler`` (Wave 3 Axis 3) and must never leak into tool
    kwargs or be exposed in tool schemas.  Call this at the top of any
    tool that needs chat-level binding.

    Returns ``(None, args)`` unchanged when ``_chat_id`` is absent.
    """
    chat_id = args.pop("_chat_id", None) or None
    return chat_id, args


def _resolve_session(
    session_id: str | None,
    chat_id: str | None = None,
    *,
    auto_create_hint: str | None = None,
    allow_auto_create: bool = True,
) -> str:
    # Wave 3 Axis 3: if no chat_id was passed explicitly, read from the
    # per-request ContextVar populated by the _call_tool_mcp interceptor.
    # This fires for every tool invoked via the MCP server while the extension
    # injects _chat_id; it is a no-op when the ContextVar holds None (e.g.
    # direct Python calls, tests, CLI invocations).
    if chat_id is None:
        from ai_hydro.mcp.app import ACTIVE_CHAT_ID
        chat_id = ACTIVE_CHAT_ID.get()
    """
    Resolve which study (HydroSession) a tool call should operate on.

    Priority chain
    --------------
    1. **Explicit** ``session_id`` — always wins.  Also rebinds the chat
       to this study if ``chat_id`` is provided.
    2. **Chat binding** — look up ``~/.aihydro/chat_studies.json`` for
       a study previously bound to *chat_id*.
    3. **Auto-create** — if *auto_create_hint* is set (typically a
       gauge ID, lat/lon slug, or explicit name) and
       *allow_auto_create* is True, create a new HydroSession with
       that ID and bind it to the chat.
    4. **Error** — raises ``SessionResolutionError`` with a helpful
       recovery message.

    Parameters
    ----------
    session_id:
        Explicitly supplied session ID (may be ``None``).
    chat_id:
        Chat ULID injected by the extension (may be ``None`` before
        Wave 3 Axis 3 is wired).
    auto_create_hint:
        A slug to use when auto-creating a new study.  Typically
        supplied by delineation tools.
    allow_auto_create:
        Set to ``False`` for admin / query tools that should not
        silently create sessions.

    Returns
    -------
    str
        A normalised session_id ready for ``HydroSession.load()``.
    """
    from ai_hydro.session.chat_binding import get_binding_store

    store = get_binding_store()

    # Guard: hard-reject the legacy 'map' placeholder so it never silently
    # overwrites the global map session or obscures a real chat binding.
    if session_id and _normalize_session_id(session_id) == "map":
        raise SessionResolutionError(
            "'map' is a reserved legacy placeholder and cannot be used as a session_id. "
            "Omit session_id entirely — it is auto-resolved from the chat context via "
            "Wave 3 chat binding.",
            recovery=(
                "Remove session_id='map' from your tool call. "
                "Use aihydro_chat_status() to inspect the current binding. "
                "Use aihydro_rebind_chat(study_id) to switch to a specific study."
            ),
            next_tools=["aihydro_chat_status", "aihydro_rebind_chat"],
        )

    # 1. Explicit session_id → use it; optionally rebind chat
    if session_id:
        sid = _normalize_session_id(session_id)
        if chat_id and store.lookup_study(chat_id) != sid:
            try:
                store.bind(chat_id, sid)
                log.debug("Rebound chat=%s → study=%s (explicit override)", chat_id, sid)
            except Exception as exc:
                log.debug("Could not persist chat binding: %s", exc)
        return sid

    # 2. Chat binding lookup
    if chat_id:
        bound = store.lookup_study(chat_id)
        if bound:
            log.debug("Resolved study=%s from chat binding chat=%s", bound, chat_id)
            # Opportunistically backfill workspace_dir if the session was
            # created before _workspace injection was wired.
            try:
                from ai_hydro.session import HydroSession
                _maybe_set_workspace(HydroSession.load(bound))
            except Exception:
                pass
            return bound

    # 3. Auto-create from hint
    if auto_create_hint and allow_auto_create:
        from ai_hydro.session import HydroSession
        sid = _normalize_session_id(auto_create_hint)
        try:
            session = HydroSession.load(sid)  # creates if not exists
            # Populate workspace_dir from the injected _workspace ContextVar
            # so file outputs (TWI, geomorphic, etc.) land in the VS Code project.
            _maybe_set_workspace(session)
        except Exception as exc:
            log.debug("Auto-create session %s: %s", sid, exc)
        if chat_id:
            try:
                store.bind(chat_id, sid)
                log.debug("Auto-created study=%s and bound to chat=%s", sid, chat_id)
            except Exception as exc:
                log.debug("Could not persist auto-created binding: %s", exc)
        return sid

    # 3.5 Most-recent-session fallback (WS-4 session-binding fix).
    # The weak model frequently omits session_id on a follow-up call before a
    # chat binding exists. Rather than hard-failing, fall back to the most
    # recently modified study on disk and bind it to the chat. This is gated by
    # allow_auto_create so query/admin tools that opt out are unaffected.
    if allow_auto_create:
        try:
            # Reference the private symbol: tests patch ``_SESSIONS_DIR`` to a
            # tmp dir, so reading it here keeps the fallback test-isolated.
            from ai_hydro.session.store import _SESSIONS_DIR as SESSIONS_DIR
            candidates = [
                p for p in SESSIONS_DIR.glob("*.json")
                if ".shard." not in p.name and not p.name.startswith("_")
            ]
            if candidates:
                latest = max(candidates, key=lambda p: p.stat().st_mtime)
                sid = latest.stem
                log.info(
                    "No session_id and no chat binding; falling back to most "
                    "recent study '%s'. Pass session_id to target another.", sid
                )
                if chat_id:
                    try:
                        store.bind(chat_id, sid)
                    except Exception as exc:
                        log.debug("Could not persist fallback binding: %s", exc)
                try:
                    from ai_hydro.session import HydroSession
                    _maybe_set_workspace(HydroSession.load(sid))
                except Exception:
                    pass
                return sid
        except Exception as exc:
            log.debug("Most-recent-session fallback skipped: %s", exc)

    # 4. Failure
    hint_parts: list[str] = []
    if chat_id:
        hint_parts.append(
            "No study is bound to this chat yet. "
            "Run a delineation first (delineate_watershed or delineate_watershed_from_point)."
        )
    else:
        hint_parts.append("Pass session_id explicitly, or run a delineation first.")

    raise SessionResolutionError(
        "Cannot determine which study to use: no session_id, no chat binding, "
        "and no auto-create hint is available.",
        recovery=" ".join(hint_parts) or (
            "Pass session_id explicitly. "
            "Example: compute_twi(session_id='basin_26p9_78p1')"
        ),
        next_tools=["delineate_watershed", "delineate_watershed_from_point", "start_session"],
    )


# ---------------------------------------------------------------------------
# Session identity helpers
# ---------------------------------------------------------------------------

def _normalize_session_id(session_id: str | None) -> str:
    """
    Accept any string as a session identifier.

    - Non-empty string → returned as-is (slugs, UUIDs, gauge IDs all valid)
    - None / empty → auto-generate "hydro-<8hex>" UUID
    """
    if session_id and str(session_id).strip():
        return str(session_id).strip()
    import uuid
    return f"hydro-{uuid.uuid4().hex[:8]}"


def _validate_usgs_gauge_id(gauge_id: str) -> str:
    """
    Validate and normalise a USGS station number.

    Used ONLY in tools that fetch data from USGS NWIS / NLDI.
    Auto-pads short IDs (e.g. '1031500' → '01031500').
    Raises ValueError for non-numeric inputs.
    """
    gid = str(gauge_id).strip()
    if gid.isdigit() and len(gid) < 8:
        gid = gid.zfill(8)
    if not gid.isdigit():
        raise ValueError(
            f"Invalid USGS gauge_id: {gauge_id!r}. "
            "Expected an 8-digit USGS station number (e.g. '01031500'). "
            "Find gauge IDs at https://waterdata.usgs.gov/"
        )
    return gid


# Backward-compat alias — callers that haven't been updated yet
def _validate_gauge_id(gauge_id: str) -> str:
    return _validate_usgs_gauge_id(gauge_id)


# ---------------------------------------------------------------------------
# Result conversion
# ---------------------------------------------------------------------------

def _result_to_dict(result: Any) -> dict:
    if hasattr(result, "to_dict"):
        return result.to_dict()
    if isinstance(result, dict):
        return result
    return {"data": str(result)}


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def _lean_key_outputs(result_dict: dict) -> dict:
    """Extract small, JSON-safe key outputs for replay/provenance panels."""
    data = result_dict.get("data") if isinstance(result_dict.get("data"), dict) else result_dict
    out: dict[str, Any] = {}
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        if key.startswith("_"):
            continue
        if isinstance(value, list):
            out[f"{key}_n"] = len(value)
        elif isinstance(value, dict):
            out[key] = {k: (len(v) if isinstance(v, list) else v) for k, v in list(value.items())[:20]}
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        if len(out) >= 40:
            break
    return out


def _record_run_log_entry(session: Any, slot: str, result_dict: dict, tool_name: str | None = None) -> None:
    """Append/replace a deterministic run-log entry for a stored result."""
    raw_meta = result_dict.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    resolved_tool = tool_name or meta.get("tool") or slot
    timestamp = meta.get("computed_at") or datetime.now(timezone.utc).isoformat()
    digest_src = json.dumps(
        {"slot": slot, "tool": resolved_tool, "timestamp": timestamp, "outputs": _lean_key_outputs(result_dict)},
        sort_keys=True,
        default=str,
    )
    run_id = result_dict.get("run_id") or f"{slot}.{hashlib.sha1(digest_src.encode('utf-8')).hexdigest()[:12]}"
    run_log = session.get("_run_log") or {}
    run_log[run_id] = {
        "run_id": run_id,
        "tool_name": resolved_tool,
        "session_id": session.session_id,
        "timestamp": timestamp,
        "key_outputs": _lean_key_outputs(result_dict),
        "slot": slot,
    }
    from ai_hydro.session.evidence import capture_result_evidence
    run_log[run_id]["evidence"] = capture_result_evidence(result_dict)
    session.set("_run_log", run_log)


def _session_store(
    session_id: str, slot: str, result_dict: dict, *, tool_name: str | None = None
) -> None:
    """Cache a tool result in HydroSession and refresh research.md.

    If ``tool_name`` is provided, Tier 1 data-source citations for that tool
    are added to the session in the same save() call (zero extra file writes).
    """
    try:
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        if tool_name:
            result_dict.setdefault("meta", {})["tool"] = tool_name
        result_dict.setdefault("meta", {}).setdefault("computed_at", datetime.now(timezone.utc).isoformat())
        session.set(slot, result_dict)
        if tool_name:
            from ai_hydro.citations import citation_keys_for_tool
            keys = citation_keys_for_tool(tool_name)
            if keys:
                session.add_citations(keys)
        session.save()
    except Exception as exc:
        log.debug("Session store skipped (%s): %s", slot, exc)


def _get_session_geometry(session_id: str) -> dict:
    """
    Return the watershed GeoJSON dict from the cached session.

    Supports both storage forms:
    - New (v1.3+): session stores geometry_geojson_path → reads from file
    - Legacy: session stores full geometry_geojson dict inline
    """
    try:
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        if session.watershed is None:
            raise RuntimeError(
                f"No watershed cached for session '{session_id}'. "
                "Run delineate_watershed first."
            )
        ws_data = session.watershed.get("data", {})

        geojson_path = ws_data.get("geometry_geojson_path")
        if geojson_path:
            p = Path(geojson_path)
            if p.exists():
                with open(p) as f:
                    return json.load(f)
            log.warning(
                "geometry_geojson_path points to missing file %s for session %s; "
                "trying legacy inline storage", geojson_path, session_id
            )

        geojson = (
            ws_data.get("geometry_geojson")
            or ws_data.get("geometry")
            or ws_data.get("geojson")
        )
        if geojson is not None:
            return geojson

        raise RuntimeError(
            f"Watershed geometry missing from session '{session_id}'. "
            "The session may be corrupted. Run: "
            f"clear_session('{session_id}', ['watershed']) then delineate_watershed again."
        )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Could not load session geometry: {exc}") from exc


class ActiveRoiResolutionError(RuntimeError):
    """Raised when an explicitly selected ROI cannot be loaded safely."""


_GEOJSON_TYPES = {
    "Feature",
    "FeatureCollection",
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
    "GeometryCollection",
}


def _normalize_workspace_path(value: Any) -> str | None:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        return None
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            return None
        return str(path.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return None


def _map_session_workspace_ownership(
    map_session: dict[str, Any],
    requested_workspace: str | Path | None,
    *,
    requested: bool = True,
) -> dict[str, Any]:
    """Classify whether global host state belongs to a requested workspace."""
    host_raw = map_session.get("workspaceRoot") or map_session.get("workspace_root")
    host_workspace = _normalize_workspace_path(host_raw)
    session_workspace = _normalize_workspace_path(requested_workspace)
    if not requested:
        status = "not_requested"
    elif host_workspace is None or session_workspace is None:
        status = "unknown"
    elif host_workspace == session_workspace:
        status = "matching"
    else:
        status = "conflicting"
    return {
        "status": status,
        "host_workspace_root": host_workspace,
        "requested_workspace_dir": session_workspace,
    }


def _validate_basic_geojson(value: Any, *, selection: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("type") not in _GEOJSON_TYPES:
        raise ActiveRoiResolutionError(
            f"Selected {selection} is not a GeoJSON Feature, FeatureCollection, or geometry."
        )
    geojson_type = value["type"]
    if geojson_type == "FeatureCollection" and not isinstance(value.get("features"), list):
        raise ActiveRoiResolutionError(f"Selected {selection} has an invalid GeoJSON features array.")
    if geojson_type == "Feature" and not isinstance(value.get("geometry"), dict):
        raise ActiveRoiResolutionError(f"Selected {selection} has no valid GeoJSON geometry.")
    if geojson_type == "GeometryCollection" and not isinstance(value.get("geometries"), list):
        raise ActiveRoiResolutionError(f"Selected {selection} has an invalid GeoJSON geometries array.")
    if geojson_type not in {"Feature", "FeatureCollection", "GeometryCollection"} and "coordinates" not in value:
        raise ActiveRoiResolutionError(f"Selected {selection} has no GeoJSON coordinates.")
    return value


def _load_selected_geojson(path: Path, *, selection: str) -> dict[str, Any]:
    if not path.exists():
        raise ActiveRoiResolutionError(f"Selected {selection} does not exist: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ActiveRoiResolutionError(f"Selected {selection} could not be read as GeoJSON: {path}: {exc}") from exc
    return _validate_basic_geojson(value, selection=selection)


def _resolve_active_roi_geojson(session_id: str) -> tuple[dict, str]:
    """
    Resolve study-basin geometry for map / GEE tools.

    Priority:
    1. session.working_geometry_path (workspace file selected via map_set_working_geometry)
    2. Workspace roi/active.json → GeoJSON file
    3. Host map session ~/.aihydro/map_session.json active_roi
    4. HydroSession watershed (delineation)
    """
    from ai_hydro.session import HydroSession
    session = HydroSession.load(session_id)
    working = getattr(session, "working_geometry_path", None)
    ws_dir = session.workspace_dir
    if working:
        if not ws_dir:
            raise ActiveRoiResolutionError(
                "A working geometry is selected, but the session has no workspace_dir."
            )
        return _load_selected_geojson(
            Path(ws_dir) / working,
            selection="working geometry",
        ), "working_geometry"

    if ws_dir:
        pointer_path = Path(ws_dir) / "roi" / "active.json"
        if pointer_path.exists():
            try:
                pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ActiveRoiResolutionError(
                    f"Selected workspace ROI pointer could not be read: {pointer_path}: {exc}"
                ) from exc
            if not isinstance(pointer, dict) or not isinstance(pointer.get("path"), str) or not pointer["path"].strip():
                raise ActiveRoiResolutionError(
                    f"Selected workspace ROI pointer has no valid path: {pointer_path}"
                )
            return _load_selected_geojson(
                Path(ws_dir) / pointer["path"],
                selection="workspace ROI",
            ), "workspace_roi"

    map_session_path = Path.home() / ".aihydro" / "map_session.json"
    try:
        if map_session_path.exists():
            data = json.loads(map_session_path.read_text(encoding="utf-8"))
            active = data.get("activeRoi") or data.get("active_roi")
            ownership = _map_session_workspace_ownership(data, ws_dir, requested=True)
            if active and ownership["status"] == "matching":
                if not isinstance(active, dict):
                    raise ActiveRoiResolutionError("The matching host ROI record is malformed.")
                raw = active.get("geojson")
                if not raw:
                    raise ActiveRoiResolutionError("The matching host ROI has no GeoJSON geometry.")
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                except json.JSONDecodeError as exc:
                    raise ActiveRoiResolutionError(
                        f"The matching host ROI contains malformed GeoJSON: {exc}"
                    ) from exc
                return _validate_basic_geojson(parsed, selection="host ROI"), "map_session"
    except ActiveRoiResolutionError:
        raise
    except Exception as exc:
        log.debug("Map session ROI resolution skipped: %s", exc)

    return _get_session_geometry(session_id), "session_watershed"


def _workspace_write(session_id: str, filename: str, content: Any) -> str | None:
    """Write content to the workspace directory stored in HydroSession."""
    try:
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        return session.write_workspace_file(filename, content)
    except Exception as exc:
        log.debug("Workspace write skipped (%s): %s", filename, exc)
        return None


def _canonical_prefix(session_id: str, prefix: str) -> str:
    """Return a canonical filename prefix for artifacts.

    Tries to use the session's canonical site/gauge ID so outputs from the
    same study share a stable namespace.  Falls back to ``<session_id>_<prefix>``
    when the session cannot be loaded.

    Example: ``_canonical_prefix("01031500", "index_ndwi")``
             → ``"01031500_index_ndwi"``
    """
    try:
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        canonical_id = getattr(session, "canonical_id", None) or session_id
        return f"{canonical_id}_{prefix}"
    except Exception:
        return f"{session_id}_{prefix}"


def _canonical_workspace_path(session_id: str, prefix: str, ext: str = "json") -> str | None:
    """
    Return a workspace-relative filename using the session's canonical_id
    (preferring site_id, falling back to a slugified session_id). Use this
    in tools that write artifacts so a single study's outputs land in a
    consistent namespace regardless of how the tool was called.

    Returns None if the session can't be loaded (no fatal — caller should
    fall back to whatever local filename it had).
    """
    try:
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        return session.workspace_filename(prefix, ext)
    except Exception as exc:
        log.debug("canonical filename resolution failed: %s", exc)
        return None


def _ensure_session(session_id: str, workspace_dir: str | None = None):
    """Load (or create) a HydroSession. Store workspace_dir if new."""
    from ai_hydro.session import HydroSession
    session = HydroSession.load(session_id)
    if workspace_dir and session.workspace_dir != workspace_dir:
        session.workspace_dir = workspace_dir
        session.save()
    return session


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _sync_reminder(session_id: str) -> str | None:
    """
    Return a mandatory reminder to call write_research_interpretation when ≥2 slots
    are computed and no interpretation has been written yet.

    Injected into every analysis tool response so the LLM cannot miss it.
    Returns None when not yet relevant (< 2 computed, or already interpreted).
    """
    try:
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        n = len(session.computed())
        if n >= 2 and not session.interpretation:
            return (
                f"[{n} analyses complete, no interpretation yet] "
                f"When ALL planned steps are done, call "
                f"get_session_raw_state('{session_id}') then "
                f"write_research_interpretation('{session_id}', ...) to persist "
                "the scientific context across conversations."
            )
    except Exception:
        pass
    return None


def _tool_error_to_dict(e: Exception) -> dict:
    """
    Convert any exception to a structured agent-facing error envelope.

    For ToolError / AihydroDataError: uses their .to_dict() which carries
    code / message / recovery / next_tools — always fully populated.

    For raw / unexpected exceptions: emits a generic envelope that still
    includes recovery hints and next_tools so the agent is never stranded.
    The ``_traceback`` key is included for diagnostics but kept short.
    """
    if hasattr(e, "to_dict"):
        return e.to_dict()

    import traceback as _tb
    tb_short = _tb.format_exc(limit=5)

    return {
        "error":      True,
        "code":       "UNEXPECTED_ERROR",
        "message":    str(e) or repr(e),
        "recovery": (
            "This is an unexpected internal error. Check the tool arguments "
            "and try again. If it persists, call data_doctor() or check the "
            "server log."
        ),
        "next_tools": ["aihydro_describe_capability", "data_doctor"],
        "docs_anchor": "",
        "_traceback":  tb_short[-800:] if tb_short else "",
    }


def _cached_response(slot: str, session, *, extra: dict | None = None) -> dict:
    result = getattr(session, slot)
    r: dict = {
        "data": result.get("data", {}),
        "meta": result.get("meta", {}),
        "_cached": True,
        "_note": (
            f"Result loaded from session cache. "
            f"Call clear_session('{session.session_id}', ['{slot}']) to recompute."
        ),
        **(extra or {}),
    }
    reminder = _sync_reminder(session.session_id)
    if reminder:
        r["_sync_required"] = reminder
    return r


# ---------------------------------------------------------------------------
# aihydro-data interop shim
# ---------------------------------------------------------------------------

def _legacy_data_shim(
    variable: str,
    geometry: "Any",
    start: str,
    end: str,
    **kw: "Any",
) -> "tuple[Any | None, dict | None]":
    """
    Route a data fetch through ``aihydro_data.fetch()`` and return the
    FetchResult.

    On success  → ``(FetchResult, None)``
    On failure  → ``(None, {"_aihydro_data_unavailable": True, "detail": "…"})``

    Used by legacy MCP tools to attempt routing through the new variable-centric
    data layer while falling back gracefully if ``aihydro-data`` is unavailable,
    the variable is unsupported, or the remote fetch fails.

    Parameters
    ----------
    variable : str
        aihydro-data variable name (e.g. ``"streamflow"``, ``"precipitation"``).
    geometry : Any
        Geometry accepted by aihydro-data: shapely, GeoDataFrame, GeoJSON dict,
        ``(lat, lon)`` tuple, bounding-box 4-tuple, or a USGS gauge-ID string.
    start, end : str
        ISO-8601 date strings.  Pass ``""`` for static products.
    **kw
        Extra kwargs forwarded to ``aihydro_data.fetch()`` (e.g.
        ``mode="manual"``, ``product="CHIRPS"``).
    """
    try:
        from aihydro_data import fetch as _adata_fetch  # type: ignore[import]
        result = _adata_fetch(variable, geometry, start, end, **kw)
        return result, None
    except Exception as exc:
        return None, {"_aihydro_data_unavailable": True, "detail": str(exc)[:300]}


def _strip_forcing_arrays(data: dict) -> dict:
    """Remove large daily arrays from forcing data, keeping per-variable means."""
    compact: dict = {}
    var_means: dict = {}
    for k, v in data.items():
        if isinstance(v, list):
            valid = [x for x in v if x is not None and isinstance(x, (int, float))]
            if valid:
                var_means[f"{k}_mean"] = round(sum(valid) / len(valid), 4)
        else:
            compact[k] = v
    compact.update(var_means)
    if var_means:
        compact["n_variables"] = len(var_means)
    return compact
