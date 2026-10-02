"""Helper: identify optional community-plugin tools on the shared FastMCP.

Built-in tools (and the aihydro-data tools, which are pinned in CI) must be
listed in ``TOOL_TIERS``.  Tools that an *optional* ``aihydro.tools`` plugin
(e.g. ``aihydro-lsh``, deliberately not installed in public CI, see commit
9a81960) attaches at import time are not part of the built-in contract: they
default to tier 2 (see ``ai_hydro/mcp/__init__.py``) and their presence depends
on the environment.  This returns exactly those names, so the registry tests
stay strict for everything else.
"""
from __future__ import annotations


def optional_plugin_tool_names(tools, tier_registry) -> set[str]:
    from importlib.metadata import entry_points

    plugin_roots = {
        ep.value.split(":")[0].split(".")[0]
        for ep in entry_points(group="aihydro.tools")
    }
    extras: set[str] = set()
    for t in tools:
        if t.name in tier_registry:
            continue
        module = getattr(getattr(t, "fn", None), "__module__", "") or ""
        if module.split(".")[0] in plugin_roots:
            extras.add(t.name)
    return extras
