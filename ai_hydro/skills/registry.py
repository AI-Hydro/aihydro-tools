"""
Skills registry — four-tier discovery for workflow playbooks.

Tier 1 (built-in):      ai_hydro/skills/**/*.md
Tier 2 (plugin):         aihydro.skills entry-point group
Tier 3 (user-installed): ~/.aihydro/skills/{marketplace,agent-created,manual}/**/*.md
Tier 4 (workspace):      <workspace>/.aihydrorules/skills/**/*.md

Later tiers override earlier when names collide.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("ai_hydro.skills")

_SKILLS_DIR = Path(__file__).parent


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a SKILL.md file. Returns (meta, body)."""
    import re
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


def _lint_skill(meta: dict, path: Path) -> list[str]:
    """Check skill metadata for quality bar. Returns list of error messages."""
    errors = []
    if not meta.get("name"):
        errors.append("Missing 'name' in frontmatter")
    if not meta.get("description") or not str(meta["description"]).strip():
        errors.append("Empty or missing 'description'")
    if not meta.get("when_to_use") or not str(meta["when_to_use"]).strip():
        errors.append("Empty or missing 'when_to_use' (required for discovery)")
    if not meta.get("domain"):
        errors.append("Missing 'domain'")
    return errors


def _load_skill_files_from_dir(directory: Path) -> list[dict]:
    """Load all SKILL.md files from a directory tree. Filters invalid skills."""
    skills = []
    for md_path in sorted(directory.rglob("*.md")):
        if md_path.name == "README.md":
            continue
        try:
            content = md_path.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(content)

            errors = _lint_skill(meta, md_path)
            if errors:
                log.warning("Skill at %s failed validation: %s. Skipping.",
                            md_path.relative_to(directory.parent.parent),
                            "; ".join(errors))
                continue

            skills.append({
                "name": meta["name"],
                "description": meta.get("description", "").strip(),
                "domain": meta.get("domain", "general"),
                "when_to_use": meta.get("when_to_use", ""),
                "tools_used": meta.get("tools_used", []),
                "citations": meta.get("citations", []),
                "path": str(md_path),
                "_meta": meta,
            })
        except Exception as exc:
            log.warning("Failed to load skill from %s: %s", md_path, exc)
    return skills


def _plugin_skill_dirs() -> list[Path]:
    """Discover plugin-contributed skill directories via aihydro.skills entry-points."""
    dirs = []
    try:
        from importlib.metadata import entry_points
        eps = entry_points(group="aihydro.skills")
        for ep in eps:
            try:
                get_dir = ep.load()
                skill_dir = get_dir()
                if isinstance(skill_dir, Path) and skill_dir.is_dir():
                    dirs.append(skill_dir)
                    log.info("Discovered skill plugin: %s -> %s", ep.name, skill_dir)
            except Exception as exc:
                log.warning("Failed to load skill plugin %s: %s", ep.name, exc)
    except Exception as exc:
        log.debug("aihydro.skills entry-point discovery failed: %s", exc)
    return dirs


def list_skills(domain: str | None = None, workspace_dir: str | None = None) -> list[dict]:
    """
    Return all available skills across all three tiers.

    Parameters
    ----------
    domain : str, optional
        Filter by domain (e.g. 'frequency-analysis', 'modelling', 'baseflow').
    workspace_dir : str, optional
        Workspace path to search for researcher-local skills in
        <workspace>/.aihydrorules/skills/.

    Returns
    -------
    list of skill descriptor dicts (name, description, domain, when_to_use,
    tools_used, citations, path).
    """
    # Collect by tier (later tiers override earlier by name)
    by_name: dict[str, dict] = {}

    # Tier 1: built-in
    for skill in _load_skill_files_from_dir(_SKILLS_DIR):
        by_name[skill["name"]] = skill

    # Tier 2: plugin
    for plugin_dir in _plugin_skill_dirs():
        for skill in _load_skill_files_from_dir(plugin_dir):
            by_name[skill["name"]] = skill

    # Tier 3: user-installed (marketplace, agent-created, manual)
    user_skills_dir = Path.home() / ".aihydro" / "skills"
    if user_skills_dir.is_dir():
        for sub in ("marketplace", "agent-created", "manual"):
            sub_dir = user_skills_dir / sub
            if sub_dir.is_dir():
                for skill in _load_skill_files_from_dir(sub_dir):
                    skill["_source"] = sub
                    by_name[skill["name"]] = skill

    # Tier 4: workspace
    if workspace_dir:
        ws_skills_dir = Path(workspace_dir) / ".aihydrorules" / "skills"
        if ws_skills_dir.is_dir():
            for skill in _load_skill_files_from_dir(ws_skills_dir):
                by_name[skill["name"]] = skill

    skills = list(by_name.values())
    if domain:
        skills = [s for s in skills if s.get("domain") == domain]
    return sorted(skills, key=lambda s: (s.get("domain", ""), s["name"]))


def load_skill(name: str, workspace_dir: str | None = None) -> dict | None:
    """
    Load the full content of a skill by name.

    Parameters
    ----------
    name : str
        Skill name (as returned by list_skills).
    workspace_dir : str, optional
        Workspace path for workspace-tier skills.

    Returns
    -------
    dict with all skill metadata plus 'body' (full markdown content),
    or None if not found.
    """
    all_skills = list_skills(workspace_dir=workspace_dir)
    for skill in all_skills:
        if skill["name"] == name:
            try:
                body = Path(skill["path"]).read_text(encoding="utf-8")
                result = dict(skill)
                result["body"] = body
                return result
            except Exception as exc:
                log.error("Failed to read skill body for %s: %s", name, exc)
                return None
    return None
