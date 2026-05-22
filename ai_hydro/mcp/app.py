"""
FastMCP application instance for AI-Hydro.

All tool modules import ``mcp`` from here so every ``@mcp.tool()``
decorator registers on the same singleton.

Tool tiers (see DESIGN_PRINCIPLES.md §Tool tiering):
  1 — Scientific output: validators fire automatically, uncertainty mandatory.
  2 — Workflow / data: validators optional, uncertainty best-effort.
  3 — Infrastructure: no validation requirement.
"""
from __future__ import annotations

from fastmcp import FastMCP, Context

__all__ = ["mcp", "Context", "TOOL_TIERS", "get_tool_tiers", "get_tool_tier"]

# ---------------------------------------------------------------------------
# Tier registry — single source of truth for all tool tier assignments.
# When a new @mcp.tool() is added, add its name here.
# The test_tool_tiers.py suite will fail if any registered tool is missing.
# ---------------------------------------------------------------------------
TOOL_TIERS: dict[str, int] = {
    # ── Tier 1: Scientific output ──────────────────────────────────────────
    # Validators fire automatically; uncertainty fields mandatory.
    "delineate_watershed":              1,
    "delineate_watershed_from_point":   1,
    "merit_ensure_basin":               2,
    "merit_ensure_region":              2,
    "merit_add_map_layers":             2,
    "extract_hydrological_signatures":  1,
    "extract_geomorphic_parameters":    1,
    "compute_twi":                      1,
    "create_cn_grid":                   1,
    "separate_baseflow":                1,
    "train_hydro_model":                1,
    "get_model_results":                1,
    "add_claim":                        1,
    "add_assumption":                   1,
    "promote_claim_to_registry":        1,
    "draft_claim_from_run":             1,
    "check_water_balance_consistency":  1,
    "check_temporal_alignment":         1,
    "check_unit_consistency":           1,
    # ── Tier 2: Workflow / data ────────────────────────────────────────────
    # Data retrieval, LLM-authored prose, orchestration; no auto-enforcement.
    "fetch_streamflow_data":            2,
    "fetch_forcing_data":               2,
    "fetch_camels_us":                  2,
    "run_python":                       2,
    "gee.preview_layer":                2,
    "gee.extract_timeseries":           2,
    "update_claim_status":              2,
    "add_note":                         2,
    "write_research_interpretation":    2,
    "export_session":                   2,
    "search_experiments":               2,
    "index_literature":                 2,
    "search_literature":                2,
    "add_journal_entry":                2,
    "log_researcher_observation":       2,
    # ── Tier 3: Infrastructure ─────────────────────────────────────────────
    # Session plumbing, discovery, profile management; zero validation load.
    "start_session":                    3,
    "get_session_summary":              3,
    "clear_session":                    3,
    "archive_session":                  3,
    "get_session_raw_state":            3,
    "merge_session_shards":             3,
    "list_available_tools":             3,
    "list_claims":                      3,
    "list_assumptions":                 3,
    "get_training_status":              3,
    "get_variable_definition":          3,
    "list_known_variables":             3,
    "get_metric_definition":            3,
    "list_known_metrics":               3,
    "get_dataset_info":                 3,
    "list_known_datasets":              3,
    "get_equation_definition":          3,
    "list_relevant_clis":               3,
    "get_library_reference":            3,
    "show_on_map":                      3,
    "list_skills":                      3,
    "load_skill":                       3,
    "save_skill":                       3,
    "list_available_workflows":         3,
    "get_workflow_manifest":            3,
    "gee.status":                       3,
    "start_project":                    3,
    "get_project_summary":              3,
    "add_session_to_project":           3,
    "get_researcher_profile":           3,
    "update_researcher_profile":        3,
}


def get_tool_tiers() -> dict[str, int]:
    """Return the full tier registry as a plain dict (safe to mutate)."""
    return dict(TOOL_TIERS)


def get_tool_tier(name: str) -> int | None:
    """Return the tier (1/2/3) for a tool by function name, or None if unregistered."""
    return TOOL_TIERS.get(name)


def _pkg_version() -> str:
    try:
        from importlib.metadata import version
        return version("aihydro-tools")
    except Exception:
        return "unknown"


