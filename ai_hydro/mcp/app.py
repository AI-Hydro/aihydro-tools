"""
FastMCP application instance for AI-Hydro.

All tool modules import ``mcp`` from here so every ``@mcp.tool()``
decorator registers on the same singleton.

Tool tiers (see DESIGN_PRINCIPLES.md §Tool tiering):
  1 — Scientific output: eligible for registered post-run validators.
  2 — Workflow / data: lighter audit expectations.
  3 — Infrastructure: no validation requirement.

Tier assignment does not itself guarantee that a validator is registered or
that uncertainty is available. Post-run validators are advisory; the separate
claim-promotion gate enforces evidence, limitations, researcher approval, and
uncertainty for metric-scoped empirical claims.
"""
from __future__ import annotations

import contextvars
from fastmcp import FastMCP, Context

__all__ = [
    "mcp", "Context", "TOOL_TIERS", "get_tool_tiers", "get_tool_tier",
    "ACTIVE_CHAT_ID", "ACTIVE_WORKSPACE", "ACTIVE_STUDY_ID",
    "ACTIVE_CONTEXT_SOURCE", "ACTIVE_CONTEXT_CLIENT", "CONTEXT_META_KEY", "ACTIVE_READ_ONLY",
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
# Explicit context (ADR-004): clients send ``_meta["aihydro/context"] =
# {study_id?, workspace?, chat_id?, client?}`` on tools/call.  ``study_id`` is
# the session_id (no tool is renamed).  The legacy hidden ``_chat_id`` /
# ``_workspace`` arguments still work, are read second, and log a one-time
# deprecation notice.  ACTIVE_CONTEXT_SOURCE is "meta", "legacy_args" or "none"
# and is stamped into run records as ``extra.context_source``.
CONTEXT_META_KEY = "aihydro/context"
ACTIVE_STUDY_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ACTIVE_STUDY_ID", default=None
)
ACTIVE_CONTEXT_SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ACTIVE_CONTEXT_SOURCE", default="none"
)
# ``_meta["aihydro/context"].project_id`` (``prj_...``): the chat's project, sent by the host once it
# has called ensure_project. Without it the project is found from the workspace directory.
ACTIVE_PROJECT_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ACTIVE_PROJECT_ID", default=None
)
ACTIVE_CONTEXT_CLIENT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ACTIVE_CONTEXT_CLIENT", default=None
)
# True while a tool annotated readOnlyHint=true (tool_annotations.toml) runs through the MCP server.
# Session resolution then has no side effects: no chat rebinding, no workspace_dir backfill/save.
ACTIVE_READ_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ACTIVE_READ_ONLY", default=False
)
# A per-call mutable dict (set by _SessionWarningMiddleware) that session resolution writes into
# when it guesses the "most recent study" (key ``recent_fallback_session``). A tool runs in a worker
# thread with a copy of the context, so only a shared mutable object carries the note back.
ACTIVE_CALL_NOTES: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ACTIVE_CALL_NOTES", default=None
)

# Name of the tool being called (set by _ContextInjectionMiddleware). The offload wrapper and the
# SESSION_BUSY result use it to say which tool is running.
ACTIVE_TOOL_NAME: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ACTIVE_TOOL_NAME", default=None
)

