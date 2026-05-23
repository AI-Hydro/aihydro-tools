"""
Artifact-display MCP tools.

These tools drop marker files into ~/.aihydro/preview-requests/, which the
AI-Hydro VS Code extension watches and acts on. The marker-file pattern keeps
the MCP server decoupled from the extension's webview transport — the agent
just writes a small JSON file and the extension picks it up within ~1s.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp")

_PREVIEW_REQUESTS_DIR = Path.home() / ".aihydro" / "preview-requests"


@mcp.tool()
def show_html_preview(file_path: str, title: str | None = None) -> dict:
    """
    Open an HTML file in the AI-Hydro HTML Preview panel.

    Use this after writing an interactive learning module (see the
    `interactive-module-builder` skill) or any standalone HTML artifact you
    want the researcher to view inside the extension. The bundled Python
    kernel will execute `.aihydro-cell` Python cells in the file — no
    external browser needed.

    Parameters
    ----------
    file_path : str
        Absolute path to the .html file on disk.
    title : str, optional
        Tab title for the preview. Defaults to the file basename.

    Returns
    -------
    dict with `success` and the request marker path.
    """
    try:
        abs_path = str(Path(file_path).expanduser().resolve())
        if not os.path.isfile(abs_path):
            return {
                "error": True,
                "code": "NOT_FOUND",
                "message": f"File not found: {abs_path}",
            }
        if not abs_path.lower().endswith((".html", ".htm")):
            return {
                "error": True,
                "code": "INVALID_EXT",
                "message": f"Not an HTML file: {abs_path}",
            }

        _PREVIEW_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
        marker_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
        marker_path = _PREVIEW_REQUESTS_DIR / marker_id
        marker_path.write_text(
            json.dumps({"file_path": abs_path, "title": title}),
            encoding="utf-8",
        )
        log.info("show_html_preview marker written: %s -> %s", marker_path, abs_path)
        return {
            "success": True,
            "file_path": abs_path,
            "marker": str(marker_path),
            "_note": (
                "The AI-Hydro extension is watching ~/.aihydro/preview-requests/ "
                "and will open the file in the HTML Preview panel within ~1s. "
                "If the extension is not running, the marker persists until it starts."
            ),
        }
    except Exception as e:
        log.error("show_html_preview failed: %s", e)
        return _tool_error_to_dict(e)
