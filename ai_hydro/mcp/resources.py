"""
MCP Resource layer for AI-Hydro.

Serves two families of resources via the native MCP resource protocol:

  Knowledge resources (static, no session required):
    aihydro://knowledge/library/{name}   — a library knowledge card
    aihydro://knowledge/list             — catalog of all knowledge cards

  Session resources (headless mode — no VS Code / chat binding required):
    aihydro://session/list               — all sessions on this machine
    aihydro://session/{session_id}       — summary of one session
    aihydro://session/{session_id}/claims         — claims ledger (JSON)
    aihydro://session/{session_id}/evidence_board — claims grouped by status
    aihydro://session/{session_id}/experiments    — experiment table slot

CLI and cloud agents can read session resources with an explicit session_id
without needing the VS Code extension or a chat binding.  The facade tools
(audit_interpretation, list_claims, etc.) work identically in both modes;
resources provide *read-only* snapshots optimised for programmatic consumption.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ai_hydro.mcp.app import mcp
from ai_hydro.session.store import SESSIONS_DIR

log = logging.getLogger("ai_hydro.mcp")


def _load_card(name: str) -> dict | None:
    """Load a library card from built-in dirs and plugin dirs, with drift detection."""
    from ai_hydro.knowledge import get_library_ref
    card = get_library_ref(name)
    if card is None:
        return None
    return _inject_drift_warning(card)


def _inject_drift_warning(card: dict) -> dict:
    """Compare card's version_compatible against the installed library version."""
    library = card.get("library", "")
    vc = card.get("version_compatible")
    if not library or not vc:
        return card
    try:
        from importlib.metadata import version as pkg_version
        from packaging.specifiers import SpecifierSet
        installed = pkg_version(library)
        spec = SpecifierSet(vc)
        if installed not in spec:
            card = dict(card)
            card["stale"] = True
            card["stale_reason"] = (
                f"Installed {library}=={installed} is outside the tested range {vc}. "
                "API details in this card may be inaccurate. Update the library or consult "
                "the package changelog before using these patterns."
            )
    except Exception:
        pass  # packaging not installed, or library not found — serve card as-is
    return card


def _list_all_library_names() -> list[str]:
    """Return all available library card names across built-in + plugin sources."""
    from ai_hydro.knowledge import list_library_refs
    return list_library_refs()


@mcp.resource("aihydro://knowledge/library/{name}")
def library_card(name: str) -> str:
    """Serve a library knowledge card as a JSON string resource."""
    card = _load_card(name.lower())
    if card is None:
        return json.dumps({
            "error": True,
            "code": "NOT_FOUND",
            "message": f"No reference available for '{name}'.",
            "available": _list_all_library_names(),
        })
    return json.dumps(card, indent=2)


@mcp.resource("aihydro://knowledge/list")
def knowledge_catalog() -> str:
    """Return a catalog of all available knowledge resources."""
    names = _list_all_library_names()
    return json.dumps({
        "library_cards": names,
        "n_library_cards": len(names),
        "uri_pattern": "aihydro://knowledge/library/{name}",
        "facade_tool": "get_library_reference",
    }, indent=2)


# ---------------------------------------------------------------------------
# Session resource helpers
# ---------------------------------------------------------------------------

def _load_session(session_id: str):
    """Load HydroSession by ID, or return None when the session file does not exist."""
    try:
        import ai_hydro.session.store as _store
        path = _store._SESSIONS_DIR / f"{session_id}.json"
        if not path.exists():
            return None
        return _store.HydroSession.load(session_id)
    except Exception:
        return None


def _session_not_found(session_id: str) -> str:
    return json.dumps({
        "error": True,
        "code": "SESSION_NOT_FOUND",
        "message": f"Session '{session_id}' not found.",
        "hint": "Use aihydro://session/list to enumerate available sessions.",
    })


def _claim_status_order() -> list[str]:
    return ["proposed", "tested", "weakly_supported", "supported", "contradicted",
            "retracted", "stale"]


# ---------------------------------------------------------------------------
# Session list resource
# ---------------------------------------------------------------------------

@mcp.resource("aihydro://session/list")
def session_list() -> str:
    """List all AI-Hydro sessions available on this machine.

    Headless-safe: no VS Code or chat binding required.
    """
    import ai_hydro.session.store as _store
    sessions_dir = _store._SESSIONS_DIR
    if not sessions_dir.exists():
        return json.dumps({"sessions": [], "n_sessions": 0})

    entries: list[dict] = []
    for path in sorted(sessions_dir.glob("*.json")):
        sid = path.stem
        try:
            s = _store.HydroSession.load(sid)
            entries.append({
                "session_id": s.session_id,
                "site_id": s.site_id or None,
                "site_name": s.site_name or None,
                "n_claims": len(s.claims),
                "n_experiments": len(s.get("_experiments") or {}),
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "archived": s.archived,
                "uri": f"aihydro://session/{sid}",
            })
        except Exception:
            entries.append({"session_id": sid, "error": "could not load"})

    return json.dumps({"sessions": entries, "n_sessions": len(entries)}, indent=2)