# ---------------------------------------------------------------------------
# Tier registry — single source of truth for all tool tier assignments.
# When a new @mcp.tool() is added, add its name here.
# The test_tool_tiers.py suite will fail if any registered tool is missing.
# ---------------------------------------------------------------------------
TOOL_TIERS: dict[str, int] = {
    # ── Tier 1: Scientific output ──────────────────────────────────────────
    # Higher-consequence outputs; registered validators run where instrumented.
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
    "fetch_soil_attributes_ssurgo":     2,
    "gauge_network_context":            2,
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
    # Deterministic series/geometry tools (identical in every evaluation arm).
    "summarize_series":                 1,
    "detect_threshold_runs":            1,
    "compare_series":                   1,
    "bootstrap_statistic":              1,
    "measure_feature":                  1,
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
    # ── Project registry v2 (T-P1; host audience) ─────────────────────────
    "ensure_project":                   3,
    "rename_project":                   3,
    "set_project_default_study":        3,
    "add_project_study":                3,
    "remove_project_study":             3,
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
    "bind_claim_to_item":               2,
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
        from ai_hydro import __version__
        return __version__      # the running code, not (possibly stale) dist-info
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
        "aihydro_describe_capability(domain), and use another "
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


def _register_form(function):
    """What FastMCP registers for ``function``: a sync tool becomes an async wrapper that runs the
    original in a worker thread (``ai_hydro.mcp.offload``), so no tool or resource can block the
    event loop. Async functions pass through unchanged."""
    from ai_hydro.mcp.offload import offload_sync

    return offload_sync(function)


def _preserve_callable_registration(method):
    """Register FastMCP components but leave ordinary callables in modules.

    FastMCP 2.7+ decorators replace decorated functions with component wrapper
    objects. Keeping the original function is useful for internal composition
    and direct unit tests; registration still goes through FastMCP unchanged.
    """
    def decorator_factory(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            function = args[0]
            method(_register_form(function))
            return function

        register = method(*args, **kwargs)

        def decorate(function):
            register(_register_form(function))
            return function

        return decorate

    return decorator_factory


# Stable project convention: decorated implementations remain regular Python
# callables while FastMCP owns the protocol-facing registered components.
mcp.tool = _preserve_callable_registration(mcp.tool)
mcp.resource = _preserve_callable_registration(mcp.resource)


async def _list_registered_tools():
    """Return registered tool components through FastMCP's public accessor."""
    return list((await mcp.get_tools()).values())


async def _call_registered_tool(name, arguments=None):
    """Compatibility facade for the former FastMCP ``call_tool`` helper."""
    return await mcp._tool_manager.call_tool(name, arguments or {})


async def _read_registered_resource(uri):
    """Preserve the small legacy ``read_resource`` result shape used internally."""
    from types import SimpleNamespace

    content = mcp._resource_manager.read_resource(uri)
    if hasattr(content, "__await__"):
        content = await content
    return SimpleNamespace(contents=[SimpleNamespace(content=content)])


# FastMCP 2.14 removed these convenience methods from the server object. Keep
# the package's existing internal call sites stable while using its current
# managers/accessors underneath.
mcp.list_tools = _list_registered_tools
mcp.call_tool = _call_registered_tool
mcp.read_resource = _read_registered_resource


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


import logging as _ctx_logging  # noqa: E402

log = _ctx_logging.getLogger("ai_hydro.mcp.app")
_legacy_context_warned = False


def _request_context_meta(context) -> dict:
    """The ``aihydro/context`` object from the request ``_meta``, or ``{}``.

    FastMCP 2.14 does not copy ``_meta`` onto the middleware message
    (``CallToolRequestParams.meta`` is None there); it is on the live request
    context (``fastmcp_context.request_context.meta``), where unknown keys are
    pydantic extras.  Anything unreadable yields ``{}``.
    """
    try:
        meta = context.fastmcp_context.request_context.meta
    except Exception:
        meta = None
    if meta is None:
        meta = getattr(context.message, "meta", None)
    if meta is None:
        return {}
    value = None
    try:
        value = meta.get(CONTEXT_META_KEY) if isinstance(meta, dict) else getattr(meta, CONTEXT_META_KEY, None)
        if value is None:
            value = (getattr(meta, "model_extra", None) or {}).get(CONTEXT_META_KEY)
    except Exception:
        value = None
    return value if isinstance(value, dict) else {}


def _str_or_none(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


class _ContextInjectionMiddleware(Middleware):
    """Bind request context (``_meta`` first, legacy hidden args second).

    Runs for EVERY tool call.  The legacy ``_chat_id`` / ``_workspace`` keys
    are always stripped from the arguments before validation.  Per field,
    ``_meta["aihydro/context"]`` wins over the legacy argument; the recorded
    source is ``meta`` when any field came from ``_meta``, else
    ``legacy_args`` when any came from a legacy argument, else ``none``.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):  # type: ignore[override]
        global _legacy_context_warned
        meta_ctx = _request_context_meta(context)
        chat_id = _str_or_none(meta_ctx.get("chat_id"))
        workspace = _str_or_none(meta_ctx.get("workspace"))
        study_id = _str_or_none(meta_ctx.get("study_id"))
        project_id = _str_or_none(meta_ctx.get("project_id"))
        client = _str_or_none(meta_ctx.get("client"))
        used_meta = any((chat_id, workspace, study_id, project_id, client))
        used_legacy = False

        message = context.message
        args = getattr(message, "arguments", None)
        if args and ("_chat_id" in args or "_workspace" in args):
            args = dict(args)
            legacy_chat = args.pop("_chat_id", None)
            legacy_ws = args.pop("_workspace", None)
            if isinstance(legacy_chat, str) and legacy_chat and chat_id is None:
                chat_id = legacy_chat
                used_legacy = True
            if isinstance(legacy_ws, str) and legacy_ws and workspace is None:
                workspace = legacy_ws
                used_legacy = True
            # Mutate the request in place so downstream validation never sees
            # the injected keys.
            message.arguments = args
            if not _legacy_context_warned:
                _legacy_context_warned = True
                log.warning(
                    "Deprecated: hidden _chat_id/_workspace tool arguments. "
                    "Send _meta[%r] = {study_id, workspace, chat_id, client} "
                    "instead; the legacy arguments are read second and will "
                    "be removed in a later release.", CONTEXT_META_KEY,
                )

        source = "meta" if used_meta else ("legacy_args" if used_legacy else "none")
        tokens = (
            (ACTIVE_CHAT_ID, ACTIVE_CHAT_ID.set(chat_id)),
            (ACTIVE_WORKSPACE, ACTIVE_WORKSPACE.set(workspace)),
            (ACTIVE_STUDY_ID, ACTIVE_STUDY_ID.set(study_id)),
            (ACTIVE_PROJECT_ID, ACTIVE_PROJECT_ID.set(project_id)),
            (ACTIVE_CONTEXT_SOURCE, ACTIVE_CONTEXT_SOURCE.set(source)),
            (ACTIVE_CONTEXT_CLIENT, ACTIVE_CONTEXT_CLIENT.set(client)),
            (ACTIVE_TOOL_NAME, ACTIVE_TOOL_NAME.set(getattr(message, "name", None) or None)),
        )
        try:
            return await call_next(context)
        finally:
            for var, token in reversed(tokens):
                var.reset(token)


class _SessionWarningMiddleware(Middleware):
    """Outermost: add visible top-level fields to a result whose session was inferred or rebound.

    ``session_warning``  the session was the recent-study guess.
    ``session_note``     the session was the only study in the workspace.
    ``session_binding``  this call changed the chat's binding; the host refreshes its display from it
                         (the server store is the only authority). Not added in evaluation runs.

    Applies to every tool (including record-exempt ones) and runs after the run record is sealed, so
    the sealed digest is unaffected. Errors are left alone.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):  # type: ignore[override]
        notes: dict = {}
        token = ACTIVE_CALL_NOTES.set(notes)
        try:
            result = await call_next(context)
        finally:
            ACTIVE_CALL_NOTES.reset(token)
        sid = notes.get("recent_fallback_session")
        if sid:
            from ai_hydro.mcp.tool_surface import read_only_tools

            name = getattr(context.message, "name", "") or ""
            _inject_session_warning(result, sid, read_only=name in read_only_tools())
        single = notes.get("workspace_single_study")
        if single:
            _inject_top_level(result, "session_note", WORKSPACE_SINGLE_STUDY_NOTE.format(**single))
        pdefault = notes.get("project_default")
        if pdefault:
            _inject_top_level(result, "session_note", PROJECT_DEFAULT_NOTE.format(**pdefault))
        binding = notes.get("session_binding")
        if binding:
            from ai_hydro.mcp.helpers import _in_eval

            if not _in_eval():
                _inject_top_level(result, "session_binding", binding)
        return result


mcp.add_middleware(_SessionWarningMiddleware())
mcp.add_middleware(_ContextInjectionMiddleware())
# Evaluation arms (P1/W2): registered BEFORE RunRecordMiddleware so it wraps it and strips only after sealing.
from ai_hydro.mcp.eval_condition import EvalConditionMiddleware as _EvalConditionMiddleware  # noqa: E402
mcp.add_middleware(_EvalConditionMiddleware())


# ---------------------------------------------------------------------------
# Run records (ADR-001) — every tool call that resolves a session
# ---------------------------------------------------------------------------
# Registered AFTER _ContextInjectionMiddleware, so it runs inside it: the
# injected _chat_id is already stripped from the arguments and ACTIVE_CHAT_ID
# is still set when this middleware resolves the session after the call.
#
# Contract (see ai_hydro/session/run_records.py for the record itself):
#   * never breaks a tool call — every failure here is caught and logged;
#   * never alters the tool's result, except to add ``_record_error`` when a
#     record could not be built or stored completely, and ``_run_id`` (the id of
#     the sealed row, so the agent can cite it) to a successful dict result that
#     does not already carry one;
#   * tools listed in RECORD_EXEMPT are skipped (catalog, read-only views, UI
#     state, lifecycle); calls with no resolvable session are counted as
#     ``no_session`` and not recorded.
import asyncio  # noqa: E402
import json as _json  # noqa: E402
import time as _time  # noqa: E402
import logging as _logging  # noqa: E402

_record_log = _logging.getLogger("ai_hydro.mcp.run_records")


def _tool_result_dict(tool_result):
    """``(dict_or_None, serialized_bytes)`` for a FastMCP ``ToolResult``."""
    size = 0
    text_dict = None
    for block in getattr(tool_result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            size += len(text)
            if text_dict is None:
                try:
                    parsed = _json.loads(text)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    text_dict = parsed
    structured = getattr(tool_result, "structured_content", None)
    if isinstance(structured, dict):
        if set(structured) == {"result"}:
            return structured["result"], size       # wrapped non-object result
        return structured, size
    if text_dict is not None:
        return text_dict, size
    # List, string or number results: digest what was delivered rather than
    # leaving the output digest silently empty.
    for block in getattr(tool_result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                return _json.loads(text), size
            except ValueError:
                return text, size
    return None, size


def _inject_record_error(tool_result, message: str) -> None:
    """Add ``_record_error`` to the result the caller sees. Best effort."""
    try:
        structured = getattr(tool_result, "structured_content", None)
        if isinstance(structured, dict):
            structured["_record_error"] = message
        for block in getattr(tool_result, "content", None) or []:
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                continue
            try:
                parsed = _json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                parsed["_record_error"] = message
                block.text = _json.dumps(parsed, default=str)
                return
    except Exception as exc:  # the result must still go out
        _record_log.debug("could not inject _record_error: %s", exc)


def _inject_run_id(tool_result, run_id: str) -> None:
    """Add ``_run_id`` to a successful dict result that does not carry one.

    General mechanism for every recorded tool: the id of the sealed row the
    middleware just stored is the address the agent cites. Tools that already
    return ``_run_id`` (via ``post_run``) are left alone. ``_run_id`` is a
    transport key, excluded from the output digest, so the sealed record is
    unaffected. Best effort; the result must still go out.
    """
    try:
        structured = getattr(tool_result, "structured_content", None)
        if isinstance(structured, dict) and set(structured) != {"result"}:
            if structured.get("error") or "_run_id" in structured:
                return
            structured["_run_id"] = run_id
        for block in getattr(tool_result, "content", None) or []:
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                continue
            try:
                parsed = _json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                if not parsed.get("error") and "_run_id" not in parsed:
                    parsed["_run_id"] = run_id
                    block.text = _json.dumps(parsed, default=str)
                return
    except Exception as exc:
        _record_log.debug("could not inject _run_id: %s", exc)


def _inject_reuse(tool_result, run_id: str, reuse: dict) -> None:
    """Make a cache-served result say so: ``reused``, ``reused_from``, ``computed_at``, ``_note``.

    ``_run_id`` is REPLACED by the reuse row's id (the tool's own, if any, named the original
    run). Added after the record is sealed, like ``_run_id``: these are transport keys outside
    ``result["data"]``, so the sealed output digest is unaffected. Best effort.
    """
    from ai_hydro.session.run_records import reuse_disclosure

    def apply(d: dict) -> None:
        prior_note = d.get("_note")
        d["_run_id"] = run_id
        d["reused"] = True
        d["reused_from"] = reuse.get("reused_from")
        d["computed_at"] = reuse.get("computed_at")
        d["origin_match"] = reuse.get("origin_match")
        note = reuse_disclosure(run_id, reuse)
        d["_note"] = f"{note} {prior_note}" if prior_note else note

    try:
        structured = getattr(tool_result, "structured_content", None)
        if isinstance(structured, dict) and set(structured) != {"result"} and not structured.get("error"):
            apply(structured)
        for block in getattr(tool_result, "content", None) or []:
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                continue
            try:
                parsed = _json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                if not parsed.get("error"):
                    apply(parsed)
                    block.text = _json.dumps(parsed, default=str)
                return
    except Exception as exc:
        _record_log.debug("could not inject reuse disclosure: %s", exc)


RECENT_FALLBACK_WARNING = (
    "No study was named or bound; {verb} most-recent study {sid} \u2014 name a session to be sure"
)


def _inject_session_warning(tool_result, session_id: str, read_only: bool = False) -> None:
    """Add a visible top-level ``session_warning`` to a successful dict result.

    Set when the call's session came from the recent-study guess (``recent_fallback``). Added after
    the run record is sealed, like ``_run_id``, so the sealed digest is unaffected. Best effort.
    """
    # "used" for a tool annotated readOnlyHint (it changes nothing); "wrote to" for a writer.
    text = RECENT_FALLBACK_WARNING.format(verb="used" if read_only else "wrote to", sid=session_id)
    try:
        structured = getattr(tool_result, "structured_content", None)
        if isinstance(structured, dict) and set(structured) != {"result"}:
            if not structured.get("error"):
                structured.setdefault("session_warning", text)
        for block in getattr(tool_result, "content", None) or []:
            raw = getattr(block, "text", None)
            if not isinstance(raw, str):
                continue
            try:
                parsed = _json.loads(raw)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                if not parsed.get("error") and "session_warning" not in parsed:
                    parsed["session_warning"] = text
                    block.text = _json.dumps(parsed, default=str)
                return
    except Exception as exc:
        _record_log.debug("could not inject session_warning: %s", exc)


WORKSPACE_SINGLE_STUDY_NOTE = "Used the only study in this workspace: {label} ({study_id})."
PROJECT_DEFAULT_NOTE = "Used the project's confirmed default study: {label} ({study_id})."


def _inject_top_level(tool_result, key: str, value) -> None:
    """Add ``key`` to a successful dict result, in the structured content and the text block.

    Never overwrites a value the tool set. Added after the run record is sealed, so the sealed
    digest is unaffected. Best effort; the result must still go out.
    """
    try:
        structured = getattr(tool_result, "structured_content", None)
        if isinstance(structured, dict) and set(structured) != {"result"}:
            if not structured.get("error"):
                structured.setdefault(key, value)
        for block in getattr(tool_result, "content", None) or []:
            raw = getattr(block, "text", None)
            if not isinstance(raw, str):
                continue
            try:
                parsed = _json.loads(raw)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                if not parsed.get("error") and key not in parsed:
                    parsed[key] = value
                    block.text = _json.dumps(parsed, default=str)
                return
    except Exception as exc:
        _record_log.debug("could not inject %s: %s", key, exc)


def _mcp_client_label(context) -> str | None:
    try:
        info = context.fastmcp_context.session.client_params.clientInfo
        return f"{info.name}/{info.version}" if info.version else str(info.name)
    except Exception:
        return None


_CANCEL_SEALS: set = set()


def _seal_cancelled_call(run_records, *, tool, arguments, capture, chat_id, started, mcp_client, run_context) -> None:
    """Seal the run record of a cancelled call as ``CANCELLED`` (fire and forget, never raises).

    Runs as its own task: an awaiting task inside a cancelled anyio scope is cancelled again at every
    await, so sealing in-line would be skipped. The record says the worker may still complete and write.
    """
    from ai_hydro.mcp.enforcement import _generate_unique_run_id
    from ai_hydro.core.cancel import ToolCancelled
    from ai_hydro.mcp.offload import run_off_loop

    envelope = {
        "error": True, "code": "CANCELLED", "cancelled": True,
        "message": ("call cancelled by the client; its worker may still complete and write rows or files "
                    "that this record cannot undo"),
    }

    async def seal():
        try:
            await run_off_loop(
                run_records.record_call,
                tool=tool, arguments=arguments, result=envelope,
                failure=ToolCancelled(envelope["message"]), capture=capture, chat_id=chat_id,
                id_factory=_generate_unique_run_id,
                duration_ms=(_time.monotonic() - started) * 1000.0,
                mcp_client=mcp_client, output_bytes=len(_json.dumps(envelope)), context=run_context,
                _track=False,
            )
        except BaseException as exc:                      # noqa: BLE001 - sealing must never raise
            _record_log.warning("could not seal cancelled call %s: %s", tool, exc)

    try:
        task = asyncio.get_running_loop().create_task(seal())
        _CANCEL_SEALS.add(task)
        task.add_done_callback(_CANCEL_SEALS.discard)
    except Exception as exc:
        _record_log.warning("could not schedule sealing of cancelled call %s: %s", tool, exc)


class RunRecordMiddleware(Middleware):
    """Attach a sealed ``aihydro.run/2`` record to every recorded tool call.

    A seal proves integrity, not origin. Insert-only rows protect against
    cooperating writers, not adversaries. A deleted sealed row is not
    detectable (no hash chain yet). A writer that pre-seals its own record
    (including run_python code or any same-user process) authors its own
    provenance, and this middleware does not overwrite an already-sealed row.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):  # type: ignore[override]
        try:
            from ai_hydro.session import run_records
        except Exception as exc:  # e.g. aihydro-core without aihydro_core.records
            _record_log.warning("run recording unavailable: %s", exc)
            return await call_next(context)

        message = context.message
        tool = getattr(message, "name", "") or ""
        if not run_records.is_recorded_tool(tool):
            run_records.count("exempt")
            return await call_next(context)

        arguments = dict(getattr(message, "arguments", None) or {})
        run_context = {
            "source": ACTIVE_CONTEXT_SOURCE.get(),
            "study_id": ACTIVE_STUDY_ID.get(),
            "client": ACTIVE_CONTEXT_CLIENT.get(),
        }
        from ai_hydro.mcp.enforcement import _generate_unique_run_id as _run_id_factory

        capture, token = run_records.begin_capture(
            tool=tool, arguments=arguments, chat_id=ACTIVE_CHAT_ID.get(),
            study_id=run_context["study_id"], id_factory=_run_id_factory)
        started = _time.monotonic()
        failure = None
        tool_result = None
        try:
            tool_result = await call_next(context)
        except Exception as exc:
            failure = exc
        except asyncio.CancelledError:
            # The client gave up. The worker may still be running and may already have written rows
            # (a cancel cannot undo them): seal what exists as a cancelled call, in a task the
            # cancelled scope cannot interrupt, then let the cancellation propagate.
            _seal_cancelled_call(
                run_records, tool=tool, arguments=arguments, capture=capture, chat_id=ACTIVE_CHAT_ID.get(),
                started=started, mcp_client=_mcp_client_label(context), run_context=run_context)
            raise
        finally:
            run_records.end_capture(token)

        record_error = None
        try:
            from ai_hydro.mcp.enforcement import _generate_unique_run_id

            result_dict, size = (None, None) if tool_result is None else _tool_result_dict(tool_result)
            if failure is not None:
                # An exception carrying an envelope (StructuredToolError, or a plain ToolError whose
                # text is the JSON error dict) seals that envelope, so a dict-error tool digests
                # identically to when it returned the dict (v2.3.7).
                from ai_hydro.mcp.errors import envelope_from_exception

                envelope = envelope_from_exception(failure)
                if isinstance(envelope, dict):
                    result_dict = dict(envelope)
                    size = len(_json.dumps(result_dict, default=str))
                else:
                    result_dict = {"error": True, "message": f"{type(failure).__name__}: {failure}"}
            # Sealing writes the run log (SQLite, up to a 30 s lock wait) and digests files: never
            # on the event loop, or one contended study stalls every call.
            from ai_hydro.mcp.offload import run_off_loop

            outcome = await run_off_loop(
                run_records.record_call,
                tool=tool,
                arguments=arguments,
                result=result_dict,
                failure=failure,
                capture=capture,
                chat_id=ACTIVE_CHAT_ID.get(),
                id_factory=_generate_unique_run_id,
                duration_ms=(_time.monotonic() - started) * 1000.0,
                mcp_client=_mcp_client_label(context),
                output_bytes=size,
                context=run_context,
                _track=False,
            )
            record_error = outcome.record_error
            if (failure is None and tool_result is not None and outcome.call_run_id
                    and isinstance(result_dict, dict) and not result_dict.get("error")):
                if outcome.reuse:
                    _inject_reuse(tool_result, outcome.call_run_id, outcome.reuse)
                elif "_run_id" not in result_dict:
                    _inject_run_id(tool_result, outcome.call_run_id)

        except Exception as exc:
            _record_log.warning("run recording failed for %s: %s", tool, exc)
            record_error = f"recording: {type(exc).__name__}: {exc}"

        if failure is not None:
            if record_error:   # an error call has no result to carry it; log, never inject
                _record_log.warning("run record problem on failed call %s: %s", tool, record_error)
            raise failure
        if record_error:
            _inject_record_error(tool_result, record_error)
        return tool_result


mcp.add_middleware(RunRecordMiddleware())


class ErrorHygieneMiddleware(Middleware):
    """Strip tracebacks and local paths from error dicts a tool built itself.

    Registered AFTER RunRecordMiddleware, so it runs inside it: the record seals the
    sanitized error, and no stack trace or absolute path reaches the model or the run log.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):  # type: ignore[override]
        tool_result = await call_next(context)
        from ai_hydro.mcp.error_hygiene import sanitize_tool_result
        sanitize_tool_result(tool_result)
        return tool_result


mcp.add_middleware(ErrorHygieneMiddleware())
