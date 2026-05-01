"""
MCP Resource layer for AI-Hydro knowledge (M8).

Serves static knowledge content via the native MCP resource protocol.
URI scheme: aihydro://knowledge/library/{name}
            aihydro://knowledge/list

The facade tool get_library_reference (in tools_analysis.py) delegates here
for backwards compatibility with clients that do not support list_resources.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ai_hydro.mcp.app import mcp

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
