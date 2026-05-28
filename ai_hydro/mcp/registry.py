"""
MCP Tool Entry Point Discovery
================================

Auto-discover tools and knowledge extensions registered via Python entry points.

Community plugins register tools in their own ``pyproject.toml``::

    [project.entry-points."aihydro.tools"]
    my_tool = "my_package.module:my_tool_function"

Knowledge plugins can extend library references::

    [project.entry-points."aihydro.knowledge"]
    my_lib = "my_package.knowledge:get_refs_dir"

Then ``discover_tools()`` and ``discover_knowledge()`` find them at runtime so
the MCP server can register them without any code change in ai-hydro core.
"""

from __future__ import annotations

import logging
import inspect
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("ai_hydro.mcp")


def discover_tools() -> list[tuple[str, Callable[..., Any]]]:
    """
    Auto-discover *individual* tool functions registered via the
    ``aihydro.tools`` entry point group.

    Plugins that need to register a *single* tool function declare it directly::

        [project.entry-points."aihydro.tools"]
        my_tool = "my_package.module:my_tool_function"

    Plugins that need to register *multiple* tools (e.g. aihydro-data, which
    exposes 9 ``data_*`` tools) instead declare a *registrar* function whose
    name is ``register_tools`` and whose first parameter is named ``mcp``.
    Those are NOT returned here — they are discovered + invoked separately by
    :func:`invoke_plugin_registrars` so they can attach all their tools to the
    shared FastMCP singleton in one shot.

    Returns
    -------
    list of (name, callable) tuples
        Each entry is a (tool_name, tool_function) pair that the MCP server
        can register as an additional tool.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:
        from importlib_metadata import entry_points  # type: ignore[no-redef]

    discovered: list[tuple[str, Callable[..., Any]]] = []
    eps = entry_points(group="aihydro.tools")
    for ep in eps:
        try:
            tool_fn = ep.load()
            if _is_registrar(tool_fn):
                # Skip — registrars are handled by invoke_plugin_registrars()
                continue
            sig = inspect.signature(tool_fn)
            if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()):
                log.warning(
                    "Skipping plugin tool %s (%s): FastMCP tools cannot use *args.",
                    ep.name,
                    ep.value,
                )
                continue
            discovered.append((ep.name, tool_fn))
            log.info("Discovered plugin tool: %s (%s)", ep.name, ep.value)
        except Exception as exc:
            log.warning("Failed to load plugin tool %s: %s", ep.name, exc)

    return discovered


def _is_registrar(fn: Callable[..., Any]) -> bool:
    """Return True if ``fn`` looks like a multi-tool registrar callback.

    A registrar is a function whose first parameter is named ``mcp`` (the
    FastMCP server instance to attach tools to). aihydro-data uses this
    pattern to register 9 ``data_*`` tools in one call.
    """
    if getattr(fn, "__name__", "") == "register_tools":
        return True
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (ValueError, TypeError):
        return False
    return bool(params) and params[0].name == "mcp"


def invoke_plugin_registrars(mcp: Any) -> int:
    """Find every ``aihydro.tools`` entry point that is a registrar (e.g.
    ``register_tools(mcp)``) and call it with the shared FastMCP singleton.

    This is the multi-tool counterpart to :func:`discover_tools`. Registrars
    can attach an arbitrary number of tools to ``mcp`` in one call. Returns
    the number of registrars successfully invoked.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:
        from importlib_metadata import entry_points  # type: ignore[no-redef]

    invoked = 0
    eps = entry_points(group="aihydro.tools")
    for ep in eps:
        try:
            fn = ep.load()
        except Exception as exc:
            log.warning("Failed to load entry point %s: %s", ep.name, exc)
            continue
        if not _is_registrar(fn):
            continue
        try:
            fn(mcp)
            log.info("Invoked plugin registrar: %s (%s)", ep.name, ep.value)
            invoked += 1
        except Exception as exc:
            log.warning("Plugin registrar %s failed: %s", ep.name, exc)
    return invoked


def discover_knowledge() -> list[Path]:
    """
    Auto-discover knowledge reference directories via the ``aihydro.knowledge``
    entry point group.

    Plugins export a callable that returns a ``pathlib.Path`` to a directory
    of ``*.json`` library reference files::

        # my_package/knowledge.py
        from pathlib import Path
        def get_refs_dir() -> Path:
            return Path(__file__).parent / "library_refs"

        # pyproject.toml
        [project.entry-points."aihydro.knowledge"]
        my_lib = "my_package.knowledge:get_refs_dir"

    Returns
    -------
    list of Path objects pointing to knowledge reference directories.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:
        from importlib_metadata import entry_points  # type: ignore[no-redef]

    dirs: list[Path] = []
    eps = entry_points(group="aihydro.knowledge")
    for ep in eps:
        try:
            get_dir = ep.load()
            ref_dir = get_dir()
            if isinstance(ref_dir, Path) and ref_dir.is_dir():
                dirs.append(ref_dir)
                log.info("Discovered knowledge plugin: %s → %s", ep.name, ref_dir)
            else:
                log.warning("Knowledge plugin %s did not return a valid Path", ep.name)
        except Exception as exc:
            log.warning("Failed to load knowledge plugin %s: %s", ep.name, exc)

    return dirs
