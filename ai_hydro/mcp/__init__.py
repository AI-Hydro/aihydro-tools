"""
AI-Hydro MCP Server Infrastructure.

Importing this package triggers tool registration via ``@mcp.tool()``
decorators in the tool modules.
"""
from ai_hydro.mcp.app import mcp  # noqa: F401 — the FastMCP singleton

# Import tool modules so their @mcp.tool() decorators execute and
# register all built-in tools on the shared ``mcp`` instance.
from ai_hydro.mcp import tools_analysis   # noqa: F401
from ai_hydro.mcp import tools_inundation_physics  # noqa: F401 — Phase 3 validate-tier
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
from ai_hydro.mcp import tools_data_async  # noqa: F401  — data_fetch_background + get_data_fetch_result (async GloFAS / slow sources)
from ai_hydro.mcp import tools_audit       # noqa: F401  — v1.8.0: audit_interpretation (Answer Auditor)
from ai_hydro.mcp import tools_experiments # noqa: F401  — v2.1.0: define_experiment, run_experiment, get_experiment_table
from ai_hydro.mcp import tools_skeptic    # noqa: F401  — v2.3.0: run_skeptic (adversarial second-pass referee)

# ── Tier 1 post-run validator registrations ───────────────────────────────
# Registered after all tool modules are imported so validator callables exist.
# Each registration maps a Tier 1 tool name → validator fn + kwargs builder.
from ai_hydro.mcp.enforcement import register_post_validator as _rpv  # noqa: E402
from ai_hydro.mcp.enforcement import register_next_steps as _rns      # noqa: E402
from ai_hydro.mcp.tools_validators import (  # noqa: E402
    check_water_balance_consistency,
    check_unit_consistency,
    check_uncertainty_present,
    check_record_length,
    check_usgs_qualification_codes,
    check_regulated_basin,
    check_stationarity,
)

# extract_hydrological_signatures → water balance check
# Fires after signatures are written to session; reads runoff_ratio from session.
_rpv(
    "extract_hydrological_signatures",
    check_water_balance_consistency,
    lambda sid: {"session_id": sid},
)

# extract_hydrological_signatures → uncertainty presence check
# Fires after signatures; warns if bootstrap CIs were not computed.
_rpv(
    "extract_hydrological_signatures",
    check_uncertainty_present,
    lambda sid: {"session_id": sid, "slot": "signatures"},
)

# fetch_streamflow_data → unit consistency check (expects m3/s)
# Fires after streamflow is written to session; checks data.units field.
_rpv(
    "fetch_streamflow_data",
    check_unit_consistency,
    lambda sid: {"session_id": sid, "slot": "streamflow", "expected_units": "m3/s"},
)

# fetch_streamflow_data → record length guard
# Warns when < 10 years; fails when < 20 years relative to flood-frequency needs.
_rpv(
    "fetch_streamflow_data",
    check_record_length,
    lambda sid: {"session_id": sid},
)

# fetch_streamflow_data → USGS qualification-code propagation
# Flags provisional ('P') or estimated ('e') records before they enter claims.
_rpv(
    "fetch_streamflow_data",
    check_usgs_qualification_codes,
    lambda sid: {"session_id": sid},
)

# delineate_watershed / delineate_watershed_from_point → regulated-basin check
# Queries the NID GeoPackage if present; otherwise flags regulation status as unknown.
_rpv(
    "delineate_watershed",
    check_regulated_basin,
    lambda sid: {"session_id": sid},
)
_rpv(
    "delineate_watershed_from_point",
    check_regulated_basin,
    lambda sid: {"session_id": sid},
)

# extract_hydrological_signatures → stationarity advisory
# Mann-Kendall trend test on annual streamflow totals; warns if p < 0.05.
_rpv(
    "extract_hydrological_signatures",
    check_stationarity,
    lambda sid: {"session_id": sid},
)

# ── Tier 1 next-steps registrations ──────────────────────────────────────────
# Injected automatically by post_run() into every successful Tier 1 result.
# A tool's own explicit next_steps key (if any) takes priority over these.

# delineate_watershed / delineate_watershed_from_point
_NS_DELINEATION = [
    {"tool": "extract_hydrological_signatures",
     "reason": "Compute runoff ratio, BFI, and flow-duration statistics for the delineated basin."},
    {"tool": "extract_geomorphic_parameters",
     "reason": "Derive slope, elongation ratio, drainage density, and other morphometric indices."},
    {"tool": "compute_twi",
     "reason": "Map topographic wetness index — needed for saturation-excess runoff modelling."},
    {"tool": "create_cn_grid",
     "reason": "Build a curve-number grid for design-storm / event-based analysis.",
     "when": "if event-based or design-storm analysis is planned"},
]
_rns("delineate_watershed",            _NS_DELINEATION)
_rns("delineate_watershed_from_point", _NS_DELINEATION)

