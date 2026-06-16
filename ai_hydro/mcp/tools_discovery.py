"""
Capability-discovery MCP tools.

Token-efficient alternative to dumping all 93 tool schemas in every system
prompt. The agent calls ``aihydro_describe_capability(domain)`` to get a
focused, 1-line-per-tool summary of just the tools relevant to a domain,
then references those tools by name without needing each full schema
preloaded into context.

This is the first step toward lazy tool exposure (Wave 1.5 will explore
dynamic registration via FastMCP's add_tool + tools/list_changed
notification).
"""
from __future__ import annotations

import logging

from ai_hydro.mcp.app import TOOL_TIERS, mcp

log = logging.getLogger("ai_hydro.mcp.discovery")


# Domain → matching predicate. Maintained here (not in app.py TOOL_TIERS)
# because domains are orthogonal to tiers and may overlap (a tool can fit
# multiple domains; tiers are exclusive).
_DOMAIN_PREFIXES: dict[str, tuple[str, ...]] = {
    "session":     ("start_session", "get_session", "clear_session", "archive_session",
                    "merge_session", "add_note", "export_session",
                    "aihydro_rebind_chat", "aihydro_chat_status"),
    "project":     ("start_project", "get_project", "add_session_to_project"),
    "watershed":   ("delineate_watershed", "delineation_", "merit_", "extract_geomorphic",
                    "compute_twi", "create_cn_grid", "compute_soil_loss_rusle",
                    "compute_design_hydrograph"),
    "streamflow":  ("fetch_streamflow", "separate_baseflow", "extract_hydrological",
                    "compute_flow_duration_curve", "compute_flood_frequency"),
    "forcing":     ("fetch_forcing", "compute_drought_index"),
    "camels":      ("fetch_camels",),
    "modelling":   ("train_hydro_model", "get_training_status", "get_model_results"),
    "maps":        ("map_", "show_on_map", "gee."),
    "preview":     ("show_html_preview", "preview_"),
    "course":      ("course_",),
    "citations":   ("lookup_citation", "search_literature", "index_literature",
                    "get_citation_by_doi", "list_cached_citations"),
    "claims":      ("add_claim", "list_claims", "promote_claim", "update_claim",
                    "add_assumption", "list_assumptions", "draft_claim",
                    "register_research_plan"),
    "validators":  ("check_water_balance", "check_temporal", "check_unit",
                    "audit_interpretation",
                    "check_record_length", "check_usgs_qualification_codes",
                    "check_regulated_basin", "check_stationarity"),
    "skills":      ("list_skills", "load_skill", "save_skill"),
    "knowledge":   ("get_variable", "list_known_variables", "get_metric",
                    "list_known_metrics", "get_dataset_info", "list_known_datasets",
                    "get_equation"),
    "workflows":   ("list_available_workflows", "get_workflow_manifest"),
    "discovery":   ("aihydro_describe_capability", "describe_tool", "describe_tools",
                    "list_available_tools"),
    "execution":   ("run_python", "list_relevant_clis", "get_library_reference",
                    "cancel_job", "list_jobs", "wait_for_job"),
    "persona":     ("get_researcher", "update_researcher", "log_researcher",
                    "write_research_interpretation"),
    "ledger":      ("add_journal", "search_experiments",
                    "add_journal_entry",   # alias present in some versions
                    "check_registry_staleness", "list_registry_claims"),
    "experiments": ("define_experiment", "run_experiment", "get_experiment_table"),
    "skeptic":     ("run_skeptic",),
    "literature":  ("index_passages", "search_passages_tool", "resolve_passage"),
    # Feature registry (C2 — multi-geometry)
    "features":    ("register_feature", "list_features", "set_active_feature",
                    "bind_map_to_claim"),
    # Wave 2.5 — aihydro-data tools surfaced via the `aihydro.tools` entry-point group.
    # Longest-prefix match means these win over any generic prefixes.
    "data_fetch":  ("data_fetch", "data_batch", "data_list_products",
                    "data_describe_product", "data_validate_request",
                    "data_get_cache_status", "data_invalidate_cache",
                    "data_doctor", "data_help",
                    "data_fetch_background", "get_data_fetch_result"),
    # v0.2.0 — spectral index tools (TorchGeo cherry-pick Day 5)
    "analysis":    ("compute_spectral_index", "list_spectral_indices"),
}


