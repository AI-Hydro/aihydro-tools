"""
Skills MCP tools — list, load, and save workflow playbooks.

All skills live at ~/.aihydro/skills/ in three sub-directories:
  marketplace/    — installed from the GitHub Skills marketplace
  agent-created/  — saved by the agent via save_skill()
  manual/         — added by the user via the VS Code panel

Optional workspace override: <workspace>/.aihydrorules/skills/
"""
from __future__ import annotations

import logging
import re

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp")


@mcp.tool()
def list_skills(
    domain: str | None = None,
    workspace_dir: str | None = None,
) -> dict:
    """
    List all installed workflow skills.

    Skills are workflow playbooks that guide multi-step hydrological analyses.
    They are installed from the AI-Hydro Skills marketplace, created by the
    agent via save_skill(), or added manually by the user.

    Use list_skills() at the start of a conversation to see what workflows
    are available, then load_skill(name) to get the full instructions.

    Parameters
    ----------
    domain : str, optional
        Filter by domain: 'frequency-analysis', 'baseflow', 'modelling',
        'interpretation', 'composition', or 'general'.
    workspace_dir : str, optional
        Workspace path for workspace-local skills.

    Returns
    -------
    dict with skills list and count.
    """
    try:
        from ai_hydro.skills.registry import list_skills as _list
        skills = _list(domain=domain, workspace_dir=workspace_dir)
        return {
            "skills": skills,
            "n_skills": len(skills),
            "domain_filter": domain,
            "_note": (
                "Use load_skill(name) to get the full workflow instructions. "
                "Skills are stored at ~/.aihydro/skills/. "
                "Use save_skill() to create new skills from completed workflows."
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

    Returns the complete SKILL.md content (frontmatter + body).
    Read the skill fully before starting the workflow — it contains
    parameter guides, interpretation thresholds, and code examples.

    Parameters
    ----------
    name : str
        Skill name as returned by list_skills().
    workspace_dir : str, optional
        Workspace path for workspace-local skills.
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


@mcp.tool()
def save_skill(
    name: str,
    description: str,
    content: str,
    domain: str = "general",
    when_to_use: str = "",
    tags: list[str] | None = None,
    tools_used: list[str] | None = None,
) -> dict:
    """
    Save a reusable workflow skill for future conversations.

    Call this when you have completed a novel multi-step hydrological
    analysis and want to capture the workflow as a reusable skill.
    The skill will be saved to ~/.aihydro/skills/agent-created/ and
    will appear in list_skills() and the VS Code Skills panel immediately.

    The saved SKILL.md follows the Agent Skills open standard format
    and can be shared via the AI-Hydro Skills marketplace.

    Parameters
    ----------
    name : str
        Human-readable skill name (e.g. "Drought Index Analysis").
        Will be slugified for the file path.
    description : str
        One-sentence description of what this skill does.
    content : str
        Full markdown body of the skill (everything after the frontmatter).
        Should include numbered steps, code examples, interpretation guides.
    domain : str
        One of: frequency-analysis, baseflow, modelling, interpretation,
        composition, general. Default 'general'.
    when_to_use : str, optional
        When should the agent apply this skill? Be specific about trigger
        phrases and contexts.  Defaults to the description.
    tags : list[str], optional
        Keywords for discovery and filtering.
    tools_used : list[str], optional
        AI-Hydro MCP tools used in this workflow.
    """
    try:
        from ai_hydro.skills.registry import save_skill as _save
        skill_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return _save(
            skill_id=skill_id,
            name=name,
            description=description,
            content=content,
            domain=domain,
            when_to_use=when_to_use,
            tags=tags,
            tools_used=tools_used,
        )
    except Exception as e:
        log.error("save_skill failed: %s", e)
        return _tool_error_to_dict(e)
