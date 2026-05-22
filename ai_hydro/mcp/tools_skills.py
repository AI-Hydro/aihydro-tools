"""
Skills MCP tools — list and load workflow playbooks.

list_skills: enumerate skills across all three tiers (built-in / plugin / workspace).
load_skill:  load the full content of a named skill.
"""
from __future__ import annotations

import logging

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp")


@mcp.tool()
def list_skills(
    domain: str | None = None,
    workspace_dir: str | None = None,
) -> dict:
    """
    List all available workflow skills across built-in, plugin, and workspace tiers.

    Skills are workflow playbooks for judgment-heavy tasks — they compose MCP
    tools with domain knowledge, decision logic, and methods-section templates.
    Load a skill before multi-step analyses to get the recommended approach for
    the researcher's basin type, record length, and research goal.

    Four tiers (later overrides earlier on name collision):
      1. Built-in:       ai_hydro/skills/ (ships with aihydro-tools)
      2. Plugin:         aihydro.skills entry-point group (community packages)
      3. User-installed: ~/.aihydro/skills/{marketplace,agent-created,manual}/
      4. Workspace:      <workspace>/.aihydrorules/skills/ (researcher-local)

    Parameters
    ----------
    domain : str, optional
        Filter by domain (e.g. 'frequency-analysis', 'modelling', 'baseflow',
        'interpretation', 'composition'). Omit to list all domains.
    workspace_dir : str, optional
        Workspace directory for workspace-tier skills.

    Returns
    -------
    dict with skills list (name, description, domain, when_to_use, tools_used)
    and n_skills.
    """
    try:
        from ai_hydro.skills.registry import list_skills as _list
        skills = _list(domain=domain, workspace_dir=workspace_dir)
        return {
            "skills": skills,
            "n_skills": len(skills),
            "domain_filter": domain,
            "_note": (
                "Load a skill with load_skill(name) before multi-step analyses. "
                "User-installed skills in ~/.aihydro/skills/ override built-ins. "
                "Workspace skills in <workspace>/.aihydrorules/skills/ override all."
            ),
        }
    except Exception as e:
        log.error("list_skills failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def load_skill(
    name: str,
    workspace_dir: str | None = None,
) -> dict:
    """
    Load the full content of a workflow skill by name.

    Returns the complete SKILL.md content (frontmatter metadata + body).
    Read the skill fully before starting the workflow — it contains parameter
    decision guides, failure modes, and methods-section templates.

    Parameters
    ----------
    name : str
        Skill name as returned by list_skills.
    workspace_dir : str, optional
        Workspace directory for workspace-tier skills.

    Returns
    -------
    dict with name, description, domain, when_to_use, tools_used, citations,
    body (full markdown content), and path.
    """
    try:
        from ai_hydro.skills.registry import load_skill as _load
        skill = _load(name=name, workspace_dir=workspace_dir)
        if skill is None:
            from ai_hydro.skills.registry import list_skills as _list
            all_names = [s["name"] for s in _list(workspace_dir=workspace_dir)]
            return {
                "error": True,
                "code": "NOT_FOUND",
                "message": f"No skill named '{name}'.",
                "available_skills": all_names,
            }
        return skill
    except Exception as e:
        log.error("load_skill failed: %s", e)
        return _tool_error_to_dict(e)
