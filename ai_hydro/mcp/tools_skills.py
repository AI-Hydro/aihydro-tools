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
    List installed workflow skills (playbooks). See SKILL DISCOVERY in the
    system prompt for the mandatory pre-flight protocol. ``domain`` filters
    to one of: frequency-analysis, baseflow, modelling, interpretation,
    composition, teaching, general.
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
    Load a skill's full SKILL.md (frontmatter + body). The skill's steps and
    format contracts are binding — see SKILL DISCOVERY in the system prompt.
    name: exact match from list_skills().
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
    Persist a new SKILL.md (Agent Skills open format) to
    ~/.aihydro/skills/agent-created/. Use after completing a novel workflow
    worth reusing. name will be slugified; content is the markdown body.
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
