"""
HTML Preview orchestration MCP tools.

These tools let the agent observe and react to what's happening inside the
AI-Hydro HTML Preview panel:

  - `preview_get_state(module_id?)` — snapshot of cells, manifest, recent errors
  - `preview_recent_events(module_id?, since_seq?, kind_filter?)` — event stream
  - `preview_list_modules()` — which modules are currently open
  - `preview_focus_cell(module_id, cell_id)` — scroll + highlight a cell
  - `preview_revise_section(module_id, section_id, new_html)` — apply a section edit

All read via the file bridge at ~/.aihydro/preview_session/ and
~/.aihydro/preview_events/ written by the host's PreviewSessionService.

Commands are dropped at ~/.aihydro/preview_commands/ where the host's
PreviewCommandWatcher picks them up.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict
from ai_hydro.mcp.preview_commands import (
    push_address_comment,
    push_focus_cell,
    push_revise_section,
)

log = logging.getLogger("ai_hydro.mcp")

_PREVIEW_SESSION_DIR = Path.home() / ".aihydro" / "preview_session"
_PREVIEW_EVENTS_DIR = Path.home() / ".aihydro" / "preview_events"
_COMMENTS_DIR = Path.home() / ".aihydro" / "comments"


def _safe_module_id(module_id: str) -> str:
    """Match the host's filename sanitisation."""
    out = "".join(c if c.isalnum() or c in "._-" else "_" for c in module_id)
    return out or "unknown"


def _read_session(module_id: str) -> dict[str, Any] | None:
    safe = _safe_module_id(module_id)
    fp = _PREVIEW_SESSION_DIR / f"{safe}.json"
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None


def _list_open_modules() -> list[str]:
    if not _PREVIEW_SESSION_DIR.is_dir():
        return []
    return [p.stem for p in _PREVIEW_SESSION_DIR.glob("*.json")]


