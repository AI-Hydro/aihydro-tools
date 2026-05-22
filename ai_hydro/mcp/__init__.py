"""
AI-Hydro MCP Server Infrastructure.

Importing this package triggers tool registration via ``@mcp.tool()``
decorators in the tool modules.
"""
from ai_hydro.mcp.app import mcp  # noqa: F401 — the FastMCP singleton

# Import tool modules so their @mcp.tool() decorators execute and
# register all built-in tools on the shared ``mcp`` instance.
from ai_hydro.mcp import tools_analysis   # noqa: F401
from ai_hydro.mcp import tools_session    # noqa: F401
from ai_hydro.mcp import tools_modelling  # noqa: F401
from ai_hydro.mcp import tools_project    # noqa: F401  — v1.2: project, literature, persona
from ai_hydro.mcp import tools_execution  # noqa: F401  — v1.6.0: run_python, list_relevant_clis
from ai_hydro.mcp import resources          # noqa: F401  — v1.6.0: M8 knowledge resource layer
from ai_hydro.mcp import tools_skills       # noqa: F401  — v1.6.0: list_skills, load_skill
from ai_hydro.mcp import tools_knowledge    # noqa: F401  — v1.6.4: Knowledge Registry
from ai_hydro.mcp import tools_validators   # noqa: F401  — v1.6.6: Physics Validators
from ai_hydro.mcp import tools_ledger       # noqa: F401  — v1.6.7: Claims & Assumptions
from ai_hydro.mcp import tools_workflows    # noqa: F401  — v1.6.8: Workflow Manifests
from ai_hydro.mcp import tools_gee          # noqa: F401  — GEE map/chat tools
from ai_hydro.mcp import tools_map          # noqa: F401  — map orchestration (symbology, ROI, catalog)

# ── Tier 1 post-run validator registrations ───────────────────────────────
# Registered after all tool modules are imported so validator callables exist.
# Each registration maps a Tier 1 tool name → validator fn + kwargs builder.
from ai_hydro.mcp.enforcement import register_post_validator as _rpv  # noqa: E402
from ai_hydro.mcp.tools_validators import (  # noqa: E402
    check_water_balance_consistency,
    check_unit_consistency,
)

# extract_hydrological_signatures → water balance check
# Fires after signatures are written to session; reads runoff_ratio from session.
_rpv(
    "extract_hydrological_signatures",
    check_water_balance_consistency,
    lambda sid: {"session_id": sid},
)

# fetch_streamflow_data → unit consistency check (expects m3/s)
# Fires after streamflow is written to session; checks data.units field.
_rpv(
    "fetch_streamflow_data",
    check_unit_consistency,
    lambda sid: {"session_id": sid, "slot": "streamflow", "expected_units": "m3/s"},
)

# Discover and register community plugin tools via entry points.
# Third-party packages register tools in their pyproject.toml:
#   [project.entry-points."aihydro.tools"]
#   my_tool = "my_package.module:my_tool_function"
from ai_hydro.mcp.registry import discover_tools as _discover_tools

for _name, _fn in _discover_tools():
    mcp.tool(name=_name)(_fn)


def main() -> None:
    """Entry point for the ``aihydro-mcp`` console script."""
    import sys

    # Handle CLI flags before heavy imports
    if len(sys.argv) > 1:
        from ai_hydro.mcp.__main__ import _version, _diagnose
        arg = sys.argv[1]
        if arg in ("--version", "-V"):
            print(f"aihydro-tools {_version()}")
            return
        elif arg in ("--diagnose", "--check"):
            _diagnose()
            return

    import logging
    import os
    from pathlib import Path

    # Redirect cache/temp writes away from read-only filesystems (e.g. Box Drive)
    cache_dir = Path.home() / ".aihydro" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(cache_dir)
    os.environ.setdefault("TMPDIR", str(cache_dir))
    os.environ.setdefault("TEMP", str(cache_dir))
    os.environ.setdefault("TMP", str(cache_dir))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("ai_hydro.mcp")
    log.info("Starting AI-Hydro MCP server...")

    from ai_hydro.mcp.tools_docs import _write_tools_md

    try:
        _write_tools_md()
    except Exception as _e:
        log.debug("tools.md generation skipped: %s", _e)

    mcp.run()
