"""
Shared helper functions for MCP tool implementations.
"""
from __future__ import annotations

import json
import logging
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
        setattr(session, slot, result_dict)
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


def _resolve_active_roi_geojson(session_id: str) -> tuple[dict, str]:
    """
    Resolve study-basin geometry for map / GEE tools.

    Priority:
    1. session.working_geometry_path (workspace file selected via map_set_working_geometry)
    2. Workspace roi/active.json → GeoJSON file
    3. Host map session ~/.aihydro/map_session.json active_roi
    4. HydroSession watershed (delineation)
    """
    try:
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        working = getattr(session, "working_geometry_path", None)
        if working and session.workspace_dir:
            geo_path = Path(session.workspace_dir) / working
            if geo_path.exists():
                with open(geo_path, encoding="utf-8") as gf:
                    return json.load(gf), "working_geometry"
        ws_dir = session.workspace_dir
        if ws_dir:
            pointer_path = Path(ws_dir) / "roi" / "active.json"
            if pointer_path.exists():
                with open(pointer_path, encoding="utf-8") as f:
                    pointer = json.load(f)
                rel = pointer.get("path")
                if rel:
                    geo_path = Path(ws_dir) / rel
                    if geo_path.exists():
                        with open(geo_path, encoding="utf-8") as gf:
                            return json.load(gf), "workspace_roi"
    except Exception as exc:
        log.debug("Workspace ROI resolution skipped: %s", exc)

    map_session_path = Path.home() / ".aihydro" / "map_session.json"
    try:
        if map_session_path.exists():
            data = json.loads(map_session_path.read_text(encoding="utf-8"))
            active = data.get("activeRoi") or data.get("active_roi")
            if active:
                raw = active.get("geojson")
                if raw:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(parsed, dict):
                        return parsed, "map_session"
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
    if hasattr(e, "to_dict"):
        return e.to_dict()
    return {"error": True, "code": "UNKNOWN_ERROR", "message": str(e)}


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