@mcp.tool()
async def aihydro_describe_capability(domain: str | None = None) -> dict:
    """
    Return a focused 1-line-per-tool summary of tools relevant to a domain.

    Use this FIRST when you need to know what's available for a task —
    it's far cheaper than scanning every tool description in the system
    prompt. Once you've identified the tool you need, call it directly.

    Parameters
    ----------
    domain : str, optional
        One of: session, project, watershed, streamflow, forcing, camels,
        modelling, maps, preview, course, citations, claims, validators,
        skills, knowledge, workflows, discovery, execution, persona, ledger,
        data_fetch, analysis, features.
        If omitted, returns the list of available domains with tool counts.

    Returns
    -------
    dict with `domain`, `tools` (list of {name, tier, summary}), and
    `count`. Or, if no domain given: `domains` (list of {name, tool_count}).
    """
    tools = await mcp.list_tools()
    tool_index = {t.name: t for t in tools}

    if not domain:
        # Top-level: return domain inventory
        domains_out = []
        for d, prefixes in _DOMAIN_PREFIXES.items():
            matches = [n for n in tool_index if any(n.startswith(p) for p in prefixes)]
            if matches:
                domains_out.append({"domain": d, "tool_count": len(matches)})
        return {
            "domains": sorted(domains_out, key=lambda x: -x["tool_count"]),
            "usage": "Call again with domain=<name> for tool-level detail.",
        }

    d = domain.strip().lower()
    if d not in _DOMAIN_PREFIXES:
        return {
            "error": True,
            "message": f"Unknown domain '{domain}'.",
            "available_domains": sorted(_DOMAIN_PREFIXES.keys()),
        }

    prefixes = _DOMAIN_PREFIXES[d]
    matched = []
    for name, tool in tool_index.items():
        if not any(name.startswith(p) for p in prefixes):
            continue
        # First non-empty line of the docstring as the summary
        desc = (tool.description or "").strip()
        summary = next((ln.strip() for ln in desc.splitlines() if ln.strip()), "")
        # Cap summary length so the response stays tight
        if len(summary) > 140:
            summary = summary[:137] + "..."
        matched.append({
            "name": name,
            "tier": TOOL_TIERS.get(name),
            "summary": summary,
        })
    matched.sort(key=lambda x: (x["tier"] or 99, x["name"]))
    return {"domain": d, "count": len(matched), "tools": matched}


# ---------------------------------------------------------------------------
# describe_tool / describe_tools — on-demand full-schema fetch
#
# Under progressive disclosure, less-common tools are injected into the system
# prompt as a one-line summary only (no parameter schema). Before the agent
# calls such a tool for the first time it must fetch the exact parameters with
# describe_tool(name). This returns the full inputSchema, per-parameter docs,
# and a copy-pasteable worked example — everything a weak model needs to make
# a correct call without guessing parameter names.
# ---------------------------------------------------------------------------

def _example_value(name: str, spec: dict):
    """Best-effort placeholder value for a parameter, for the worked example."""
    if "default" in spec and spec["default"] is not None:
        return spec["default"]
    if "enum" in spec and spec["enum"]:
        return spec["enum"][0]
    t = spec.get("type")
    if isinstance(t, list):  # e.g. ["string", "null"]
        t = next((x for x in t if x != "null"), "string")
    # A few name-based hints for common AI-Hydro params
    lname = name.lower()
    if "session_id" in lname:
        return "<session_id>"
    if lname in ("start", "end") or lname.endswith("_date"):
        return "2024-01-01"
    if "lat" in lname:
        return 28.22
    if "lon" in lname:
        return 76.77
    if t == "string":
        return f"<{name}>"
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return True
    if t == "array":
        return []
    if t == "object":
        return {}
    return f"<{name}>"


