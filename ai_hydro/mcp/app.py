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

import contextvars
from fastmcp import FastMCP, Context

__all__ = [
    "mcp", "Context", "TOOL_TIERS", "get_tool_tiers", "get_tool_tier",
    "ACTIVE_CHAT_ID", "ACTIVE_WORKSPACE",
]

# ---------------------------------------------------------------------------
# Per-request identity context (Wave 3 Axis 3 + Design A)
# ---------------------------------------------------------------------------
# The TypeScript extension injects ``_chat_id`` and ``_workspace`` into every
# ai-hydro MCP tool call.  FastMCP rejects unknown parameters via Pydantic
# validation, so we intercept the raw arguments dict inside ``_call_tool_mcp``
# BEFORE dispatch, pop both fields, and store them in ContextVars.
#
# _resolve_session() in helpers.py reads ACTIVE_CHAT_ID automatically.
# ACTIVE_WORKSPACE carries the VS Code workspaceFolders[0] path so that
# auto-created sessions can set workspace_dir without the tool declaring it.
ACTIVE_CHAT_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ACTIVE_CHAT_ID", default=None
)
ACTIVE_WORKSPACE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ACTIVE_WORKSPACE", default=None
)

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
    "merit_ensure_routing_region":      2,
    "merit_ensure_basins_region":       2,
    "merit_ensure_region":              2,
    "merit_add_map_layers":             2,
    "delineation_doctor":               3,
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
    # fetch_streamflow_data / fetch_forcing_data demoted to tier 3 (Wave 2.5
    # Axis 4): agents should now use data_fetch directly; legacy tools are
    # retained as backward-compat shims only and no longer surface by default.
    "fetch_streamflow_data":            3,
    "fetch_forcing_data":               3,
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
    "lookup_citation":                  2,
    "get_citation_by_doi":              2,
    "data_fetch":                       2,
    "data_batch_fetch":                 2,
    "data_list_products":               2,
    "data_describe_product":            2,
    "data_validate_request":            2,
    "add_journal_entry":                2,
    "log_researcher_observation":       2,
    "map_set_roi":                      2,
    "map_set_working_geometry":         2,
    "map_save_roi":                     2,
    "map_update_layer":                 2,
    "map_apply_symbology":              2,
    "preview_revise_section":           2,
    "preview_address_comment":          2,
    # ── Tier 3: Infrastructure ─────────────────────────────────────────────
    # Session plumbing, discovery, profile management; zero validation load.
    "start_session":                    3,
    "get_session_summary":              3,
    "get_session_health":               3,
    "clear_session":                    3,
    "archive_session":                  3,
    "get_session_raw_state":            3,
    "merge_session_shards":             3,
    "list_available_tools":             3,
    "list_claims":                      3,
    "list_assumptions":                 3,
    "get_training_status":              3,
    "cancel_job":                       3,
    "list_jobs":                        3,
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
    "map_get_state":                    3,
    "map_show":                         3,
    "map_fit_extent":                   3,
    "map_list_layers":                  3,
    "map_remove_layer":                 3,
    "map_set_basemap":                  3,
    "map_fit_layer":                    3,
    "list_skills":                      3,
    "load_skill":                       3,
    "save_skill":                       3,
    "show_html_preview":                3,
    "preview_get_state":                3,
    "preview_recent_events":            3,
    "preview_list_modules":             3,
    "preview_focus_cell":               3,
    "preview_get_pending_changes":      3,
    "list_cached_citations":            3,
    "list_available_workflows":         3,
    "get_workflow_manifest":            3,
    "gee.status":                       3,
    "start_project":                    3,
    "get_project_summary":              3,
    "add_session_to_project":           3,
    "get_researcher_profile":           3,
    "update_researcher_profile":        3,
    # ── Course mode (v1.8.0) ─────────────────────────────────────────────
    "course_get_state":                 3,
    "course_get_curriculum":            3,
    "course_set_progress":              2,
    "course_navigate":                  2,
    "course_scaffold":                  2,
    # ── Discovery (v1.8.0) ───────────────────────────────────────────────
    "aihydro_describe_capability":      3,
    "describe_tool":                    3,
    "describe_tools":                   3,
    # ── Dataverse infra (Wave 2.5 / v1.8.0) ──────────────────────────────
    # data_get_cache_status, data_invalidate_cache, data_doctor, data_help
    # are utility/discovery tools — no scientific validation load.
    "data_get_cache_status":            3,
    "data_invalidate_cache":            3,
    "data_doctor":                      3,
    "data_help":                        3,
    # ── Chat-native session management (Wave 3) ───────────────────────────
    "aihydro_rebind_chat":              3,
    "aihydro_chat_status":              3,
    # ── Spectral indices (v0.2.0 / TorchGeo cherry-pick Day 5) ───────────
    "compute_spectral_index":           2,
    "list_spectral_indices":            3,
}


