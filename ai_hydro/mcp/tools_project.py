"""
Project, Literature, and Researcher Profile MCP tools.

PROJECT MANAGEMENT
  start_project            — create / resume a named research project
  get_project_summary      — overview of all sessions, journal, literature
  add_session_to_project   — associate any research session with a project
  search_experiments       — full-text search across all project sessions

LITERATURE
  index_literature       — scan a folder of papers → build searchable index
  search_literature      — query the index; returns excerpts for agent synthesis
  add_journal_entry      — log a timestamped research note to the project journal

RESEARCHER PROFILE
  get_researcher_profile     — return the persistent researcher persona
  update_researcher_profile  — update specific fields (agent or user driven)
  log_researcher_observation — agent logs an observation about the researcher
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp")

# ---------------------------------------------------------------------------
# PROJECT MANAGEMENT
# ---------------------------------------------------------------------------


@mcp.tool()
def start_project(
    name: str,
    description: str = "",
    topics: list[str] | None = None,
) -> dict:
    """
    Create or resume a named research project — top-level unit spanning
    multiple sessions + a literature folder + experiment journal. Idempotent.
    name: directory-safe slug (no spaces). Sets the project as active in the
    researcher profile.
    """
    try:
        from ai_hydro.session.project import ProjectSession
        from ai_hydro.session.persona import ResearcherProfile

        project = ProjectSession.load(name)
        is_new = not ProjectSession._path(name).exists()

        if description:
            project.description = description
        if topics:
            for t in topics:
                if t not in project.topics:
                    project.topics.append(t)

        # Ensure literature folder exists
        project.literature_path.mkdir(parents=True, exist_ok=True)

        project.save()

        # Mark as active project in researcher profile
        profile = ResearcherProfile.load()
        profile.update(active_project=name)
        profile.save()

        result = project.summary()
        result["is_new"] = is_new
        result["status"] = "created" if is_new else "resumed"
        result["literature_dir"] = str(project.literature_path)
        result["tip"] = (
            f"Drop papers/documents into {project.literature_path} "
            "then call index_literature to make them searchable."
        )
        return result
    except Exception as e:
        log.error("start_project failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def get_project_summary(project_name: str) -> dict:
    """
    Overview of a project: metadata, session summaries (with computed/pending
    slots), recent journal entries, literature index status.
    """
    try:
        from ai_hydro.session.project import ProjectSession

        project = ProjectSession.load(project_name)
        summary = project.summary()
        summary["session_summaries"] = project.session_summaries()
        summary["recent_journal"] = project.journal[-5:] if project.journal else []
        summary["notes"] = project.notes

        # Literature status
        lit_dir = project.literature_path
        if lit_dir.exists():
            docs = [f.name for f in lit_dir.iterdir()
                    if f.suffix.lower() in (".pdf", ".txt", ".md", ".docx")]
            summary["literature_files"] = docs
            summary["literature_indexed"] = project.literature_index_path.exists()
        else:
            summary["literature_files"] = []
            summary["literature_indexed"] = False

        return summary
    except Exception as e:
        log.error("get_project_summary failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def add_session_to_project(project_name: str, session_id: str) -> dict:
    """
    Link a session to a project. Session need not exist yet (pre-registration
    OK). Returns the updated session list.
    """
    try:
        from ai_hydro.session.project import ProjectSession

        project = ProjectSession.load(project_name)
        added = project.add_session(session_id)
        project.save()

        return {
            "project": project_name,
            "session_id": session_id,
            "added": added,
            "all_sessions": project.session_ids,
            "message": (
                f"Session '{session_id}' {'added to' if added else 'already in'} "
                f"project '{project_name}'."
            ),
        }
    except Exception as e:
        log.error("add_session_to_project failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def search_experiments(
    project_name: str,
    query: str,
    compare_sessions: bool = False,
) -> dict:
    """
    Full-text search across all sessions in a project (case-insensitive over
    every computed slot's JSON). Use for "find basins with NSE > 0.7",
    "which gauges have high BFI". compare_sessions=True adds a side-by-side
    metrics table.
    """
    try:
        from ai_hydro.session.project import ProjectSession

        project = ProjectSession.load(project_name)
        matches = project.search_experiments(query)

        result: dict = {
            "project": project_name,
            "query": query,
            "n_sessions_searched": len(project.session_ids),
            "n_matches": len(matches),
            "matches": matches,
        }

        if compare_sessions:
            result["comparison"] = project.compare_sessions()

        return result
    except Exception as e:
        log.error("search_experiments failed: %s", e)
        return _tool_error_to_dict(e)


# ---------------------------------------------------------------------------
# LITERATURE
# ---------------------------------------------------------------------------


@mcp.tool()
def index_literature(
    project_name: str,
    folder_path: str | None = None,
) -> dict:
    """
    Scan a folder of papers (.txt, .md, .pdf via pypdf) and build a
    text-only searchable index (literature_index.md with first ~800 chars
    per doc). Re-run when papers are added. folder_path defaults to
    ~/.aihydro/projects/<name>/literature/.
    """
    try:
        from ai_hydro.session.project import ProjectSession

        project = ProjectSession.load(project_name)

        # Resolve folder
        if folder_path:
            lit_dir = Path(folder_path).expanduser().resolve()
            project.literature_dir = str(lit_dir)
        else:
            lit_dir = project.literature_path
            lit_dir.mkdir(parents=True, exist_ok=True)

        if not lit_dir.exists():
            return {
                "error": True,
                "message": f"Folder not found: {lit_dir}",
            }

        # Gather files
        supported = {".txt", ".md", ".pdf"}
        files = [f for f in lit_dir.iterdir() if f.suffix.lower() in supported]

        if not files:
            return {
                "project": project_name,
                "folder": str(lit_dir),
                "n_files": 0,
                "message": (
                    f"No supported files found in {lit_dir}. "
                    "Drop .txt, .md, or .pdf files there and re-run."
                ),
            }

        # Build index
        index_lines = [
            f"# Literature Index — {project_name}",
            f"*{len(files)} documents indexed from {lit_dir}*",
            f"*Last updated: {__import__('datetime').datetime.now().isoformat()[:10]}*",
            "",
        ]

        indexed = []
        skipped = []

        for fpath in sorted(files):
            content = _read_document(fpath)
            if content is None:
                skipped.append(fpath.name)
                continue

            # Trim to ~800 chars for the index
            excerpt = content[:800].replace("\n", " ").strip()
            if len(content) > 800:
                excerpt += "…"

            index_lines += [
                f"## {fpath.name}",
                f"**Path**: `{fpath}`",
                f"**Excerpt**: {excerpt}",
                "",
            ]
            indexed.append(fpath.name)

        # Write index
        index_path = project.literature_index_path
        index_path.write_text("\n".join(index_lines))

        project.save()

        return {
            "project": project_name,
            "folder": str(lit_dir),
            "n_files": len(indexed),
            "indexed": indexed,
            "skipped": skipped,
            "index_path": str(index_path),
            "message": (
                f"Indexed {len(indexed)} documents. "
                "Use search_literature to query them."
            ),
        }
    except Exception as e:
        log.error("index_literature failed: %s", e)
        return _tool_error_to_dict(e)


def _read_document(path: Path) -> str | None:
    """Read a document to text. Returns None if unreadable."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".txt", ".md"):
            return path.read_text(errors="replace")
        elif suffix == ".pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(str(path))
                pages = [page.extract_text() or "" for page in reader.pages]
                return "\n".join(pages)
            except ImportError:
                try:
                    import pdfplumber
                    with pdfplumber.open(str(path)) as pdf:
                        pages = [p.extract_text() or "" for p in pdf.pages]
                    return "\n".join(pages)
                except ImportError:
                    log.warning(
                        "PDF reading requires 'pypdf' or 'pdfplumber'. "
                        "Install with: pip install pypdf"
                    )
                    return f"[PDF: {path.name} — install pypdf to index PDFs]"
    except Exception as e:
        log.warning("Could not read %s: %s", path, e)
        return None


@mcp.tool()
def search_literature(
    project_name: str,
    query: str,
    return_full_content: bool = False,
) -> dict:
    """
    Query the literature index. Text match on filenames + excerpts, no vector
    DB. return_full_content=True returns full text of matched docs for the
    LLM to synthesise (can be large).
    """
    try:
        from ai_hydro.session.project import ProjectSession

        project = ProjectSession.load(project_name)

        if not project.literature_index_path.exists():
            return {
                "indexed": False,
                "message": (
                    "Literature not indexed yet. "
                    "Run index_literature first."
                ),
                "n_matches": 0,
                "matches": [],
            }

        index_text = project.literature_index_path.read_text()
        q = query.lower()

        # Parse index sections (each file starts with ## filename)
        sections: list[dict] = []
        current: dict | None = None
        for line in index_text.splitlines():
            if line.startswith("## "):
                if current:
                    sections.append(current)
                current = {"filename": line[3:].strip(), "lines": []}
            elif current is not None:
                current["lines"].append(line)
        if current:
            sections.append(current)

        # Match sections where query appears
        matches = []
        for sec in sections:
            blob = " ".join(sec["lines"]).lower()
            if q in blob or q in sec["filename"].lower():
                excerpt_line = next(
                    (l for l in sec["lines"] if l.startswith("**Excerpt**")), ""
                )
                matches.append({
                    "filename": sec["filename"],
                    "excerpt": excerpt_line.replace("**Excerpt**: ", ""),
                })

        result: dict = {
            "project": project_name,
            "query": query,
            "n_matches": len(matches),
            "matches": matches,
        }

        # Optionally return full document text
        if return_full_content and matches:
            lit_dir = (
                Path(project.literature_dir)
                if project.literature_dir
                else project.literature_path
            )
            full: dict[str, str] = {}
            for m in matches:
                fpath = lit_dir / m["filename"]
                if fpath.exists():
                    content = _read_document(fpath)
                    if content:
                        full[m["filename"]] = content
            result["full_content"] = full

        result["suggestion"] = (
            f"Found {len(matches)} documents matching '{query}'. "
            "Synthesize key findings, compare methodologies, or extract "
            "specific metrics using the excerpts above."
            if matches
            else f"No documents matched '{query}'. Try broader terms or run index_literature to rebuild the index."
        )

        return result
    except Exception as e:
        log.error("search_literature failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def add_journal_entry(
    project_name: str,
    entry: str,
    tags: list[str] | None = None,
) -> dict:
    """
    Add a timestamped entry to the project's experiment journal (persistent
    across conversations; searchable via search_experiments). Use for
    findings, decisions, hypotheses, anomalies, lit-synthesis conclusions.
    """
    try:
        from ai_hydro.session.project import ProjectSession

        project = ProjectSession.load(project_name)
        entry = project.log_entry(entry, tags)
        project.save()

        return {
            "project": project_name,
            "entry": entry,
            "n_total_entries": len(project.journal),
        }
    except Exception as e:
        log.error("add_journal_entry failed: %s", e)
        return _tool_error_to_dict(e)


# ---------------------------------------------------------------------------
# RESEARCHER PROFILE
# ---------------------------------------------------------------------------


@mcp.tool()
def get_researcher_profile() -> dict:
    """
    Persistent researcher profile (domain, tools, preferences, focus). Built
    up over time from interactions; editable via update_researcher_profile.
    Returns fields + a formatted context_string for injection.
    """
    try:
        from ai_hydro.session.persona import ResearcherProfile

        profile = ResearcherProfile.load()
        result = profile.summary()
        result["context_string"] = profile.to_context_string()
        result["is_blank"] = profile.is_blank()
        return result
    except Exception as e:
        log.error("get_researcher_profile failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def update_researcher_profile(
    name: str | None = None,
    institution: str | None = None,
    role: str | None = None,
    domain: str | None = None,
    research_focus: str | None = None,
    expertise: list[str] | None = None,
    preferred_models: list[str] | None = None,
    tools_familiarity: dict | None = None,
    communication_style: str | None = None,
    active_project: str | None = None,
) -> dict:
    """
    Update the researcher profile. Strings replace; list fields (expertise,
    preferred_models) append; dict fields (tools_familiarity) merge.
    Call when the user states or you infer something durable about them.
    """
    try:
        from ai_hydro.session.persona import ResearcherProfile

        profile = ResearcherProfile.load()

        updates = {k: v for k, v in {
            "name": name,
            "institution": institution,
            "role": role,
            "domain": domain,
            "research_focus": research_focus,
            "expertise": expertise,
            "preferred_models": preferred_models,
            "tools_familiarity": tools_familiarity,
            "communication_style": communication_style,
            "active_project": active_project,
        }.items() if v is not None}

        changed = profile.update(**updates)
        profile.save()

        return {
            "changed_fields": changed,
            "profile": profile.summary(),
            "message": (
                f"Updated: {', '.join(changed)}"
                if changed
                else "No changes — values already match."
            ),
        }
    except Exception as e:
        log.error("update_researcher_profile failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def log_researcher_observation(observation: str) -> dict:
    """
    Log a meaningful observation about the researcher (memory-style; for
    durable inferences not captured by structured profile fields).
    Skip trivial confirmations or temporary preferences.
    """
    try:
        from ai_hydro.session.persona import ResearcherProfile

        profile = ResearcherProfile.load()
        profile.add_observation(observation)
        profile.save()

        return {
            "observation_logged": observation,
            "n_total_observations": len(profile.observations),
            "recent_observations": profile.observations[-5:],
        }
    except Exception as e:
        log.error("log_researcher_observation failed: %s", e)
        return _tool_error_to_dict(e)
