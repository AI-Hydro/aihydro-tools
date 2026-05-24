"""
Preview command writer — pushes orchestration commands to
~/.aihydro/preview_commands/.

The AI-Hydro VS Code extension `PreviewCommandWatcher` polls this directory
and applies focus_cell, revise_section, and address_comment commands to the
PreviewSessionService.

Mirrors aihydro-tools/ai_hydro/mcp/map_commands.py.
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_PREVIEW_COMMANDS_DIR = Path.home() / ".aihydro" / "preview_commands"


def write_preview_command(payload: dict[str, Any]) -> bool:
    """Write a one-shot command JSON file. Never raises."""
    try:
        _PREVIEW_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
        event_file = _PREVIEW_COMMANDS_DIR / f"{uuid.uuid4().hex}.json"
        event_file.write_text(json.dumps(payload), encoding="utf-8")
        log.debug("Preview command written: %s", payload.get("type"))
        return True
    except Exception as exc:
        log.warning("write_preview_command failed (non-fatal): %s", exc)
        return False


def push_focus_cell(module_id: str, cell_id: str) -> bool:
    return write_preview_command(
        {"type": "focus_cell", "module_id": module_id, "cell_id": cell_id}
    )


def push_revise_section(module_id: str, section_id: str, new_html: str) -> bool:
    return write_preview_command(
        {
            "type": "revise_section",
            "module_id": module_id,
            "section_id": section_id,
            "new_html": new_html,
        }
    )


def push_address_comment(module_id: str, comment_id: str, new_text: str | None = None) -> bool:
    payload: dict[str, Any] = {
        "type": "address_comment",
        "module_id": module_id,
        "comment_id": comment_id,
    }
    if new_text is not None:
        payload["new_text"] = new_text
    return write_preview_command(payload)
