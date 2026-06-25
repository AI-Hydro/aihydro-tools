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
# validation, so we must strip both fields BEFORE that validation runs and
# stash them in ContextVars for the request's lifetime.
#
# We do this with a FastMCP **middleware** (``_ContextInjectionMiddleware``
# below), registered via ``mcp.add_middleware``.  This is the supported,
# version-stable extension point: its ``on_call_tool`` hook runs before the
# tool's argument model is validated.  (A previous implementation monkeypatched
# ``mcp._call_tool_mcp`` *after* construction; FastMCP's low-level server binds
# that method during ``__init__``, so the patch became dead code and EVERY tool
# call failed with "Unexpected keyword argument _chat_id".  See
# tests/test_context_injection.py for the regression guard.)
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
    "map_flood_inundation":             1,
    "map_flood_inundation_hydrograph":  1,
    "run_inundation_physics_validation": 1,
    "export_inundation_surrogate_dataset": 2,
    "train_inundation_surrogate": 1,
    "create_cn_grid":                   1,
    "separate_baseflow":                1,
    "compute_flow_duration_curve":      1,
    "compute_flood_frequency":          1,
    "compute_drought_index":            1,
    "compute_soil_loss_rusle":          1,
    "compute_design_hydrograph":        1,
    "describe_model_space":             1,
    "propose_and_train":                1,
    "run_autoresearch":                 1,
    "get_leaderboard":                  3,
    "train_hydro_model":                1,
    "get_model_results":                1,
    "add_claim":                        1,
    "add_assumption":                   1,
    "promote_claim_to_registry":        1,
    "draft_claim_from_run":             1,
    "check_water_balance_consistency":  1,
    "check_temporal_alignment":         1,
    "check_unit_consistency":           1,
    "audit_interpretation":             1,
    "run_skeptic":                      1,
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
    "data_fetch_background":            2,
    "get_data_fetch_result":            2,
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
    "map_fly_to":                       2,
    "map_add_layer_from_run":           2,
    "map_set_time_range":               2,
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
    "get_inundation_physics_result":    3,
    "get_inundation_surrogate_result":  3,
    "wait_for_job":                     3,
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
    # ── Feature registry (C2 — aihydro-core multi-geometry) ──────────────
    "register_feature":                 2,
    "list_features":                    3,
    "set_active_feature":               3,
    "bind_map_to_claim":                2,
    # ── Phase 1.7: pre-registration ──────────────────────────────────────
    "register_research_plan":           2,
    # ── Phase 1.8: validators ─────────────────────────────────────────────
    "check_record_length":              3,
    "check_usgs_qualification_codes":   3,
    "check_regulated_basin":            3,
    "check_stationarity":               3,
    # ── Phase 2.1: experiments ────────────────────────────────────────────
    "define_experiment":                2,
    "run_experiment":                   2,
    "get_experiment_table":             2,
    # ── Phase 2.2: claim registry ─────────────────────────────────────────
    "check_registry_staleness":         2,
    "list_registry_claims":             3,
    # ── Phase 2.4: passage-level literature index ─────────────────────────
    "index_passages":                   2,
    "search_passages_tool":             2,
    "resolve_passage":                  3,
    # ── Wave C: aihydro-lsh community plugin (global CAMELS) ─────────────
    # Tier 1 → scientific artifact (101 scalar attrs / daily forcing);
    # Tier 2 → workflow (batch, parity comparison);
    # Tier 3 → discovery / status (families, attr info, recipe status).
    "lsh_attributes":                   1,
    "lsh_geomorphic_attributes":        1,
    "lsh_forcing":                      1,
    "lsh_dynamic_attributes":           1,
    "lsh_events":                       1,
    "lsh_batch":                        2,
    "lsh_compare_to_ref":               2,
    "lsh_families":                     3,
    "lsh_attribute_info":               3,
    "lsh_recipe_status":                3,
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
        "reasoning. Call tools only when they provide deterministic value, not "
        "to fulfil a procedural checklist. Do not infer a tool, library, or "
        "another server's abilities from its name: your own native toolset is "
        "the primary instrument — verify capabilities with "
        "aihydro_describe_capability or list_available_tools(), and use another "
        "connected server only when the task explicitly needs its specialty. "
        "Prefer the most direct tool that satisfies the request; do not launch "
        "a long-running or multi-stage pipeline unless the request requires "
        "it.\n\n"

        "TOOL DISCLOSURE PROTOCOL. Tools appear at two levels: common ones show "
        "their full schema inline (call directly); the rest are listed by NAME + "
        "one-line summary only (parameters hidden). Before the FIRST call to any "
        "name-only tool, call describe_tool(name) to fetch its parameters and "
        "example, then call it. NEVER guess parameter names. Unsure which tool "
        "exists? Call aihydro_describe_capability(domain) to browse first.\n\n"

        "Check the skill catalog only when the user explicitly asks for a "
        "reusable workflow, a named report format, or says 'use the skill for' "
        "— not before every analysis. Save a reusable workflow with "
        "save_skill() only when the user asks.\n\n"

        "When a tool reports an error, inspect the message and recover: an error "
        "often inlines the schema and a corrected example_call — use it rather "
        "than repeating the failed call. Run a missing prerequisite, adjust "
        "inputs, retry, or explain the remaining blocker with evidence. Do not report a "
        "scientific result as complete when validation, data access, or provenance "
        "failed.\n\n"

        "If several DIFFERENT tools fail with the same structural error "
        "(identical unexpected-keyword/connection/auth message), the cause is "
        "the server or its config, not your arguments — stop after two such "
        "failures and report the blocker plainly (failing tools, shared error, "
        "what to check). Do not silently switch servers or reimplement a failed "
        "tool with shell heredocs — use run_python if you must run code.\n\n"

        "Preserve research context. Check session summaries before repeating "
        "expensive work. Store outputs through tool-supported workspace paths and "
        "keep provenance, parameters, and quality flags visible. After a "
        "computation completes, summarise results directly in your response — "
        "do not call additional tools solely to record an interpretation.\n\n"

        "For long-running work, dispatch an asynchronous job and wait "
        "efficiently: make ONE server-side wait-for-completion call where one "
        "exists, or poll only at the tool's recommended cadence — never in a "
        "tight loop, since each manual check spends a whole turn re-reading the "
        "context. If nothing needs the result yet, do other useful work or hand "
        "back with the job id and an ETA. Reference large artifacts by path; "
        "don't reprint them each turn.\n\n"

        "Be transparent about scientific compromises: fallback data, inferred "
        "outlets, synthetic inputs, failed validation, uncertain geometry, or "
        "model-quality limits must be called out explicitly. Tailor depth, "
        "terminology, and focus to the researcher\u2019s profile.\n\n"

        "If course mode is active, act as a teaching assistant: inspect course "
        "state, respect prerequisites, ask before marking progress, and navigate "
        "only after agreement. If no course is active, proceed as a research "
        "collaborator.\n\n"

        "For spectral indices (NDWI, NDVI, NDBI, NBR, MNDWI, …) use "
        "compute_spectral_index(index_name, ...) rather than custom GEE scripts "
        "or raw-band data_fetch: it handles band fetch, cloud masking, "
        "compositing, colormap, GeoTIFF, and map overlay in one call. Pass "
        "frequency='monthly'/'yearly' for time-series change detection.\n\n"

        "For data retrieval, prefer the variable-centric dataverse interface "
        "that auto-routes by region, falls back across sources, and carries "
        "citation and license metadata. Before an expensive fetch (large bbox, "
        "long window, remote backend), run a pre-flight validation to catch "
        "coverage gaps and estimate payload size.\n\n"

        "Session identity is automatic. Every analysis tool resolves the active "
        "study from the chat context — do NOT prompt the user for a session_id "
        "or to name the study. The delineation tools auto-create and bind a "
        "study on first call; later tools in the same chat reuse it. Pass "
        "session_id only to switch studies; use aihydro_chat_status() and "
        "aihydro_rebind_chat(study_id) to inspect or fix the binding."
    ),
)