# extract_hydrological_signatures
_rns("extract_hydrological_signatures", [
    {"tool": "separate_baseflow",
     "reason": "Partition total streamflow into quick-flow and baseflow components."},
    {"tool": "train_hydro_model",
     "reason": "Calibrate HBV-light or LSTM using the computed signatures as objective targets."},
    {"tool": "add_claim",
     "reason": "Record a scientific finding (e.g. BFI, runoff ratio) to the evidence ledger."},
    {"tool": "check_water_balance_consistency",
     "reason": "Re-run the water-balance validator on demand if you changed forcing data.",
     "when": "if forcing data was updated after signatures were computed"},
])

# extract_geomorphic_parameters
_rns("extract_geomorphic_parameters", [
    {"tool": "compute_twi",
     "reason": "TWI complements morphometric indices — together they describe hydrological landscape position."},
    {"tool": "add_claim",
     "reason": "Record a geomorphic finding (e.g. elongation ratio, drainage density) to the ledger."},
])

# compute_twi
_rns("compute_twi", [
    {"tool": "create_cn_grid",
     "reason": "CN grid uses the same DEM; computing them in sequence avoids double data pulls."},
    {"tool": "add_claim",
     "reason": "Record the TWI computation (DEM source, resolution) as a reproducible claim."},
])

# create_cn_grid
_rns("create_cn_grid", [
    {"tool": "train_hydro_model",
     "reason": "The CN grid can be used as a prior for initial-loss parameterisation in SCS models."},
    {"tool": "add_claim",
     "reason": "Record land-cover and soil source used for the CN calculation."},
])

# separate_baseflow
_rns("separate_baseflow", [
    {"tool": "extract_hydrological_signatures",
     "reason": "Re-compute signatures after baseflow separation to get BFI and recession constants.",
     "when": "if signatures have not been computed yet"},
    {"tool": "train_hydro_model",
     "reason": "Calibrated baseflow index is a strong constraint for groundwater-exchange parameters."},
    {"tool": "add_claim",
     "reason": "Document the separation method and BFI as a citable finding."},
])

# train_hydro_model
_rns("train_hydro_model", [
    {"tool": "get_model_results",
     "reason": "Fetch performance metrics (NSE, KGE, RMSE) and simulated discharge once training completes."},
    {"tool": "get_training_status",
     "reason": "Poll job progress if training was dispatched asynchronously.",
     "when": "if the job returned a job_id instead of results"},
])

# get_model_results
_rns("get_model_results", [
    {"tool": "add_claim",
     "reason": "Record the model performance (NSE / KGE) as a reviewable claim."},
    {"tool": "check_water_balance_consistency",
     "reason": "Validate simulated vs observed water balance after calibration."},
    {"tool": "write_research_interpretation",
     "reason": "Draft an interpretation paragraph for the results section of the manuscript."},
])

# add_claim / add_assumption
_NS_CLAIM = [
    {"tool": "promote_claim_to_registry",
     "reason": "Elevate this claim to the project-level registry once peer-reviewed.",
     "when": "after the claim has been reviewed or replicated"},
    {"tool": "list_claims",
     "reason": "Review all current claims for the session to check for contradictions."},
]
_rns("add_claim",      _NS_CLAIM)
_rns("add_assumption", _NS_CLAIM)

# promote_claim_to_registry
_rns("promote_claim_to_registry", [
    {"tool": "draft_claim_from_run",
     "reason": "Auto-generate a structured claim from a run_id if you need another claim from the same run."},
    {"tool": "export_session",
     "reason": "Export the session (with promoted claims) to a portable JSON for sharing or archiving."},
])

# draft_claim_from_run
_rns("draft_claim_from_run", [
    {"tool": "add_claim",
     "reason": "Persist the drafted claim to the session ledger."},
])

# Tier 1 validators (check_* tools) — suggest review then claim
_NS_VALIDATOR = [
    {"tool": "add_claim",
     "reason": "Record the validation finding (pass or fail) to the evidence ledger."},
    {"tool": "write_research_interpretation",
     "reason": "Draft a methods note explaining how validation was performed."},
]
_rns("check_water_balance_consistency",   _NS_VALIDATOR)
_rns("check_temporal_alignment",         _NS_VALIDATOR)
_rns("check_unit_consistency",           _NS_VALIDATOR)
_rns("check_record_length",              _NS_VALIDATOR)
_rns("check_usgs_qualification_codes",   _NS_VALIDATOR)
_rns("check_regulated_basin",            _NS_VALIDATOR)
_rns("check_stationarity",               _NS_VALIDATOR)

# define_experiment / run_experiment / get_experiment_table
_rns("define_experiment", [
    {"tool": "run_experiment",
     "reason": "Execute the experiment across all features now that the design is locked."},
])
_rns("run_experiment", [
    {"tool": "get_experiment_table",
     "reason": "Fetch the tabular results (rows=features, cols=metrics+CIs, run_id per cell)."},
    {"tool": "add_claim",
     "reason": "File an aggregate claim citing experiment_id as the evidence source."},
])
_rns("get_experiment_table", [
    {"tool": "add_claim",
     "reason": "File an aggregate claim citing experiment_id in evidence_spans."},
    {"tool": "write_research_interpretation",
     "reason": "Draft an interpretation paragraph summarising the experiment results."},
])

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