# ---------------------------------------------------------------------------
# Session summary resource
# ---------------------------------------------------------------------------

@mcp.resource("aihydro://session/{session_id}")
def session_summary(session_id: str) -> str:
    """Serve a compact summary of one AI-Hydro session.

    Headless-safe: works with an explicit session_id from any client.
    """
    s = _load_session(session_id)
    if s is None:
        return _session_not_found(session_id)

    claims_by_status: dict[str, int] = {}
    for c in s.claims.values():
        st = c.get("status", "unknown")
        claims_by_status[st] = claims_by_status.get(st, 0) + 1

    exps = s.get("_experiments") or {}

    return json.dumps({
        "session_id": s.session_id,
        "site_id": s.site_id or None,
        "site_name": s.site_name or None,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "archived": s.archived,
        "n_claims": len(s.claims),
        "claims_by_status": claims_by_status,
        "n_experiments": len(exps),
        "has_interpretation": bool(s.interpretation),
        "resources": {
            "claims": f"aihydro://session/{session_id}/claims",
            "evidence_board": f"aihydro://session/{session_id}/evidence_board",
            "experiments": f"aihydro://session/{session_id}/experiments",
        },
    }, indent=2)


# ---------------------------------------------------------------------------
# Claims ledger resource
# ---------------------------------------------------------------------------

@mcp.resource("aihydro://session/{session_id}/claims")
def session_claims(session_id: str) -> str:
    """Serve the full claims ledger for a session as a JSON resource.

    Headless-safe: CLI and cloud agents can read claims without VS Code.
    Each claim dict is the exact representation stored in the session, so
    run_ids and evidence_spans are fully traceable.
    """
    s = _load_session(session_id)
    if s is None:
        return _session_not_found(session_id)

    claims_list = sorted(s.claims.values(), key=lambda c: c.get("created_at", ""))
    return json.dumps({
        "session_id": session_id,
        "n_claims": len(claims_list),
        "claims": claims_list,
    }, indent=2)


# ---------------------------------------------------------------------------
# Evidence board resource (kanban view of claims by status)
# ---------------------------------------------------------------------------

_BOARD_STATUS_LABELS: dict[str, str] = {
    "proposed": "Proposed",
    "tested": "Tested",
    "weakly_supported": "Weakly supported",
    "supported": "Supported",
    "contradicted": "Contradicted",
    "retracted": "Retracted",
    "stale": "Stale",
}


@mcp.resource("aihydro://session/{session_id}/evidence_board")
def session_evidence_board(session_id: str) -> str:
    """Serve the evidence board state for a session.

    Returns claims grouped by status in kanban column order, mirroring the
    VS Code EvidenceBoard panel.  Headless-safe: any MCP client can poll
    this resource to track claim lifecycle without the extension UI.
    """
    s = _load_session(session_id)
    if s is None:
        return _session_not_found(session_id)

    # Group claims into ordered columns
    buckets: dict[str, list[dict]] = {st: [] for st in _BOARD_STATUS_LABELS}
    unknown: list[dict] = []
    for claim in s.claims.values():
        st = claim.get("status", "")
        if st in buckets:
            buckets[st].append(claim)
        else:
            unknown.append(claim)

    columns = []
    for st, label in _BOARD_STATUS_LABELS.items():
        column_claims = sorted(buckets[st], key=lambda c: c.get("created_at", ""))
        columns.append({
            "status": st,
            "label": label,
            "n": len(column_claims),
            "claims": column_claims,
        })

    return json.dumps({
        "session_id": session_id,
        "n_claims": len(s.claims),
        "columns": columns,
        "n_unknown_status": len(unknown),
        "unknown": unknown,
    }, indent=2)


# ---------------------------------------------------------------------------
# Experiments resource
# ---------------------------------------------------------------------------

@mcp.resource("aihydro://session/{session_id}/experiments")
def session_experiments(session_id: str) -> str:
    """Serve the experiment table slot for a session.

    Returns all experiments defined or run in this session, with their
    design matrices, feature rows, and per-cell run_ids.
    Headless-safe: CLI agents can read results without VS Code.
    """
    s = _load_session(session_id)
    if s is None:
        return _session_not_found(session_id)

    exps: dict = s.get("_experiments") or {}
    exp_list = []
    for exp_id, exp_data in exps.items():
        if isinstance(exp_data, dict):
            exp_list.append(exp_data)

    exp_list.sort(key=lambda e: e.get("definition", {}).get("created_at", ""))

    return json.dumps({
        "session_id": session_id,
        "n_experiments": len(exp_list),
        "experiments": exp_list,
    }, indent=2)