# ---------------------------------------------------------------------------
# Wave 3 Axis 3 — strip injected identity params before argument validation
# ---------------------------------------------------------------------------
# FastMCP validates tool arguments through a Pydantic model that rejects any
# key not declared as a function parameter.  The extension injects ``_chat_id``
# and ``_workspace`` into every call, so both must be removed BEFORE that
# validation runs.  We use a FastMCP middleware whose ``on_call_tool`` hook
# fires before the tool's argument model is built — the supported, version-
# stable place to mutate arguments.  The hook also stores the popped values in
# request-scoped ContextVars and resets them when the call completes.
from fastmcp.server.middleware import Middleware, MiddlewareContext  # noqa: E402


class _ContextInjectionMiddleware(Middleware):
    """Pop ``_chat_id`` / ``_workspace`` from tool args into ContextVars.

    Runs for EVERY tool call.  When neither key is present (direct Python
    calls, tests, CLI) it is a cheap no-op that still binds the ContextVars to
    ``None`` for the duration of the call.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):  # type: ignore[override]
        chat_id: str | None = None
        workspace: str | None = None

        message = context.message
        args = getattr(message, "arguments", None)
        if args and ("_chat_id" in args or "_workspace" in args):
            args = dict(args)
            chat_id = args.pop("_chat_id", None)
            workspace = args.pop("_workspace", None)
            if not isinstance(chat_id, str):
                chat_id = None
            if not isinstance(workspace, str):
                workspace = None
            # Mutate the request in place so downstream validation never sees
            # the injected keys.
            message.arguments = args

        token_chat = ACTIVE_CHAT_ID.set(chat_id)
        token_ws = ACTIVE_WORKSPACE.set(workspace)
        try:
            return await call_next(context)
        finally:
            ACTIVE_CHAT_ID.reset(token_chat)
            ACTIVE_WORKSPACE.reset(token_ws)


mcp.add_middleware(_ContextInjectionMiddleware())
