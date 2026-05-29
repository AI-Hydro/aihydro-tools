"""
Documentation and version utilities for the MCP server.

Generates .aihydrorules/tools.md from the live tool registry and
provides version introspection helpers.

Also exposes a public-site generator (`generate_tool_reference`) that emits
the full mkdocs Tool Reference (all registered tools grouped by tier/domain,
with parameters and a worked example each) directly from the live registry —
so the published reference can never drift from the actual tool surface.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("ai_hydro.mcp")

# Human-readable labels for the tier system (see app.py TOOL_TIERS).
_TIER_LABELS = {
    1: "Tier 1 — Core (always in-context)",
    2: "Tier 2 — Extended",
    3: "Tier 3 — Specialist",
}

# Friendly headings for domain codes attached via _meta.
_DOMAIN_LABELS = {
    "watershed": "Watershed & Terrain",
    "streamflow": "Streamflow",
    "forcing": "Meteorological Forcing",
    "camels": "CAMELS Attributes",
    "data_fetch": "Data Fetch (aihydro-data)",
    "analysis": "Analysis",
    "modelling": "Modelling",
    "maps": "Map Panel",
    "session": "Session Management",
    "project": "Projects",
    "citations": "Citations & Literature",
    "persona": "Researcher Persona",
    "ledger": "Research Ledger",
    "claims": "Claims & Provenance",
    "validators": "Consistency Validators",
    "skills": "Skills",
    "knowledge": "Knowledge & Reference",
    "discovery": "Discovery",
    "workflows": "Workflows",
    "course": "Course / Teaching",
    "preview": "HTML Preview",
    "execution": "Execution",
    "general": "General",
}


def _tool_schema(tool) -> dict:
    """Return a tool's JSON input schema, robust across FastMCP versions.

    FunctionTool has no ``.inputSchema``; the MCP-facing schema is produced by
    ``to_mcp_tool()``. Fall back to any direct attribute for older registries.
    """
    try:
        mcp_tool = tool.to_mcp_tool()
        schema = getattr(mcp_tool, "inputSchema", None)
        if schema:
            return schema
    except Exception:
        pass
    return getattr(tool, "inputSchema", {}) or {}


def _short_desc(tool) -> str:
    """First paragraph of the docstring, collapsed to a single line."""
    desc = (tool.description or "").strip()
    return desc.split("\n\n")[0].replace("\n", " ").strip()


def _example_value(param: str, info: dict):
    """Produce a plausible example value for a worked call snippet."""
    ptype = info.get("type", "string")
    if "enum" in info and info["enum"]:
        return info["enum"][0]
    if "default" in info and info["default"] is not None:
        return info["default"]
    name = param.lower()
    if "gauge" in name:
        return "01646500"
    if name in ("lat", "latitude"):
        return 28.22
    if name in ("lon", "lng", "longitude"):
        return 76.77
    if "session" in name:
        return "01646500"
    if "start" in name and ("date" in name or ptype == "string"):
        return "2020-01-01"
    if "end" in name and ("date" in name or ptype == "string"):
        return "2020-12-31"
    if "index" in name:
        return "NDWI"
    return {
        "string": "...",
        "integer": 0,
        "number": 0.0,
        "boolean": True,
        "array": [],
        "object": {},
    }.get(ptype, "...")


def _get_version() -> str:
    """Return the installed ai-hydro package version."""
    try:
        from importlib.metadata import version
        return version("aihydro-tools")
    except Exception:
        return "unknown"


def _list_tools_sync() -> list:
    """Return the list of registered MCP tools (sync wrapper)."""
    import asyncio
    from ai_hydro.mcp.app import mcp
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return []
        return asyncio.run(mcp.list_tools())
    except Exception:
        return []


def _write_tools_md() -> Path:
    """
    Write .aihydrorules/tools.md from the live MCP tool registry.

    This is the single source of truth for tool documentation.
    Community-added tools appear here automatically on next server start
    or write_research_interpretation call — no manual edits needed.
    """
    # Repo root: ai_hydro/mcp/tools_docs.py → up 4 levels
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    rules_dir = repo_root / ".aihydrorules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    tools_md = rules_dir / "tools.md"

    tools = _list_tools_sync()
    if not tools:
        return tools_md

    lines = [
        "# AI-Hydro MCP Tools",
        "",
        "> Auto-generated from the live MCP server — do not edit manually.",
        "> Run `write_research_interpretation` or restart the server to refresh.",
        "",
        f"**{len(tools)} tools registered**",
        "",
        "---",
        "",
    ]

    for tool in sorted(tools, key=lambda t: t.name):
        short_desc = _short_desc(tool)

        lines.append(f"### `{tool.name}`")
        if short_desc:
            lines.append(short_desc)

        # Parameters from JSON schema
        schema = _tool_schema(tool)
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        if props:
            lines.append("")
            lines.append("**Parameters**")
            for param, info in props.items():
                ptype = info.get("type", "any")
                pdesc = info.get("description", "")
                req_marker = "" if param in required else " *(optional)*"
                param_line = f"- `{param}` ({ptype}){req_marker}"
                if pdesc:
                    param_line += f" — {pdesc}"
                lines.append(param_line)

        lines.append("")

    tools_md.write_text("\n".join(lines))
    log.info("tools.md written: %d tools -> %s", len(tools), tools_md)
    return tools_md


# ---------------------------------------------------------------------------
# Public-site Tool Reference generator (mkdocs)
# ---------------------------------------------------------------------------

def _render_param_table(props: dict, required: set) -> list[str]:
    """Render a markdown parameter table for one tool."""
    if not props:
        return ["_No parameters._"]
    out = [
        "| Parameter | Type | Required | Description |",
        "|-----------|------|----------|-------------|",
    ]
    for param, info in props.items():
        ptype = info.get("type", "any")
        if "enum" in info:
            ptype = "enum: " + ", ".join(f"`{e}`" for e in info["enum"])
        pdesc = (info.get("description", "") or "").replace("\n", " ").replace("|", "\\|").strip()
        req = "yes" if param in required else "—"
        out.append(f"| `{param}` | {ptype} | {req} | {pdesc} |")
    return out


def _render_example(tool_name: str, props: dict, required: set) -> list[str]:
    """Render a worked example call using required (+ first optional) params."""
    args = {}
    # Preserve declared parameter order for stable, churn-free diffs.
    for param in props:
        if param in required:
            args[param] = _example_value(param, props[param])
    # If nothing required, show the first couple of params as a hint.
    if not args:
        for param in list(props)[:2]:
            args[param] = _example_value(param, props[param])
    arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return [
        "```python",
        f"{tool_name}({arg_str})",
        "```",
    ]


def generate_tool_reference(target: Path | str | None = None) -> Path:
    """Emit the full mkdocs Tool Reference from the live registry.

    Writes a single ``reference.md`` containing every registered tool grouped
    by tier then domain, each with a one-line description, a parameter table,
    and a worked example call. The published reference is therefore generated
    from the same source of truth the agent sees — it cannot drift.

    Returns the path written.
    """
    tools = _list_tools_sync()
    if not tools:
        raise RuntimeError("no tools registered — cannot generate reference")

    if target is None:
        target = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "tools" / "reference.md"
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Group: tier -> domain -> [tools]
    grouped: dict[int, dict[str, list]] = {}
    for tool in tools:
        meta = getattr(tool, "meta", None) or {}
        tier = meta.get("tier", 3)
        domain = meta.get("domain", "general")
        grouped.setdefault(tier, {}).setdefault(domain, []).append(tool)

    n_hot = sum(1 for t in tools if (getattr(t, "meta", None) or {}).get("hot"))
    version = _get_version()

    lines = [
        "---",
        "description: Complete auto-generated reference for all AI-Hydro MCP "
        "tools, grouped by tier and domain, with parameters and worked examples.",
        "---",
        "",
        "# Complete Tool Reference",
        "",
        "<!-- AUTO-GENERATED by ai_hydro.mcp.tools_docs.generate_tool_reference — "
        "do not edit by hand. Run `python -m ai_hydro.mcp.tools_docs` to refresh. -->",
        "",
        f"This page lists **all {len(tools)} tools** registered on "
        f"aihydro-tools `v{version}`, exactly as the agent sees them. "
        f"Tools are grouped by **tier** (how readily they are surfaced to the "
        f"agent) and **domain**.",
        "",
        f"- **{len(tools)} tools** total — **{n_hot} hot** (full schema always "
        "in-context), the rest fetched on demand via `describe_tool(name)`.",
        "- Call `list_available_tools()` at runtime for the live count on your "
        "installation; community plugins add more.",
        "",
        "---",
        "",
    ]

    for tier in sorted(grouped):
        lines.append(f"## {_TIER_LABELS.get(tier, f'Tier {tier}')}")
        lines.append("")
        for domain in sorted(grouped[tier]):
            domain_tools = sorted(grouped[tier][domain], key=lambda t: t.name)
            lines.append(f"### {_DOMAIN_LABELS.get(domain, domain.title())}")
            lines.append("")
            for tool in domain_tools:
                meta = getattr(tool, "meta", None) or {}
                hot = " · :material-fire: hot" if meta.get("hot") else ""
                schema = _tool_schema(tool)
                props = schema.get("properties", {})
                required = set(schema.get("required", []))

                lines.append(f"#### `{tool.name}`{hot}")
                lines.append("")
                short = _short_desc(tool)
                if short:
                    lines.append(short)
                    lines.append("")
                lines.extend(_render_param_table(props, required))
                lines.append("")
                lines.extend(_render_example(tool.name, props, required))
                lines.append("")
        lines.append("---")
        lines.append("")

    target.write_text("\n".join(lines))
    log.info("tool reference written: %d tools -> %s", len(tools), target)
    return target


if __name__ == "__main__":  # pragma: no cover
    import ai_hydro.mcp  # noqa: F401 — trigger tool registration
    path = generate_tool_reference()
    print(f"Wrote {path}")
