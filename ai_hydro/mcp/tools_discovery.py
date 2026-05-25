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
                    "merge_session", "add_note", "export_session"),
    "project":     ("start_project", "get_project", "add_session_to_project"),
    "watershed":   ("delineate_watershed", "merit_", "extract_geomorphic", "compute_twi",
                    "create_cn_grid"),
    "streamflow":  ("fetch_streamflow", "separate_baseflow", "extract_hydrological"),
    "forcing":     ("fetch_forcing",),
    "camels":      ("fetch_camels",),
    "modelling":   ("train_hydro_model", "get_training_status", "get_model_results"),
    "maps":        ("map_", "show_on_map", "gee."),
    "preview":     ("show_html_preview", "preview_"),
    "course":      ("course_",),
    "citations":   ("lookup_citation", "search_literature", "index_literature"),
    "claims":      ("add_claim", "list_claims", "promote_claim", "update_claim",
                    "add_assumption", "list_assumptions", "draft_claim"),
    "validators":  ("check_water_balance", "check_temporal", "check_unit"),
    "skills":      ("list_skills", "load_skill", "save_skill"),
    "knowledge":   ("get_variable", "list_known_variables", "get_metric",
                    "list_known_metrics", "get_dataset_info", "list_known_datasets",
                    "get_equation"),
    "workflows":   ("list_available_workflows", "get_workflow_manifest"),
    "execution":   ("run_python", "list_relevant_clis", "get_library_reference"),
    "persona":     ("get_researcher", "update_researcher", "log_researcher"),
    "ledger":      ("add_journal", "search_experiments"),
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
        skills, knowledge, workflows, execution, persona, ledger.
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