# ---------------------------------------------------------------------------
# Hot tools — the "full schema, always inline" set for context injection.
#
# A tool is HOT when its complete inputSchema is injected into the system
# prompt verbatim (zero round-trip to call correctly). Everything else is
# injected as a one-line summary and its schema is fetched on demand via
# describe_tool(). Keep this set small and high-frequency: all Tier-1
# scientific tools are hot automatically, plus a curated allowlist of the
# entry-point tools the agent reaches for constantly, plus the discovery
# tools themselves (so the agent can always see how to call them).
#
# See ai_hydro/mcp/__init__.py:_tag_tools_with_tier_meta() which stamps the
# resulting `hot` flag into each tool's MCP `_meta`, and the extension's
# system-prompt/components/mcp.ts which renders full vs. summary accordingly.
# ---------------------------------------------------------------------------
HOT_TOOL_ALLOWLIST: frozenset[str] = frozenset({
    # High-frequency entry points (mostly Tier 2/3 but used in nearly every run)
    "start_session",
    "get_session_summary",
    "data_fetch",
    "compute_spectral_index",
    "fetch_camels_us",
    "run_python",
    # Discovery tools — must be fully visible so the agent can always use the
    # on-demand schema-fetch protocol.
    "aihydro_describe_capability",
    "describe_tool",
    "describe_tools",
    "list_available_tools",
})


