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
from ai_hydro.mcp import tools_artifacts    # noqa: F401  — show_html_preview (open HTML in preview panel)
from ai_hydro.mcp import tools_knowledge    # noqa: F401  — v1.6.4: Knowledge Registry
from ai_hydro.mcp import tools_validators   # noqa: F401  — v1.6.6: Physics Validators
from ai_hydro.mcp import tools_ledger       # noqa: F401  — v1.6.7: Claims & Assumptions
from ai_hydro.mcp import tools_workflows    # noqa: F401  — v1.6.8: Workflow Manifests
from ai_hydro.mcp import tools_gee          # noqa: F401  — GEE map/chat tools
from ai_hydro.mcp import tools_map          # noqa: F401  — map orchestration (symbology, ROI, catalog)
from ai_hydro.mcp import tools_preview      # noqa: F401  — HTML preview observability (cells, events, comments)
from ai_hydro.mcp import tools_citations    # noqa: F401  — v1.7.0: Citation lookup (CrossRef/SemanticScholar/DataCite)
from ai_hydro.mcp import tools_course       # noqa: F401  — v1.8.0: course mode (state, navigate, set_progress, scaffold, curriculum)
from ai_hydro.mcp import tools_discovery    # noqa: F401  — v1.8.0: aihydro_describe_capability (token-efficient tool discovery)
from ai_hydro.mcp import tools_indices      # noqa: F401  — v0.2.0: spectral index tools (compute_spectral_index, list_spectral_indices)

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
from ai_hydro.mcp.registry import (  # noqa: E402
    discover_tools as _discover_tools,
    invoke_plugin_registrars as _invoke_plugin_registrars,
)

# Pattern 1: single-tool entry points (one function = one tool)
for _name, _fn in _discover_tools():
    mcp.tool(name=_name)(_fn)

# Pattern 2: multi-tool registrars (one function = many tools)
# aihydro-data uses this to register its 9 data_* tools in one call.
_invoke_plugin_registrars(mcp)


# ── Wave 1.5: tag registered tools with tier/domain metadata ──────────────────
# After all tools are registered, attach `meta={"tier": N, "domain": "X"}` to
# each tool's MCP-wire payload. MCP-compatible clients (Cline patched, others
# falling back gracefully) can use this to filter the tool list shown to the
# model, keeping system-prompt context tight.
#
# tier:   from app.TOOL_TIERS (1 = scientific output, 2 = workflow, 3 = infra)
# domain: from tools_discovery._DOMAIN_PREFIXES (longest-prefix match)
#
# Tools NOT in TOOL_TIERS default to tier 2. Tools NOT matching any domain
# prefix get domain "general".

def _tag_tools_with_tier_meta() -> None:
    """Patch tier/domain into the meta dict of every registered tool."""
    from ai_hydro.mcp.app import TOOL_TIERS, is_hot_tool
    from ai_hydro.mcp.tools_discovery import _DOMAIN_PREFIXES

    # Reverse-lookup: longest matching prefix wins (more specific first)
    prefix_to_domain: list[tuple[str, str]] = []
    for domain, prefixes in _DOMAIN_PREFIXES.items():
        for p in prefixes:
            prefix_to_domain.append((p, domain))
    prefix_to_domain.sort(key=lambda x: -len(x[0]))

    def _domain_for(name: str) -> str:
        for prefix, domain in prefix_to_domain:
            if name.startswith(prefix):
                return domain
        return "general"

    # FastMCP stores tools in mcp._local_provider._components keyed by tool.key
    components = getattr(mcp._local_provider, "_components", {})
    tagged = 0
    for key, comp in components.items():
        if not hasattr(comp, "name") or not hasattr(comp, "meta"):
            continue
        name = comp.name
        tier = TOOL_TIERS.get(name, 2)  # default tier 2 if unregistered
        domain = _domain_for(name)
        # Merge into existing meta dict rather than replacing
        existing = dict(comp.meta) if comp.meta else {}
        existing.setdefault("tier", tier)
        existing.setdefault("domain", domain)
        # `hot` drives the extension's progressive-disclosure renderer: hot
        # tools get their full inputSchema inline, others get a summary line.
        existing.setdefault("hot", is_hot_tool(name))
        comp.meta = existing
        tagged += 1
    import logging
    logging.getLogger("ai_hydro.mcp").info(
        "Wave 1.5: tagged %d tools with tier+domain metadata", tagged
    )


try:
    _tag_tools_with_tier_meta()
except Exception as _e:
    import logging
    logging.getLogger("ai_hydro.mcp").warning(
        "Wave 1.5 tier tagging skipped: %s", _e
    )


# ── WS-4: argument-repair + self-correcting-error middleware ──────────────────
# Sits in front of every tool call: repairs aliased/mistyped arguments before
# execution, and on unrecoverable failure returns a structured self-help payload
# (schema + corrected example) instead of a bare error, so the weak driving
# model can self-correct on its next turn.
try:
    from ai_hydro.mcp.arg_repair import install_arg_repair as _install_arg_repair
    _install_arg_repair(mcp)
except Exception as _e:
    import logging
    logging.getLogger("ai_hydro.mcp").warning(
        "WS-4 arg-repair middleware not installed: %s", _e
    )


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
