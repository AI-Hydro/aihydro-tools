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
    "ACTIVE_CONTEXT_SOURCE", "ACTIVE_CONTEXT_CLIENT", "CONTEXT_META_KEY",
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
ACTIVE_CONTEXT_CLIENT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ACTIVE_CONTEXT_CLIENT", default=None
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


def _preserve_callable_registration(method):
    """Register FastMCP components but leave ordinary callables in modules.

    FastMCP 2.7+ decorators replace decorated functions with component wrapper
    objects. Keeping the original function is useful for internal composition
    and direct unit tests; registration still goes through FastMCP unchanged.
    """
    def decorator_factory(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            function = args[0]
            method(function)
            return function

        register = method(*args, **kwargs)

        def decorate(function):
            register(function)
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
        client = _str_or_none(meta_ctx.get("client"))
        used_meta = any((chat_id, workspace, study_id, client))
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
            (ACTIVE_CONTEXT_SOURCE, ACTIVE_CONTEXT_SOURCE.set(source)),
            (ACTIVE_CONTEXT_CLIENT, ACTIVE_CONTEXT_CLIENT.set(client)),
        )
        try:
            return await call_next(context)
        finally:
            for var, token in reversed(tokens):
                var.reset(token)


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


def _mcp_client_label(context) -> str | None:
    try:
        info = context.fastmcp_context.session.client_params.clientInfo
        return f"{info.name}/{info.version}" if info.version else str(info.name)
    except Exception:
        return None


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
        capture, token = run_records.begin_capture()
        started = _time.monotonic()
        failure = None
        tool_result = None
        try:
            tool_result = await call_next(context)
        except Exception as exc:
            failure = exc
        finally:
            run_records.end_capture(token)

        record_error = None
        try:
            from ai_hydro.mcp.enforcement import _generate_unique_run_id

            result_dict, size = (None, None) if tool_result is None else _tool_result_dict(tool_result)
            if failure is not None:
                result_dict = {"error": True, "message": f"{type(failure).__name__}: {failure}"}
            outcome = run_records.record_call(
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
            )
            record_error = outcome.record_error
            if (failure is None and tool_result is not None and outcome.call_run_id
                    and isinstance(result_dict, dict) and not result_dict.get("error")
                    and "_run_id" not in result_dict):
                _inject_run_id(tool_result, outcome.call_run_id)
        except Exception as exc:
            _record_log.warning("run recording failed for %s: %s", tool, exc)
            record_error = f"recording: {type(exc).__name__}: {exc}"

        if failure is not None:
            raise failure
        if record_error:
            _inject_record_error(tool_result, record_error)
        return tool_result


mcp.add_middleware(RunRecordMiddleware())
