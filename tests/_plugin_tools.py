"""Helper: identify optional community-plugin tools on the shared FastMCP.

Built-in tools and tools from distributions that aihydro-tools itself declares
as hard dependencies (e.g. ``aihydro-data``) must be listed in ``TOOL_TIERS``.
Only tools contributed by an ``aihydro.tools`` entry point whose *distribution*
is NOT a declared unconditional dependency of aihydro-tools (e.g. ``aihydro-lsh``,
deliberately absent from public CI, see commit 9a81960) are exempt: they default
to tier 2 and their presence depends on the environment.

Keyed on distribution name, not module root, so a declared dependency that also
ships an entry point can never be exempted.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_dependency_names() -> set[str]:
    """Normalized names of aihydro-tools' unconditional [project.dependencies]."""
    from packaging.requirements import Requirement

    with open(_PYPROJECT, "rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    return {_norm(Requirement(d).name) for d in deps}


def optional_plugin_tool_names(tools, tier_registry) -> set[str]:
    from importlib.metadata import entry_points

    declared = declared_dependency_names()
    optional_roots: set[str] = set()
    for ep in entry_points(group="aihydro.tools"):
        dist = getattr(ep, "dist", None)
        if dist is None or _norm(dist.metadata["Name"]) in declared:
            continue
        optional_roots.add(ep.value.split(":")[0].split(".")[0])

    extras: set[str] = set()
    for t in tools:
        if t.name in tier_registry:
            continue
        module = getattr(getattr(t, "fn", None), "__module__", "") or ""
        if module.split(".")[0] in optional_roots:
            extras.add(t.name)
    return extras