def _describe_one(tool) -> dict:
    """Build the full descriptor for a single MCP tool object."""
    mt = tool.to_mcp_tool() if hasattr(tool, "to_mcp_tool") else tool
    schema = getattr(mt, "inputSchema", None) or {}
    props: dict = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])

    params = []
    for pname, spec in props.items():
        spec = spec or {}
        ptype = spec.get("type")
        if isinstance(ptype, list):
            ptype = "|".join(ptype)
        params.append({
            "name": pname,
            "type": ptype or "any",
            "required": pname in required,
            "description": (spec.get("description") or "").strip(),
            "default": spec.get("default"),
            "enum": spec.get("enum"),
        })
    # Required params first, then alphabetical
    params.sort(key=lambda p: (not p["required"], p["name"]))

    # Worked example: all required params + any with a non-null default
    example_args = {}
    for pname, spec in props.items():
        spec = spec or {}
        if pname in required or (spec.get("default") is not None):
            example_args[pname] = _example_value(pname, spec)

    return {
        "name": mt.name,
        "description": (mt.description or "").strip(),
        "tier": TOOL_TIERS.get(mt.name),
        "input_schema": schema,
        "parameters": params,
        "required": sorted(required),
        "example_call": {"tool": mt.name, "arguments": example_args},
    }


@mcp.tool()
async def describe_tool(name: str) -> dict:
    """
    Fetch the FULL parameter schema for a single tool, plus a worked example.

    Call this BEFORE the first time you use any tool that was shown to you by
    name only (summary-level tools, listed under their domain). It returns the
    exact parameter names, types, which are required, and a copy-pasteable
    example call — so you never have to guess parameter names.

    Parameters
    ----------
    name : str
        Exact tool name (e.g. "compute_twi"). Case-insensitive match is
        attempted as a fallback.

    Returns
    -------
    dict with `name`, `description`, `tier`, `input_schema` (full JSON schema),
    `parameters` (per-param docs), `required`, and `example_call`.
    On an unknown name: `error`, `message`, and `did_you_mean` (closest names).
    """
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}

    tool = by_name.get(name)
    if tool is None:
        # Case-insensitive fallback
        lower = {t.name.lower(): t for t in tools}
        tool = lower.get(name.strip().lower())

    if tool is None:
        import difflib
        suggestions = difflib.get_close_matches(name, list(by_name.keys()), n=5, cutoff=0.4)
        return {
            "error": True,
            "message": f"Unknown tool '{name}'.",
            "did_you_mean": suggestions,
            "hint": "Use aihydro_describe_capability(domain) to browse tools, "
                    "or list_available_tools() for the full name list.",
        }

    return _describe_one(tool)


@mcp.tool()
async def describe_tools(names: list[str]) -> dict:
    """
    Fetch full parameter schemas for several tools at once (batch describe_tool).

    Use this when you plan to chain multiple summary-level tools — fetch all
    their schemas in one call instead of one round-trip each.

    Parameters
    ----------
    names : list[str]
        Tool names to describe.

    Returns
    -------
    dict with `tools` (list of descriptors, same shape as describe_tool) and
    `unknown` (names that could not be resolved, with suggestions).
    """
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}
    lower = {t.name.lower(): t for t in tools}

    out, unknown = [], []
    import difflib
    for raw in names or []:
        tool = by_name.get(raw) or lower.get(str(raw).strip().lower())
        if tool is None:
            unknown.append({
                "name": raw,
                "did_you_mean": difflib.get_close_matches(
                    raw, list(by_name.keys()), n=3, cutoff=0.4),
            })
        else:
            out.append(_describe_one(tool))
    return {"count": len(out), "tools": out, "unknown": unknown}
