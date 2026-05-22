"""
Skills registry — unified discovery from ~/.aihydro/skills/.

All skills live on disk at ~/.aihydro/skills/ in three sub-directories:
  - marketplace/   installed from the GitHub-backed marketplace UI
  - agent-created/ saved by the agent via save_skill() MCP tool
  - manual/        added by the user via the "Add Skill" panel form

Optional workspace override:
  - <workspace>/.aihydrorules/skills/  (researcher-local, highest priority)

The VS Code extension marketplace is the distribution channel.  The Python
MCP server (list_skills / load_skill / save_skill) is the single discovery
and authoring engine.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("ai_hydro.skills")

_USER_SKILLS_DIR = Path.home() / ".aihydro" / "skills"
_SOURCES = ("marketplace", "agent-created", "manual")


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a SKILL.md file.  Returns (meta, body)."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}, content
    try:
        import yaml
        meta = yaml.safe_load(match.group(1)) or {}
    except Exception:
        meta = {}
    body = content[match.end():]
    return meta, body


def _lint_skill(meta: dict) -> list[str]:
    """Validate skill metadata.  Returns list of error messages (empty = OK)."""
    errors: list[str] = []
    if not meta.get("name"):
        errors.append("Missing 'name' in frontmatter")
    if not meta.get("description") or not str(meta["description"]).strip():
        errors.append("Empty or missing 'description'")
    if not meta.get("when_to_use") or not str(meta["when_to_use"]).strip():
        errors.append("Empty or missing 'when_to_use'")
    if not meta.get("domain"):
        errors.append("Missing 'domain'")
    return errors


# ---------------------------------------------------------------------------
# Directory scanner
# ---------------------------------------------------------------------------

def _load_skills_from_dir(directory: Path, source: str = "unknown") -> list[dict]:
    """Load all SKILL.md files from *directory* tree.  Skips invalid ones."""
    skills: list[dict] = []
    if not directory.is_dir():
        return skills
    for md_path in sorted(directory.rglob("SKILL.md")):
        try:
            content = md_path.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(content)
            errors = _lint_skill(meta)
            if errors:
                log.warning("Skill at %s failed lint: %s — skipping.",
                            md_path, "; ".join(errors))
                continue
            skills.append({
                "name": meta["name"],
                "description": meta.get("description", "").strip(),
                "domain": meta.get("domain", "general"),
                "when_to_use": meta.get("when_to_use", "").strip(),
                "tools_used": meta.get("tools_used", []),
                "tags": meta.get("tags", []),
                "citations": meta.get("citations", []),
                "source": source,
                "path": str(md_path),
                "_meta": meta,
            })
        except Exception as exc:
            log.warning("Failed to load skill from %s: %s", md_path, exc)
    return skills


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_skills(
    domain: str | None = None,
    workspace_dir: str | None = None,
) -> list[dict]:
    """
    Return all installed skills from ~/.aihydro/skills/ (+ workspace).

    Later sources override earlier on name collision:
      marketplace → agent-created → manual → workspace
    """
    by_name: dict[str, dict] = {}

    for src in _SOURCES:
        for skill in _load_skills_from_dir(_USER_SKILLS_DIR / src, source=src):
            by_name[skill["name"]] = skill

    if workspace_dir:
        ws_dir = Path(workspace_dir) / ".aihydrorules" / "skills"
        for skill in _load_skills_from_dir(ws_dir, source="workspace"):
            by_name[skill["name"]] = skill

    skills = list(by_name.values())
    if domain:
        skills = [s for s in skills if s.get("domain") == domain]
    return sorted(skills, key=lambda s: (s.get("domain", ""), s["name"]))


def load_skill(name: str, workspace_dir: str | None = None) -> dict | None:
    """Load the full SKILL.md content for a skill by name."""
    for skill in list_skills(workspace_dir=workspace_dir):
        if skill["name"] == name:
            try:
                body = Path(skill["path"]).read_text(encoding="utf-8")
                return {**skill, "body": body}
            except Exception as exc:
                log.error("Failed to read skill %s: %s", name, exc)
                return None
    return None


def save_skill(
    skill_id: str,
    name: str,
    description: str,
    content: str,
    domain: str = "general",
    when_to_use: str = "",
    tags: list[str] | None = None,
    tools_used: list[str] | None = None,
) -> dict:
    """
    Save a skill to ~/.aihydro/skills/agent-created/<skill_id>/SKILL.md.

    Builds YAML frontmatter from the arguments and writes the file.
    Returns {"success": True, "path": ..., "skill_id": ...} on success.
    """
    import textwrap

    skill_dir = _USER_SKILLS_DIR / "agent-created" / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"

    tags_str = tags or []
    tools_str = tools_used or []

    frontmatter = textwrap.dedent(f"""\
        ---
        name: {skill_id}
        description: {description}
        when_to_use: {when_to_use or description}
        domain: {domain}
        tools_used:
        {chr(10).join(f'  - {t}' for t in tools_str) if tools_str else '  []'}
        tags:
        {chr(10).join(f'  - {t}' for t in tags_str) if tags_str else '  []'}
        ---
    """)

    full_content = frontmatter + "\n" + content
    skill_path.write_text(full_content, encoding="utf-8")
    log.info("Saved agent-created skill %s to %s", skill_id, skill_path)

    return {
        "success": True,
        "skill_id": skill_id,
        "path": str(skill_path),
        "source": "agent-created",
    }