def is_hot_tool(name: str) -> bool:
    """True if a tool's full schema should be injected inline (vs. summary-only).

    Hot = any Tier-1 tool (scientific output) or a member of HOT_TOOL_ALLOWLIST.
    """
    return TOOL_TIERS.get(name) == 1 or name in HOT_TOOL_ALLOWLIST


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
        "You are AI-Hydro, a scientific research assistant for hydrology and "
        "earth sciences. Your scope includes surface water, groundwater, snow, "
        "remote sensing, climate, water quality, ungauged basins, global data, "
        "modeling, reproducibility, and the researcher\u2019s custom workflows.\n\n"

        "Use tools for deterministic computation and state management; use your "
        "judgment for study design, interpretation, caveats, and next-step "
        "reasoning. Call tools only when they provide deterministic value — "
        "never call a tool to look up something you already know or to fulfil "
        "a procedural checklist. Do not guess tool or library names; if you "
        "need to verify a name use list_available_tools() once.\n\n"

        "TOOL DISCLOSURE PROTOCOL. Tools appear at two levels: common ones show "
        "their full schema inline (call directly); the rest are listed by NAME + "
        "one-line summary only (parameters hidden). Before the FIRST call to any "
        "name-only tool, call describe_tool(name) to fetch its parameters and "
        "example, then call it. NEVER guess parameter names. Unsure which tool "
        "exists? Call aihydro_describe_capability(domain) to browse first.\n\n"

        "Check the skill catalog only when the user explicitly asks for a "
        "reusable workflow, a named report format, or says 'use the skill for'. "
        "Do NOT check skills before every multi-step analysis — that adds "
        "unnecessary latency. If a completed workflow is genuinely reusable and "
        "the user asks you to save it, use save_skill().\n\n"

        "When a tool reports an error, inspect the message and recover: an error "
        "often inlines the schema and a corrected example_call — use it rather "
        "than repeating the failed call. Run a missing prerequisite, adjust "
        "inputs, retry, or explain the remaining blocker with evidence. Do not report a "
        "scientific result as complete when validation, data access, or provenance "
        "failed.\n\n"

        "Preserve research context. Check session summaries before repeating "
        "expensive work. Store outputs through tool-supported workspace paths and "
        "keep provenance, parameters, and quality flags visible. After a "
        "computation completes, summarise results directly in your response — "
        "do not call additional tools solely to record an interpretation.\n\n"

        "For long-running work, prefer asynchronous jobs with a job identifier "
        "and artifact path. For parallel shards, use separate session shards "
        "and merge when complete.\n\n"

        "Be transparent about scientific compromises: fallback data, inferred "
        "outlets, synthetic inputs, failed validation, uncertain geometry, or "
        "model-quality limits must be called out explicitly. Tailor depth, "
        "terminology, and focus to the researcher\u2019s profile.\n\n"

        "If course mode is active, act as a teaching assistant: inspect course "
        "state, respect prerequisites, ask before marking progress, and navigate "
        "only after agreement. If no course is active, proceed as a research "
        "collaborator.\n\n"

        "For spectral indices (NDWI, NDVI, NDBI, NBR, MNDWI, …) use "
        "compute_spectral_index(index_name, ...) — never write custom GEE "
        "scripts or call data_fetch with raw band names. It handles band "
        "fetch, cloud masking, compositing, colormap, GeoTIFF, and map overlay "
        "in one call. Pass frequency='monthly'/'yearly' for time-series change "
        "detection (per-period stats + Mann-Kendall trend).\n\n"

        "For data retrieval, prefer the variable-centric dataverse interface "
        "that auto-routes by region, falls back across sources on failure, and "
        "carries citation and license metadata on every result. Before an "
        "expensive fetch (large bbox, long window, remote compute backend), "
        "run a pre-flight validation to catch coverage gaps and estimate "
        "payload size. Use the bundled onboarding help once per session rather "
        "than guessing the API; inspect individual product specs on demand "
        "rather than listing every catalog entry. Older single-source fetch "
        "wrappers are retained for backward compatibility but routed through "
        "the new pipeline internally — their responses carry a deprecation "
        "note. Discover the full data-fetch tool surface via the capability "
        "discovery tool with the corresponding domain filter.\n\n"

        "Session identity is automatic. Every analysis tool resolves the active "
        "study from the chat context — do NOT prompt the user to supply a "
        "session_id or ask them to name the study. The delineation tools "
        "(delineate_watershed, delineate_watershed_from_point) auto-create a "
        "study and bind it to the current chat on first call; all subsequent "
        "tools in the same chat operate on that study automatically. Pass "
        "session_id explicitly only when you need to switch to a named study. "
        "Use aihydro_chat_status() to inspect the current binding and "
        "aihydro_rebind_chat(study_id) to recover when the wrong study was "
        "selected. Never use 'map' as a session_id — it is a legacy placeholder "
        "that may collide with other chats."
    ),
)


# ---------------------------------------------------------------------------
# Wave 3 Axis 3 — intercept raw MCP tool-call arguments to extract _chat_id
# ---------------------------------------------------------------------------
# FastMCP validates tool arguments through a Pydantic model that rejects any
# key not declared as a function parameter.  We need to strip ``_chat_id``
# BEFORE that validation runs.  The lowest-level entry point is
# ``FastMCP._call_tool_mcp(key, arguments)``; we wrap it here so no tool
# function ever sees ``_chat_id`` while still making it available via the
# ACTIVE_CHAT_ID ContextVar throughout the request lifetime.

_original_call_tool_mcp = mcp._call_tool_mcp  # type: ignore[attr-defined]


async def _patched_call_tool_mcp(key: str, arguments: dict) -> object:  # type: ignore[override]
    """Strip ``_chat_id`` + ``_workspace`` from ``arguments`` and store in ContextVars."""
    chat_id: str | None = None
    workspace: str | None = None
    if arguments and ("_chat_id" in arguments or "_workspace" in arguments):
        # Work on a copy so we don't mutate the caller's dict.
        arguments = dict(arguments)
        chat_id = arguments.pop("_chat_id", None)
        workspace = arguments.pop("_workspace", None)
        if not isinstance(chat_id, str):
            chat_id = None
        if not isinstance(workspace, str):
            workspace = None

    token_chat = ACTIVE_CHAT_ID.set(chat_id)
    token_ws = ACTIVE_WORKSPACE.set(workspace)
    try:
        return await _original_call_tool_mcp(key, arguments)
    finally:
        ACTIVE_CHAT_ID.reset(token_chat)
        ACTIVE_WORKSPACE.reset(token_ws)


mcp._call_tool_mcp = _patched_call_tool_mcp  # type: ignore[method-assign]