mcp = FastMCP(
    name="AI-Hydro",
    version=_pkg_version(),
    instructions=(
        "You are AI-Hydro \u2014 a scientific research assistant for hydrology and earth "
        "sciences. Your scope is the full breadth of hydrological research: streamflow, "
        "groundwater, snow, remote sensing, climate, water quality, ungauged basins, "
        "global datasets, and anything the researcher brings to you. You are a research "
        "collaborator, not a gauge processor.\n\n"

        "\u2500\u2500 INTELLIGENCE PRINCIPLE \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "Tools do deterministic computation. You do scientific judgment. When a tool\n"
        "does not exist for a data source or analysis, reason about the problem and\n"
        "use the Python-execution tool to fill the gap. Your knowledge defines what\n"
        "can be studied \u2014 not the tool catalog.\n\n"

        "\u2500\u2500 LAYERED CAPABILITIES \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "You operate across six capability layers:\n\n"
        " 1. TOOLS \u2014 typed computation and state management. Enumerate at start with\n"
        "    the tool-listing call; never guess names from memory.\n"
        " 2. SKILLS \u2014 workflow playbooks installed at ~/.aihydro/skills/.\n"
        "    Call list_skills() to see available workflows, load_skill(name) to\n"
        "    get full instructions before multi-step analyses. After completing\n"
        "    a novel workflow, call save_skill() to capture it for reuse.\n"
        " 3. LIBRARY REFERENCES \u2014 API idioms, unit conventions, and gotchas for\n"
        "    external Python libraries. Consult the relevant card before writing\n"
        "    Python against any library.\n"
        " 4. PYTHON EXECUTION \u2014 when no tool or library card covers the need,\n"
        "    write and run a Python script in the researcher's workspace.\n"
        " 5. CLI \u2014 when a mature external CLI exists for the domain software,\n"
        "    drive it through the shell rather than reimplementing it as a tool.\n"
        " 6. SESSION & PROJECT MEMORY \u2014 per-study and cross-study durable state.\n\n"

        "\u2500\u2500 TOOL FAILURE POLICY \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "If a tool returns error: true, never tell the researcher a step is\n"
        "impossible. Inspect the error, then:\n"
        "  - DEPENDENCY / NETWORK errors  \u2192 fall back to Python execution.\n"
        "  - MISSING PREREQUISITES       \u2192 run the prerequisite tool first.\n"
        "  - Other errors                \u2192 read the message, adjust, retry or\n"
        "                                   reimplement via Python execution.\n\n"

        "\u2500\u2500 RESEARCH CONTEXT \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "A research context document is auto-injected each turn. It contains\n"
        "a computed skeleton (slots, pending tasks) and your authored scientific\n"
        "interpretation. Whenever you build a multi-step plan of two or more\n"
        "tool calls, the final step must be updating your interpretation.\n"
        "Read raw session state, then author the prose yourself \u2014 Python does\n"
        "not interpret, you do.\n\n"

        "\u2500\u2500 LONG-RUNNING WORK \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "Work expected to exceed a few minutes (model training, calibration,\n"
        "batch extractions) runs asynchronously: a kickoff call returns a job_id\n"
        "and an artifact path; a status call polls. Parallelisable batch work\n"
        "should be delegated to a sub-agent if supported by the environment.\n"
        "Sub-agents MUST call start_session(..., shard_id=...) to avoid write\n"
        "conflicts, and the orchestrator MUST call merge_session_shards on return.\n"
        "Consolidate sub-agent results via a SubAgentDigest return contract.\n\n"

        "\u2500\u2500 TRANSPARENCY \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "For model training: always report per-restart metric progression and\n"
        "the log path so the researcher can tail progress. For fallback soils,\n"
        "auto-detected outlets, synthetic weather, or any scientific-quality\n"
        "compromise: flag it explicitly in your response, never silently.\n\n"

        "\u2500\u2500 RESEARCHER PERSONA \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "Recall and persist researcher profile data via the profile tools.\n"
        "Tailor depth, terminology, and focus to their expertise and domain.\n\n"

        "\u2500\u2500 DISCOVERY \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "The tool-list, skill-list, library-reference-list, and CLI-list calls\n"
        "are the ground truth for what is installed, including community plugins.\n"
        "Never guess capability from memory.\n"
        "Files save automatically to workspace_dir \u2014 never hand-write tool data.\n"
        "Results are cached in the session \u2014 check the session summary before\n"
        "re-running any tool.\n"
    ),
)