def _read_events(
    module_id: str,
    since_seq: int = 0,
    kind_filter: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    safe = _safe_module_id(module_id)
    dir_path = _PREVIEW_EVENTS_DIR / safe
    if not dir_path.is_dir():
        return []
    files = sorted(dir_path.glob("*.json"))
    out: list[dict[str, Any]] = []
    for fp in files:
        try:
            event = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        seq = int(event.get("eventSeq", 0))
        if seq <= since_seq:
            continue
        if kind_filter and event.get("kind") != kind_filter:
            continue
        out.append(event)
        if len(out) >= limit:
            break
    return out


@mcp.tool()
def preview_get_state(module_id: str | None = None) -> dict:
    """
    HTML Preview session snapshot: manifest, cell registry (IDs + last-run
    status), recent_errors. module_id defaults to most-recently-updated.
    """
    try:
        if module_id is None:
            modules = _list_open_modules()
            if not modules:
                return {
                    "open_modules": [],
                    "_note": "No HTML preview is currently open. Have the user open one, or pass a module_id.",
                }
            # Pick the most recently updated snapshot
            candidates = []
            for mid in modules:
                snap = _read_session(mid)
                if snap:
                    candidates.append((snap.get("updatedAtMs") or 0, mid, snap))
            candidates.sort(reverse=True)
            if candidates:
                _, module_id, snap = candidates[0]
                return {
                    "module_id": module_id,
                    "manifest": snap.get("manifest"),
                    "cells": snap.get("cells", []),
                    "recent_errors": snap.get("recentErrors", []),
                    "updated_at_ms": snap.get("updatedAtMs"),
                    "open_modules": modules,
                }

        snap = _read_session(module_id) if module_id else None
        if snap is None:
            return {
                "error": True,
                "code": "NOT_FOUND",
                "message": f"No preview session found for module '{module_id}'.",
                "open_modules": _list_open_modules(),
            }
        return {
            "module_id": module_id,
            "manifest": snap.get("manifest"),
            "cells": snap.get("cells", []),
            "recent_errors": snap.get("recentErrors", []),
            "updated_at_ms": snap.get("updatedAtMs"),
        }
    except Exception as e:
        log.error("preview_get_state failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def preview_recent_events(
    module_id: str | None = None,
    since_seq: int = 0,
    kind_filter: str | None = None,
    limit: int = 50,
) -> dict:
    """
    Recent PreviewEvent records (cell runs/errors, user comments, iframe
    interactions). Poll with since_seq for new-only. kind_filter examples:
    cell.error | user.comment | cell.run.completed. limit cap 200.
    """
    try:
        if module_id is None:
            modules = _list_open_modules()
            if not modules:
                return {"events": [], "next_seq": 0, "open_modules": []}
            # Pick the most recently updated
            best_mid, best_ts = None, 0
            for mid in modules:
                snap = _read_session(mid)
                ts = (snap or {}).get("updatedAtMs") or 0
                if ts > best_ts:
                    best_mid, best_ts = mid, ts
            module_id = best_mid

        events = _read_events(
            module_id or "unknown",
            since_seq=since_seq,
            kind_filter=kind_filter,
            limit=max(1, min(limit, 200)),
        )
        next_seq = max((int(e.get("eventSeq", 0)) for e in events), default=since_seq)
        return {
            "module_id": module_id,
            "events": events,
            "next_seq": next_seq,
            "count": len(events),
        }
    except Exception as e:
        log.error("preview_recent_events failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def preview_list_modules() -> dict:
    """List the module IDs that currently have an open preview session."""
    try:
        modules = _list_open_modules()
        return {
            "modules": modules,
            "count": len(modules),
        }
    except Exception as e:
        log.error("preview_list_modules failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def preview_focus_cell(module_id: str, cell_id: str) -> dict:
    """
    Scroll the HTML Preview to a specific cell and highlight it.
    """
    try:
        ok = push_focus_cell(module_id, cell_id)
        return {
            "success": ok,
            "module_id": module_id,
            "cell_id": cell_id,
        }
    except Exception as e:
        log.error("preview_focus_cell failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def preview_revise_section(module_id: str, section_id: str, new_html: str) -> dict:
    """
    Propose a revised HTML section (well-formed snippet, no <html>/<body>).
    User sees a diff to accept/reject. Used in the edit-mode comment flow.
    """
    try:
        ok = push_revise_section(module_id, section_id, new_html)
        return {
            "success": ok,
            "module_id": module_id,
            "section_id": section_id,
            "_note": "Command queued for the user — they will see a diff to accept or reject.",
        }
    except Exception as e:
        log.error("preview_revise_section failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def preview_address_comment(
    module_id: str, comment_id: str, new_text: str | None = None
) -> dict:
    """
    Address a user comment. new_text (optional) proposes replacement text
    for the commented selection — user sees a diff. Omit to just resolve.
    """
    try:
        ok = push_address_comment(module_id, comment_id, new_text)
        return {
            "success": ok,
            "module_id": module_id,
            "comment_id": comment_id,
        }
    except Exception as e:
        log.error("preview_address_comment failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def preview_get_pending_changes(module_id: str, status: str = "open") -> dict:
    """
    Return the batched queue of pending user comments + text edits for a
    module (from ~/.aihydro/comments/<id>.json). Process them in one turn,
    then preview_address_comment each.
    status: open (default) | awaiting_review | addressed | all.
    """
    try:
        safe = _safe_module_id(module_id)
        fp = _COMMENTS_DIR / f"{safe}.json"
        if not fp.exists():
            return {"module_id": module_id, "comments": [], "count": 0}
        data = json.loads(fp.read_text(encoding="utf-8"))
        comments = data.get("comments", [])
        if status != "all":
            comments = [c for c in comments if c.get("status") == status]
        return {
            "module_id": module_id,
            "comments": comments,
            "count": len(comments),
            "_note": (
                "Process each comment: read body + anchor, address it (look up citations, "
                "propose edits, etc), then call preview_address_comment(module_id, comment_id, new_text) "
                "to round-trip the proposal to the user for diff-review."
            ),
        }
    except Exception as e:
        log.error("preview_get_pending_changes failed: %s", e)
        return _tool_error_to_dict(e)
