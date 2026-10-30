"""
Shared helper functions for MCP tool implementations.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_hydro.registry.paths import aihydro_home

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
        candidates: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.recovery = recovery
        self.next_tools = next_tools or []
        self.candidates = candidates or []

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
        if self.candidates:
            d["candidates"] = self.candidates
        return d


def _read_active_workspace_file() -> str | None:
    """
    Read ~/.aihydro/active_workspace.json written by the VS Code extension on
    activation.  Returns the workspace path, or None if the file is absent or
    the path no longer exists on disk.
    """
    try:
        import json as _json
        ws_file = aihydro_home() / "active_workspace.json"
        if not ws_file.exists():
            return None
        data = _json.loads(ws_file.read_text())
        ws = data.get("workspace")
        if ws and Path(ws).is_dir():
            return ws
    except Exception:
        pass
    return None


def _call_workspace() -> str | None:
    """The workspace of THIS call: the per-call context first; the machine-global
    ``active_workspace.json`` only when the call carries no context of its own.

    The global file is written by whichever VS Code window was focused last, so with two
    windows on two projects it names the wrong one. A call that sent ``_meta`` (or the legacy
    hidden arguments) is therefore never answered from it, even when that context holds no workspace.
    """
    from ai_hydro.mcp.app import ACTIVE_CONTEXT_SOURCE, ACTIVE_WORKSPACE

    ws = ACTIVE_WORKSPACE.get()
    if ws and Path(ws).is_dir():
        return ws
    if ACTIVE_CONTEXT_SOURCE.get() == "none":
        return _read_active_workspace_file()
    return None


def _maybe_set_workspace(session: "Any") -> None:
    """
    If the session has no workspace_dir yet, resolve it from (in priority order):
      1. ACTIVE_WORKSPACE ContextVar   - set per call from ``_meta`` (or the legacy hidden argument)
      2. ~/.aihydro/active_workspace.json - written on every extension activation; used ONLY when
         the call carries no context of its own (see ``_call_workspace``)

    Having a file-based fallback means the workspace is available even when the
    extension has not been reloaded after a VSIX install (no window-reload ceremony).
    """
    try:
        if session.workspace_dir:
            return  # already set - don't override
        from ai_hydro.mcp.app import ACTIVE_WORKSPACE
        ws = _call_workspace()
        if ws and Path(ws).is_dir():
            session.workspace_dir = ws
            session.save()
            log.debug("Set workspace_dir=%s on session %s (source=%s)",
                      ws, session.session_id,
                      "injection" if ACTIVE_WORKSPACE.get() else "activation_file")
    except Exception as exc:
        log.debug("_maybe_set_workspace failed (non-fatal): %s", exc)


_FALLBACK_ON_VALUES = frozenset({"on", "1", "true", "yes"})


def recent_fallback_enabled() -> bool:
    """True only when ``AIHYDRO_RECENT_SESSION_FALLBACK`` is on/1/true/yes, and never in an evaluation run.

    The global "most recent study on disk" guess is OFF by default (2.8.2; owner decision, 2026-10-28):
    it picked test and unrelated studies. The variable is kept for one release as an opt-in and then
    removed; the project default replaces it. An evaluation run is one the P1 harness started
    (see eval_condition.in_evaluation_run): a guessed study would silently change what an arm computes.
    """
    from ai_hydro.mcp.eval_condition import in_evaluation_run

    if in_evaluation_run():
        return False
    return os.environ.get("AIHYDRO_RECENT_SESSION_FALLBACK", "").strip().lower() in _FALLBACK_ON_VALUES


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
    allow_recent_fallback: bool = True,
) -> str:
    """
    Resolve which study (HydroSession) a tool call should operate on.

    Priority chain
    --------------
    1. **Explicit** ``session_id`` - always wins (rule ``explicit_arg``).
       Also rebinds the chat to this study if ``chat_id`` is provided and the tool is not read-only.
    1b. **Request context** - ``study_id`` from ``_meta["aihydro/context"]`` (rule ``meta``); same rebinding.
    2. **Chat binding** (rule ``chat_binding``) - the study bound to *chat_id* in
       ``chat_bindings.v2.json``. Many chats may share one study.
    3. **Auto-create** (rule ``auto_create``) - if *auto_create_hint* is set (a gauge ID,
       lat/lon slug or explicit name) and *allow_auto_create* is True, create the study and bind it.
    4. **Project default** (rule ``project_default``) - the CONFIRMED default study of the call's project
       (``_meta.project_id``, else the project of the call's workspace). Same gating as step 5. A default
       nobody confirmed (a suggestion) never resolves; it is only listed as a candidate in step 6.
    5. **Single study in the workspace** (rule ``workspace_single_study``) - exactly one visible
       study whose ``workspace_dir`` is this call's workspace. Inferred, so closed to the audit
       (``allow_recent_fallback=False``), to evaluation runs, and to state-changing admin tools
       (``allow_auto_create=False`` and not readOnlyHint). A read-only call does not bind.
    5b. **Most recent study** on disk (rule ``recent_fallback``): OFF unless
       ``AIHYDRO_RECENT_SESSION_FALLBACK=on``; kept for one release.
    6. **Error** - ``SessionResolutionError`` listing at most 5 labelled candidate studies
       and ``next_tools`` naming ``aihydro_rebind_chat``.

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
        silently create sessions.  Also disables the recent-study guess, and the
        workspace inference unless the tool is read-only.
    allow_recent_fallback:
        Set to ``False`` to forbid every inferred resolution (steps 5 and 5b), as the audit does.
        An evaluation run forbids them for every tool.  When the recent-study guess
        fires, the result carries a top-level ``session_warning``.

    Returns
    -------
    str
        A normalised session_id ready for ``HydroSession.load()``.  Use
        ``_resolve_session_with_rule`` to also get the rule used; the rule is
        always noted on the in-flight run record
        (``extra.session_resolution``).
    """
    return _resolve_session_with_rule(
        session_id, chat_id,
        auto_create_hint=auto_create_hint, allow_auto_create=allow_auto_create,
        allow_recent_fallback=allow_recent_fallback,
    )[0]


def _resolve_session_with_rule(
    session_id: str | None,
    chat_id: str | None = None,
    *,
    auto_create_hint: str | None = None,
    allow_auto_create: bool = True,
    allow_recent_fallback: bool = True,
) -> tuple[str, str]:
    """``(session_id, rule)``; see ``_resolve_session`` for the chain."""
    sid, rule = _resolve_session_impl(
        session_id, chat_id,
        auto_create_hint=auto_create_hint, allow_auto_create=allow_auto_create,
        allow_recent_fallback=allow_recent_fallback,
    )
    try:
        from ai_hydro.session.run_records import note_session_resolution

        note_session_resolution(rule)
    except Exception:  # recording must never break resolution
        pass
    return sid, rule


def note_binding_change(chat_id: str, study_id: str, previous: str | None, bound_by: str) -> None:
    """Record, on the call's notes, that a chat's binding changed.

    ``_SessionWarningMiddleware`` adds it to the result as ``session_binding``, which is how the
    host learns about a server-side bind (an auto-create on delineation, a rebind). The host mirror
    is display-only: this field is the only way it is refreshed. No-op outside a tool call.
    """
    from ai_hydro.mcp.app import ACTIVE_CALL_NOTES

    notes = ACTIVE_CALL_NOTES.get()
    if notes is None:
        return
    try:
        from ai_hydro.session.study_index import label_of

        label = label_of(study_id)
    except Exception:
        label = study_id
    notes["session_binding"] = {
        "chat_id": chat_id, "study_id": study_id, "label": label,
        "previous_study_id": previous, "bound_by": bound_by,
    }


def _bind(store, chat_id: str, study_id: str, bound_by: str) -> None:
    """Bind and report the change; a failed bind (invalid chat id, I/O) never fails the call."""
    try:
        previous = store.lookup_study(chat_id)
        if store.bind(chat_id, study_id, bound_by=bound_by):
            note_binding_change(chat_id, study_id, previous, bound_by)
            log.debug("Bound chat=%s -> study=%s (%s)", chat_id, study_id, bound_by)
    except Exception as exc:
        from ai_hydro.session.chat_binding import InvalidChatIdError

        if isinstance(exc, InvalidChatIdError):      # not silent: the chat stays unbound
            log.warning("Chat binding skipped (%s): %s", bound_by, exc)
        else:
            log.debug("Could not persist chat binding (%s): %s", bound_by, exc)
        return
    _link_to_chat_project(study_id)


def _link_to_chat_project(study_id: str) -> None:
    """Add a study the chat was just bound to into the chat's project (T-P1). Never fails the call.

    The project is the one named by ``_meta.project_id``, else the one of the call's own workspace
    (created when there is none). The machine-global ``active_workspace.json`` is not used: it may
    name another window's folder. Not done in an evaluation run.
    """
    try:
        if _in_eval():
            return
        from ai_hydro.mcp.app import ACTIVE_PROJECT_ID, ACTIVE_WORKSPACE
        from ai_hydro.session.project_registry import get_project_registry

        ws, pid = ACTIVE_WORKSPACE.get(), ACTIVE_PROJECT_ID.get()
        if not ws and not pid:
            return
        get_project_registry().note_study_bound(ws, study_id, pid)
    except Exception as exc:
        log.debug("Could not add study %s to the chat's project (non-fatal): %s", study_id, exc)


def _with_suggested_default(cands: list[dict]) -> list[dict]:
    """Put the project's UNCONFIRMED default first, marked ``suggested_default``, at most 5 rows in all.

    A suggestion is only ever a candidate: nothing here (or anywhere in the chain) resolves it.
    """
    try:
        from ai_hydro.mcp.app import ACTIVE_PROJECT_ID
        from ai_hydro.session.project_registry import get_project_registry

        sug = get_project_registry().suggested_default(workspace=_call_workspace(), project_id=ACTIVE_PROJECT_ID.get())
    except Exception as exc:
        log.debug("suggested default skipped: %s", exc)
        return cands
    if not sug:
        return cands
    return [sug] + [c for c in cands if c.get("study_id") != sug["study_id"]][:4]


def _resolution_failure(chat_id: str | None) -> SessionResolutionError:
    """The step-6 error: labelled candidates (at most 5) and a recovery tool that works."""
    from ai_hydro.session.study_index import candidates_for

    try:
        cands, scope = candidates_for(_call_workspace(), limit=5)
    except Exception as exc:
        log.debug("candidate listing failed: %s", exc)
        cands, scope = [], "none"
    cands = _with_suggested_default(cands)
    where = "in this workspace" if scope == "workspace" else "recently active (other workspaces)"
    parts = ["Cannot determine which study to use: no session_id, no chat binding, "
             "and no unambiguous study for this call."]
    if cands:
        parts.append(f"Candidate studies {where}: "
                     + "; ".join(f"{c['study_id']} = {c['label']}" + (" (suggested default, not confirmed)"
                                                                      if c.get("suggested_default") else "")
                                 for c in cands) + ".")
    if chat_id:
        how = ("Pick one and call aihydro_rebind_chat(study_id=<id>) to bind this chat, "
               "or pass session_id=<id> to this call. ")
    else:
        how = "This call carries no chat context: pass session_id=<id> explicitly. "
    recovery = how + "To start a new study, run delineate_watershed or delineate_watershed_from_point."
    return SessionResolutionError(
        " ".join(parts),
        recovery=recovery,
        next_tools=["aihydro_rebind_chat", "aihydro_chat_status", "start_session",
                    "delineate_watershed", "delineate_watershed_from_point"],
        candidates=cands,
    )


def _resolve_session_impl(
    session_id: str | None,
    chat_id: str | None,
    *,
    auto_create_hint: str | None,
    allow_auto_create: bool,
    allow_recent_fallback: bool = True,
) -> tuple[str, str]:
    # Wave 3 Axis 3: if no chat_id was passed explicitly, read from the
    # per-request ContextVar populated by _ContextInjectionMiddleware (from
    # _meta, else the legacy hidden args).  No-op when it holds None (direct
    # Python calls, tests, CLI invocations).
    from ai_hydro.mcp.app import ACTIVE_CALL_NOTES, ACTIVE_CHAT_ID, ACTIVE_READ_ONLY, ACTIVE_STUDY_ID

    # A tool annotated readOnlyHint=true must not change anything by resolving its session: no
    # chat rebinding, no workspace_dir backfill (which saves the session and bumps its mtime).
    read_only = ACTIVE_READ_ONLY.get()

    if chat_id is None:
        chat_id = ACTIVE_CHAT_ID.get()
    rule = "explicit_arg"
    if not session_id:
        session_id = ACTIVE_STUDY_ID.get()
        rule = "meta"
    from ai_hydro.session.chat_binding import get_binding_store

    store = get_binding_store()

    # Guard: hard-reject the legacy 'map' placeholder so it never silently
    # overwrites the global map session or obscures a real chat binding.
    if session_id and _normalize_session_id(session_id) == "map":
        raise SessionResolutionError(
            "'map' is a reserved legacy placeholder and cannot be used as a session_id. "
            "Omit session_id entirely - it is auto-resolved from the chat context via "
            "Wave 3 chat binding.",
            recovery=(
                "Remove session_id='map' from your tool call. "
                "Use aihydro_chat_status() to inspect the current binding. "
                "Use aihydro_rebind_chat(study_id) to switch to a specific study."
            ),
            next_tools=["aihydro_chat_status", "aihydro_rebind_chat"],
        )

    # 1. Explicit session_id (or _meta study_id) -> use it; optionally rebind chat
    if session_id:
        sid = _normalize_session_id(session_id)
        if chat_id and not read_only:
            # Only a study that already exists is bound: a typo or a made-up id must not become the chat's study.
            from ai_hydro.session.study_index import study_exists

            if study_exists(sid):
                _bind(store, chat_id, sid, rule)
            else:
                log.info("Explicit session_id %r does not exist; chat %s not bound to it", sid, chat_id)
        return sid, rule

    # 2. Chat binding lookup
    if chat_id:
        bound = store.lookup_study(chat_id)
        if bound:
            log.debug("Resolved study=%s from chat binding chat=%s", bound, chat_id)
            # Opportunistically backfill workspace_dir if the session was
            # created before _workspace injection was wired.
            if not read_only:
                try:
                    from ai_hydro.session import HydroSession
                    _maybe_set_workspace(HydroSession.load(bound))
                except Exception:
                    pass
            return bound, "chat_binding"

    # 3. Auto-create from hint
    if auto_create_hint and allow_auto_create:
        from ai_hydro.session import HydroSession
        sid = _normalize_session_id(auto_create_hint)
        try:
            session = HydroSession.load(sid)  # creates if not exists
            # Populate workspace_dir from the injected _workspace ContextVar
            # so file outputs (TWI, geomorphic, etc.) land in the VS Code project.
            if not read_only:
                _maybe_set_workspace(session)
        except Exception as exc:
            log.debug("Auto-create session %s: %s", sid, exc)
        if chat_id and not read_only:
            _bind(store, chat_id, sid, "auto_create")
        return sid, "auto_create"

    # 4. The project's CONFIRMED default study (B4). Scoped to this call's project (`_meta.project_id`, else the
    # project of the call's own workspace), deterministic, and a declared scope rather than a guess: allowed for
    # read-only calls (which do not bind). Closed to the audit, evaluation runs and state-changing admin tools,
    # like step 5. An UNCONFIRMED default never resolves here; it is only offered as a candidate in step 6.
    if allow_recent_fallback and (allow_auto_create or read_only) and not _in_eval():
        try:
            from ai_hydro.mcp.app import ACTIVE_PROJECT_ID, ACTIVE_WORKSPACE
            from ai_hydro.session.project_registry import get_project_registry

            ws4 = ACTIVE_WORKSPACE.get()           # the call's own workspace only, never the machine-global file
            default = get_project_registry().confirmed_default(workspace=ws4, project_id=ACTIVE_PROJECT_ID.get())
            if default:
                sid, _pid = default
                if chat_id and not read_only:
                    _bind(store, chat_id, sid, "project_default")
                if not read_only:
                    try:
                        from ai_hydro.session import HydroSession
                        _maybe_set_workspace(HydroSession.load(sid))
                    except Exception:
                        pass
                notes = ACTIVE_CALL_NOTES.get()
                if notes is not None:
                    from ai_hydro.session.study_index import label_of

                    notes["project_default"] = {"study_id": sid, "label": label_of(sid)}
                return sid, "project_default"
        except Exception as exc:
            log.debug("Project-default resolution skipped: %s", exc)

    # 5. Exactly one study in this call's workspace. Inferred, so it is closed to the audit
    # (allow_recent_fallback=False), to evaluation runs, and to admin tools that change state
    # (they pass allow_auto_create=False and are not readOnlyHint): those need a named or bound study.
    # A read-only call resolves it without binding; a writer binds the chat and the result says so.
    if allow_recent_fallback and (allow_auto_create or read_only) and not _in_eval():
        try:
            from ai_hydro.session.study_index import study_summaries

            ws = _call_workspace()
            from ai_hydro.mcp.app import ACTIVE_WORKSPACE

            # A workspace taken from the machine-global file (no call context) may belong to another window:
            # fine to read from, never to bind or write through.
            if ws and not ACTIVE_WORKSPACE.get() and not read_only:
                ws = None
            here = study_summaries(workspace=ws) if ws else []
            if len(here) == 1:
                sid = here[0]["study_id"]
                if chat_id and not read_only:
                    _bind(store, chat_id, sid, "workspace_single_study")
                if not read_only:
                    try:
                        from ai_hydro.session import HydroSession
                        _maybe_set_workspace(HydroSession.load(sid))
                    except Exception:
                        pass
                notes = ACTIVE_CALL_NOTES.get()
                if notes is not None:
                    notes["workspace_single_study"] = {"study_id": sid, "label": here[0]["label"]}
                return sid, "workspace_single_study"
        except Exception as exc:
            log.debug("Single-study workspace resolution skipped: %s", exc)

    # 5b. Most-recent-session guess. OFF unless AIHYDRO_RECENT_SESSION_FALLBACK=on (kept one release).
    if allow_auto_create and allow_recent_fallback and recent_fallback_enabled():
        try:
            from ai_hydro.session.study_index import study_summaries

            recent = study_summaries()
            if recent:
                sid = recent[0]["study_id"]
                log.info(
                    "No session_id and no chat binding; falling back to most "
                    "recent study '%s'. Pass session_id to target another.", sid
                )
                if chat_id and not read_only:
                    _bind(store, chat_id, sid, "recent_fallback")
                if not read_only:
                    try:
                        from ai_hydro.session import HydroSession
                        _maybe_set_workspace(HydroSession.load(sid))
                    except Exception:
                        pass
                notes = ACTIVE_CALL_NOTES.get()
                if notes is not None:          # the middleware adds a visible `session_warning`
                    notes.setdefault("recent_fallback_session", sid)
                return sid, "recent_fallback"
        except Exception as exc:
            log.debug("Most-recent-session fallback skipped: %s", exc)

    # 6. Failure: labelled candidates and a recovery tool that works
    raise _resolution_failure(chat_id)


def _in_eval() -> bool:
    from ai_hydro.mcp.eval_condition import in_evaluation_run

    return in_evaluation_run()


# ---------------------------------------------------------------------------
# Session identity helpers
# ---------------------------------------------------------------------------

def _normalize_session_id(session_id: str | None) -> str:
    """
    Accept any string as a session identifier.

    - Non-empty string → returned as-is (slugs, UUIDs, gauge IDs all valid)
    - None / empty → a minted ``st_<ULID>`` id (the study's label comes from its site, never the id)
    """
    if session_id and str(session_id).strip():
        return str(session_id).strip()
    from ai_hydro.registry.ids import mint_study_id
    return mint_study_id()


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
    """Write a deterministic run-log entry for a stored result (identical rewrites are no-ops)."""
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
    entry = {
        "run_id": run_id,
        "tool_name": resolved_tool,
        "session_id": session.session_id,
        "timestamp": timestamp,
        "key_outputs": _lean_key_outputs(result_dict),
        "slot": slot,
    }
    from ai_hydro.session.evidence import capture_result_evidence
    entry["evidence"] = capture_result_evidence(result_dict)
    # One row, written directly: re-sending the whole log would touch every
    # existing (possibly sealed) row for no reason.
    from ai_hydro.session.store import _run_log_record
    _run_log_record(session.session_id, run_id, entry, writer="helpers")


def _session_store(
    session_id: str, slot: str, result_dict: dict, *, tool_name: str | None = None
) -> None:
    """Cache a tool result in HydroSession (does NOT rewrite research.md; see DECISIONS 2026-10-20).

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


class MissingPrerequisiteError(RuntimeError):
    """A tool was called before the step whose output it needs.

    Distinct from an unexpected failure: the caller did nothing wrong in the
    tool arguments, it skipped a step. ``to_dict`` returns the code
    ``MISSING_PREREQUISITES`` (the vocabulary the tools already use for the
    same condition) with ``next_tools`` naming the step to run.
    """

    code = "MISSING_PREREQUISITES"

    def __init__(self, message: str, *, next_tools: list[str], recovery: str = ""):
        super().__init__(message)
        self.next_tools = list(next_tools)
        self.recovery = recovery or f"Run {', '.join(self.next_tools)} first."

    def to_dict(self) -> dict:
        return {
            "error": True,
            "code": self.code,
            "message": str(self),
            "recovery": self.recovery,
            "next_tools": list(self.next_tools),
            "docs_anchor": "",
        }


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
            raise MissingPrerequisiteError(
                f"No watershed cached for session '{session_id}'. "
                "Run delineate_watershed first.",
                next_tools=["delineate_watershed"],
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

    map_session_path = aihydro_home() / "map_session.json"
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
        n = len(session.computed_healthy())     # a degraded slot is not a completed analysis
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
    The traceback is written to the server log and is NOT part of the envelope,
    and absolute local paths in the text are scrubbed (see ``error_hygiene``).
    """
    from ai_hydro.mcp.error_hygiene import sanitize_error

    if hasattr(e, "to_dict"):
        return sanitize_error(e.to_dict())

    # An optional sibling package that is not installed is a setup condition, not a bug.
    if isinstance(e, ModuleNotFoundError) and (e.name or "").split(".")[0] == "aihydro_modelling":
        from aihydro_core.primitives.errors import ToolError

        return sanitize_error(ToolError(
            "MODELLING_NOT_INSTALLED",
            "This tool needs the optional aihydro-modelling package, which is not installed.",
            recovery='Install the modelling extra: pip install "aihydro-tools[modelling]" '
                     "(requires access to aihydro-modelling >= 0.6.1).",
        ).to_dict())

    import traceback as _tb
    log.error("unexpected tool error: %s\n%s", e, _tb.format_exc(limit=8))

    return sanitize_error({
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
    })


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
    On a transient or unsupported failure
                → ``(None, {"_aihydro_data_unavailable": True, "code": "…", "detail": "…"})``
    On a terminal refusal (``TerminalSourceError``: GAUGE_AMBIGUOUS, GAUGE_NOT_FOUND,
    USGS_NO_DATA, ...) → the exception **propagates**.

    A terminal refusal means "I looked and this gauge/series cannot be established".
    Folding it into the ``(None, err)`` fallback return let callers re-answer the
    request with another fetcher (Wave U D2); it must reach the caller as a refusal.
    Only "could not look" (aihydro-data absent, variable unsupported, transport
    failure) is returned as ``err`` for the caller to handle.

    Parameters
    ----------
    variable : str
        aihydro-data variable name (e.g. ``"streamflow"``, ``"precipitation"``).
    geometry : Any
        Geometry accepted by aihydro-data: shapely, GeoDataFrame, GeoJSON dict,
        ``{"lat", "lon"}`` dict, bounding-box, or a USGS gauge-ID string. The MCP tools refuse a bare
        coordinate pair (no axis order); this internal shim does not check, callers must pass an unambiguous form.
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
        try:
            from aihydro_data.exceptions import TerminalSourceError
            if isinstance(exc, TerminalSourceError):
                raise
        except ImportError:
            pass
        return None, {
            "_aihydro_data_unavailable": True,
            "code": getattr(exc, "code", None),
            "detail": str(exc)[:300],
        }


def _strip_forcing_arrays(data: dict) -> dict:
    """Remove large daily arrays from forcing data, keeping per-variable means.

    Only DATE and NUMERIC arrays are dropped. A list of strings or records is metadata
    (``variables``, ``_warnings``, ...) and is kept: dropping it silently
    deleted the warnings that say a variable failed. Means are over the FINITE
    days (a missing day is not zero); ``variable_coverage`` says how many days
    each mean rests on.
    """
    import math
    compact: dict = {}
    var_means: dict = {}
    coverage: dict = {}
    for k, v in data.items():
        if isinstance(v, list):
            if k != "dates" and v and all(isinstance(x, (str, dict)) for x in v):
                compact[k] = v                       # variables, _warnings, ...
                continue
            valid = [x for x in v if isinstance(x, (int, float)) and not isinstance(x, bool)
                     and math.isfinite(x)]
            if k != "dates" and v:
                coverage[k] = {"n_days": len(v), "n_finite": len(valid),
                               "n_missing": len(v) - len(valid)}
            if valid:
                var_means[f"{k}_mean"] = round(sum(valid) / len(valid), 4)
        else:
            compact[k] = v
    compact.update(var_means)
    if var_means:
        compact["n_variables"] = len(var_means)
    if coverage:
        compact["variable_coverage"] = coverage
    return compact
