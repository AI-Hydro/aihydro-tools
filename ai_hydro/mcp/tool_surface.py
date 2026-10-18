"""
Tool surface: MCP annotations, deterministic ``tools/list`` ordering, optional hot-only listing.

Three small mechanisms, installed once after every tool is registered
(``ai_hydro/mcp/__init__.py``):

1. ``apply_tool_annotations`` stamps ``readOnlyHint`` / ``destructiveHint`` /
   ``idempotentHint`` / ``openWorldHint`` from ``tool_annotations.toml`` (one row per tool, with
   the code evidence) onto each registered tool, so hosts can auto-approve read-only tools and
   gate destructive or open-world ones.
2. ``ToolSurfaceMiddleware.on_list_tools`` sorts ``tools/list`` by name, so the order no longer
   depends on import or entry-point iteration order.
3. The same middleware implements server-side progressive disclosure:
   ``AIHYDRO_TOOL_SURFACE=full`` (default, every tool) or ``hot`` (hot tools plus the discovery
   helpers only). A tool left out of the hot listing is still registered and callable by name;
   ``aihydro_describe_capability`` / ``list_available_tools`` / ``describe_tool`` find it.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from fastmcp.server.middleware import Middleware

log = logging.getLogger("ai_hydro.mcp")

__all__ = [
    "ANNOTATIONS_PATH", "SURFACE_ENV", "load_annotation_rows", "apply_tool_annotations",
    "ToolSurfaceMiddleware", "install_tool_surface", "tool_surface_mode",
]

ANNOTATIONS_PATH = Path(__file__).with_name("tool_annotations.toml")
SURFACE_ENV = "AIHYDRO_TOOL_SURFACE"
_HINT_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

HOT_MODE_NOTE = (
    "TOOL SURFACE: this server lists only the common tools (tools/list). Many more exist: "
    "call aihydro_describe_capability(domain) or list_available_tools() to browse them, and "
    "describe_tool(name) for parameters; any registered tool can be called by name."
)


def tool_surface_mode() -> str:
    """``"full"`` (default) or ``"hot"``, from ``AIHYDRO_TOOL_SURFACE``; unknown values mean full."""
    value = os.environ.get(SURFACE_ENV, "").strip().lower()
    return "hot" if value == "hot" else "full"


# ---------------------------------------------------------------------------
# annotation table
# ---------------------------------------------------------------------------

def _parse_minimal_toml(text: str) -> dict[str, dict[str, Any]]:
    """Parse the restricted table format of tool_annotations.toml (Python 3.10 has no tomllib)."""
    rows: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r'\["(.+)"\]', line)
        if m:
            current = rows.setdefault(m.group(1), {})
            continue
        m = re.fullmatch(r'(\w+)\s*=\s*(.+)', line)
        if not m or current is None:
            raise ValueError(f"tool_annotations.toml: cannot parse line {raw!r}")
        key, val = m.group(1), m.group(2).strip()
        if val in ("true", "false"):
            current[key] = val == "true"
        elif val.startswith('"') and val.endswith('"'):
            current[key] = val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        else:
            raise ValueError(f"tool_annotations.toml: unsupported value in {raw!r}")
    return rows


def load_annotation_rows(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Return ``{tool_name: row}``; each row has the four hints, ``evidence`` and ``source``."""
    text = (path or ANNOTATIONS_PATH).read_text(encoding="utf-8")
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - 3.10
        return _parse_minimal_toml(text)
    return tomllib.loads(text)


def _components(mcp: Any) -> dict[Any, Any]:
    local_provider = getattr(mcp, "_local_provider", None)
    components = getattr(local_provider, "_components", {})
    if not components:
        components = getattr(getattr(mcp, "_tool_manager", None), "_tools", {})
    return components


def apply_tool_annotations(mcp: Any, rows: dict[str, dict[str, Any]] | None = None) -> int:
    """Stamp ToolAnnotations on every registered tool that has a row. Returns the count stamped."""
    from mcp.types import ToolAnnotations

    rows = load_annotation_rows() if rows is None else rows
    stamped = 0
    for comp in _components(mcp).values():
        name = getattr(comp, "name", None)
        if not name or not hasattr(comp, "annotations"):
            continue
        row = rows.get(name)
        if row is None:
            log.warning("tool %s has no row in tool_annotations.toml", name)
            continue
        existing = comp.annotations.model_dump(exclude_none=True) if comp.annotations else {}
        existing.update({k: bool(row[k]) for k in _HINT_KEYS})
        comp.annotations = ToolAnnotations(**existing)
        stamped += 1
    return stamped


# ---------------------------------------------------------------------------
# tools/list middleware
# ---------------------------------------------------------------------------

def _is_hot(tool: Any) -> bool:
    meta = getattr(tool, "meta", None) or {}
    if "hot" in meta:
        return bool(meta["hot"])
    from ai_hydro.mcp.app import is_hot_tool
    return is_hot_tool(getattr(tool, "name", ""))


class ToolSurfaceMiddleware(Middleware):
    """Sort ``tools/list`` by name; under ``AIHYDRO_TOOL_SURFACE=hot`` list hot tools only."""

    async def on_list_tools(self, context, call_next):
        tools = list(await call_next(context))
        if tool_surface_mode() == "hot":
            tools = [t for t in tools if _is_hot(t)]
        return sorted(tools, key=lambda t: t.name)


def install_tool_surface(mcp: Any) -> None:
    """Apply annotations and attach the list middleware (idempotent)."""
    if getattr(mcp, "_aihydro_tool_surface_installed", False):
        return
    n = apply_tool_annotations(mcp)
    mcp.add_middleware(ToolSurfaceMiddleware())
    if tool_surface_mode() == "hot":
        current = getattr(mcp, "instructions", None) or ""
        if HOT_MODE_NOTE not in current:
            mcp.instructions = (current + "\n\n" + HOT_MODE_NOTE).strip()
    mcp._aihydro_tool_surface_installed = True
    log.info("tool surface: annotated %d tools, mode=%s", n, tool_surface_mode())
