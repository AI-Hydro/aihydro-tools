"""
Layering contract — aihydro-tools sits at the TOP of the dependency graph.

Unlike every package below it, aihydro-tools is *allowed* to import all of
aihydro-core, aihydro-data, aihydro-watershed, aihydro-lsh, pygeoglim,
camels-attrs, and (via dependency injection, never a direct import) the
private aihydro-modelling package — that's its job as the MCP surface /
meta-package. So there's no "forbidden downward set" to check here the way
every other package's test_layering.py checks "don't import ai_hydro".

Two invariants ARE meaningful from this side of the graph:

1. Never import from `_archive/` — those two subprojects (AIHydro_SDK,
   AIHydro_paper) are explicitly stale (see PROJECT.md non-goals); nothing
   in the live package should reach into them.
2. Never reach past a sibling package's public API into its private
   (underscore-prefixed) submodules — that couples aihydro-tools to
   implementation details that can change without notice, defeating the
   point of the layered architecture. The one documented exception is
   `ai_hydro/analysis/*.py`, which contains deliberate backward-compat shims
   that re-export from aihydro_watershed's private modules (see
   MCP/docs/ARCHITECTURE.md "Layering discipline" — an intentional upward
   re-export, not a bug) and is excluded from this check.

Runs offline with zero extra dependencies (uses ``ast``).
"""
from __future__ import annotations

import ast
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent / "ai_hydro"
_SHIM_EXCEPTION_DIR = _PKG_ROOT / "analysis"
_ARCHIVE_MARKERS = {"AIHydro_SDK", "AIHydro_paper", "_archive"}
_SIBLING_PACKAGES = {
    "aihydro_data", "aihydro_watershed", "aihydro_lsh",
    "aihydro_core", "pygeoglim", "aihydro_modelling", "camels_attrs",
}


def _python_files(exclude: Path | None = None) -> list[Path]:
    return [
        p for p in sorted(_PKG_ROOT.rglob("*.py"))
        if exclude is None or exclude not in p.parents
    ]


def _imported_modules(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_no_archive_imports():
    """Nothing in ai_hydro/ may import from the stale _archive/ subprojects."""
    offenders: dict[str, list[str]] = {}
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        bad = [m for m in _imported_modules(tree) if any(marker in m for marker in _ARCHIVE_MARKERS)]
        if bad:
            offenders[str(path.relative_to(_PKG_ROOT))] = bad

    assert not offenders, (
        "ai_hydro must never import from _archive/ (stale, non-goal per PROJECT.md):\n"
        + "\n".join(f"  {f}: {mods}" for f, mods in offenders.items())
    )


def test_no_private_submodule_reach_through():
    """
    Sibling packages must be imported via their public API, not by reaching
    into an underscore-prefixed private submodule. ai_hydro/analysis/*.py is
    the one documented, deliberate exception (backward-compat shims).
    """
    offenders: dict[str, list[str]] = {}
    for path in _python_files(exclude=_SHIM_EXCEPTION_DIR):
        tree = ast.parse(path.read_text(), filename=str(path))
        bad = []
        for m in _imported_modules(tree):
            parts = m.split(".")
            if parts[0] not in _SIBLING_PACKAGES:
                continue
            if any(part.startswith("_") for part in parts[1:]):
                bad.append(m)
        if bad:
            offenders[str(path.relative_to(_PKG_ROOT))] = bad

    assert not offenders, (
        "ai_hydro must import siblings via their public API only, not private "
        "submodules (excluding the documented ai_hydro/analysis/ shim exception):\n"
        + "\n".join(f"  {f}: {mods}" for f, mods in offenders.items())
    )
