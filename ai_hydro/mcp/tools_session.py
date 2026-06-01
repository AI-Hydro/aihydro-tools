"""
Session management MCP tools (8 tools — 2.0.0).

Start, query, clear, annotate, export, and discover research sessions.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import (
    SessionResolutionError,
    _normalize_session_id,
    _resolve_session,
    _tool_error_to_dict,
    _workspace_write,
)

log = logging.getLogger("ai_hydro.mcp")


@mcp.tool()
def start_session(
    session_id: str | None = None,
    shard_id: str | None = None,
    workspace_dir: str | None = None,
) -> dict:
    """
    Start or resume a research session (persistent memory for a study).
    session_id: any string (slug, USGS gauge id, UUID); auto-generated if
    omitted. workspace_dir: pass once, remembered for all future calls —
    enables auto-save of tool outputs there. shard_id: for sub-agent forks.
    """
    try:
        from ai_hydro.session import HydroSession
        session_id = _normalize_session_id(session_id)
        session = HydroSession.load(session_id, shard_id=shard_id)
        if workspace_dir:
            session.workspace_dir = workspace_dir
        session.save()
        summary = session.summary()
        summary["shard_id"] = session.shard_id
        summary["workspace_dir"] = session.workspace_dir
        summary["python_interpreter"] = sys.executable
        pip_path = Path(sys.executable).parent / "pip"
        summary["mcp_pip"] = str(pip_path) if pip_path.exists() else f"{sys.executable} -m pip"
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "list", "--format=json"],
                capture_output=True, text=True, timeout=10,
            )
            pkgs = json.loads(result.stdout) if result.returncode == 0 else []
            summary["available_packages"] = {p["name"]: p["version"] for p in pkgs}
        except Exception:
            summary["available_packages"] = {}
        return summary
    except Exception as e:
        log.error("start_session failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def get_session_summary(session_id: str | None = None) -> dict:
    """
    Return computed/pending slots for the session + notes + interpretation.
    session_id is optional — auto-resolved from the active chat when omitted.
    """
    try:
        session_id = _resolve_session(session_id, None, allow_auto_create=False)
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        summary = session.summary()
        summary["workspace_dir"] = session.workspace_dir
        return summary
    except Exception as e:
        log.error("get_session_summary failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def get_session_health(session_id: str | None = None) -> dict:
    """
    Audit a session for identity drift, workspace problems, and consistency
    gaps. Returns warnings keyed by code so the agent can fix issues before
    they propagate into research.md, exports, or downstream tools.

    Common warning codes:
      - site_name_drift: display name disagrees with watershed.gauge_name
      - site_id_format:  USGS site_type but site_id isn't a gauge number
      - session_id_gauge_mismatch: session_id looks like a gauge != site_id
      - workspace_missing: workspace_dir no longer exists on disk
      - site_name_unset:  interpretation written but site_name empty

    Also returns the audit trail of site_name overwrites (when the LLM
    rewrote the display name during interpretation) and the canonical_id
    used for workspace filenames.
    """
    try:
        session_id = _resolve_session(session_id, None, allow_auto_create=False)
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        warnings = session.validate_identity()
        return {
            "session_id": session_id,
            "canonical_id": session.canonical_id,
            "site_name": session.site_name,
            "site_id": session.site_id,
            "site_type": session.site_type,
            "workspace_dir": session.workspace_dir,
            "warnings": warnings,
            "n_warnings": len(warnings),
            "site_name_history": session._site_name_history,
            "n_notes": len(session.notes),
            "n_computed_slots": len(session.computed()),
            "has_interpretation": bool(session.interpretation),
            "_remediation": (
                "If a warning is real, fix the underlying field via the "
                "appropriate tool — e.g. re-run write_research_interpretation "
                "with a corrected site_name; re-run delineate_watershed with "
                "the right gauge_id; update workspace_dir via start_session."
            ) if warnings else None,
        }
    except Exception as e:
        log.error("get_session_health failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def clear_session(session_id: str | None = None, slots: list[str] | None = None) -> dict:
    """
    Clear cached results to force recompute. Notes/workspace_dir/site_name/
    interpretation are always preserved. slots: subset of [watershed,
    streamflow, signatures, geomorphic, camels, forcing, twi, cn, model]
    (omit for all data slots).
    """
    try:
        session_id = _resolve_session(session_id, None, allow_auto_create=False)
        from ai_hydro.session import HydroSession
        from ai_hydro.session.store import _COMMON_SLOTS as _RESULT_SLOTS
        session = HydroSession.load(session_id)
        to_clear = slots if slots else list(_RESULT_SLOTS)
        invalid = [s for s in to_clear if s not in _RESULT_SLOTS]
        if invalid:
            hint = (
                " (notes are always preserved)"
                if "notes" in invalid else ""
            )
            return {
                "error": True,
                "code": "INVALID_SLOTS",
                "message": f"Unknown slots: {invalid}. Valid: {list(_RESULT_SLOTS)}{hint}",
            }
        cleared = []
        for slot in to_clear:
            if getattr(session, slot) is not None:
                setattr(session, slot, None)
                cleared.append(slot)
        session.save()
        summary = session.summary()
        summary["cleared"] = cleared
        summary["workspace_dir"] = session.workspace_dir
        return summary
    except Exception as e:
        log.error("clear_session failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def add_note(session_id: str | None = None, note: str = "") -> dict:
    """
    Append a researcher annotation (hypothesis, anomaly, decision) to the session.
    """
    try:
        session_id = _resolve_session(session_id, None, allow_auto_create=False)
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        added = session.add_note(note)
        if not added:
            # Deduped — surface clearly so the agent knows the note already exists.
            summary = session.summary()
            summary["_note_deduped"] = (
                "Identical note already at the tail of the list — not appended again."
            )
            return summary
        session.save()
        return session.summary()
    except Exception as e:
        log.error("add_note failed: %s", e)
        return _tool_error_to_dict(e)



@mcp.tool()
def archive_session(session_id: str | None = None) -> dict:
    """
    Freeze the session: move current interpretations + notes to a 'Historical'
    section in research.md. Use when concluding a study phase. Archived
    sessions stay readable and exportable.
    """
    try:
        session_id = _resolve_session(session_id, None, allow_auto_create=False)
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        session.archive()
        return session.summary()
    except Exception as e:
        log.error("archive_session failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def get_session_raw_state(session_id: str | None = None) -> dict:
    """
    Phase 1 of interpretation: return raw computed state for the LLM to read
    (large arrays summarised). Follow with write_research_interpretation to
    author + persist the prose synthesis.
    """
    try:
        session_id = _resolve_session(session_id, None, allow_auto_create=False)
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        slots = {}
        for slot in session.computed():
            slots[slot] = session.get(slot)
        return {
            "session_id": session_id,
            "site_name": session.site_name or None,
            "computed": session.computed(),
            "pending": session.pending(),
            "slots": slots,
            "notes": session.notes,
            "_instruction": (
                "You have received the raw computed session state. "
                "Read every slot carefully, look for cross-slot patterns and "
                "contradictions with the researcher notes, then call "
                "write_research_interpretation with your 3-6 sentence scientific "
                "synthesis. Write flowing prose — no bullet points."
            ),
        }
    except Exception as e:
        log.error("get_session_raw_state failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def merge_session_shards(session_id: str | None = None, shard_ids: list[str] | None = None) -> dict:
    """
    Consolidate sub-agent shard files into the main session (notes, citations,
    slots). Shard files are deleted after merge. shard_ids: subset or omit to
    merge all available shards.
    """
    try:
        session_id = _resolve_session(session_id, None, allow_auto_create=False)
        from ai_hydro.session import merge_session_shards as _merge
        result = _merge(session_id, shard_ids=shard_ids)
        return result
    except Exception as e:
        log.error("merge_session_shards failed: %s", e)
        return _tool_error_to_dict(e)

@mcp.tool()
def write_research_interpretation(
    session_id: str | None = None,
    site_name: str = "",
    interpretation: str = "",
) -> dict:
    """
    Phase 2 of interpretation: persist LLM-authored scientific synthesis to
    research.md + session so it's auto-injected into every future conversation.
    interpretation: 3-6 sentences of flowing prose (no bullets). site_name:
    short descriptive slug for display + exports.
    """
    try:
        session_id = _resolve_session(session_id, None, allow_auto_create=False)
        from ai_hydro.session import HydroSession
        from ai_hydro.session.store import _REPO_ROOT, _RULES_DIR_NAME
        from ai_hydro.mcp.tools_docs import _write_tools_md, _list_tools_sync

        session = HydroSession.load(session_id)
        session.interpretation = interpretation.strip()
        session.interpretation_at = datetime.now(timezone.utc).isoformat()
        # Use set_site_name so the previous value (if any) is audited into
        # _site_name_history — this is how we catch the "the agent rewrote
        # site_name during interpretation and drifted from the canonical
        # gauge_name" failure mode.
        session.set_site_name(site_name, reason="set via write_research_interpretation")
        session.save()

        tools_path = _write_tools_md()
        base = Path(session.workspace_dir) if session.workspace_dir else _REPO_ROOT
        research_md_path = base / _RULES_DIR_NAME / "research.md"

        citations_path: str | None = None
        bib = session.export_bibtex()
        if bib:
            saved = _workspace_write(session_id, "citations.bib", bib)
            citations_path = saved

        n_citations = len(session.get_citations())
        return {
            "written_path": str(research_md_path),
            "char_count": len(session.interpretation),
            "session_id": session_id,
            "site_name": session.site_name,
            "tools_md": str(tools_path),
            "n_tools": len(_list_tools_sync()),
            "citations_bib": citations_path,
            "n_data_source_citations": n_citations,
            "_note": (
                "Interpretation stored. research.md updated — your scientific "
                "context will be pre-loaded into every future conversation. "
                f"citations.bib written with {n_citations} data-source entries."
            ),
        }
    except Exception as e:
        log.error("write_research_interpretation failed: %s", e)
        return _tool_error_to_dict(e)

@mcp.tool()
def list_available_tools() -> dict:
    """
    List all registered MCP tools (built-in + community plugins via the
    aihydro.tools entry point). For token-efficient discovery, prefer
    aihydro_describe_capability(domain) over this full dump.
    """
    try:
        from ai_hydro.mcp.tools_docs import _list_tools_sync
        tools_raw = _list_tools_sync()
        tools_out = []
        for t in tools_raw:
            entry: dict = {"name": t.name, "description": (t.description or "").strip()}
            if hasattr(t, "parameters") and t.parameters:
                params = {}
                props = getattr(t.parameters, "properties", None) or {}
                for pname, pschema in props.items():
                    params[pname] = {
                        "type": pschema.get("type", "any"),
                        "description": pschema.get("description", ""),
                        "required": pname in (getattr(t.parameters, "required", None) or []),
                    }
                    if "default" in pschema:
                        params[pname]["default"] = pschema["default"]
                entry["parameters"] = params
            tools_out.append(entry)
        return {
            "tools": tools_out,
            "n_tools": len(tools_out),
            "python_interpreter": sys.executable,
            "note": (
                "Install community plugins with: pip install <plugin-package>. "
                "Restart the MCP server to discover newly installed plugins."
            ),
        }
    except Exception as e:
        log.error("list_available_tools failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def export_session(
    session_id: str | None = None,
    capsule_path: str | None = None,
    format: str = "capsule",
) -> dict:
    """
    Export the session. format='capsule' (default) writes a reproducible
    research folder (README.md + methods.md + citations.bib + environment.yml
    + session.json + data/figures/model/); 'bibtex' or 'json' write a single
    file. capsule_path: override default location.
    """
    try:
        session_id = _resolve_session(session_id, None, allow_auto_create=False)
        from ai_hydro.session import HydroSession
        import shutil
        from datetime import datetime

        session = HydroSession.load(session_id)
        today = datetime.now().strftime("%Y-%m-%d")
        slug = session.site_name or session.site_id or session_id
        files_written: list[str] = []

        if format in ("bibtex", "json"):
            content = session.export_bibtex() if format == "bibtex" else session.to_json()
            ext = ".bib" if format == "bibtex" else ".json"
            fname = f"session_{session_id}_{format}{ext}"
            saved = _workspace_write(session_id, fname, content)
            if not saved:
                d = Path.home() / ".aihydro" / "exports"
                d.mkdir(parents=True, exist_ok=True)
                p = d / fname
                p.write_text(content)
                saved = str(p)
            return {"session_id": session_id, "format": format, "file_saved": saved,
                    "computed": session.computed()}

        # Capsule
        if capsule_path:
            capsule_dir = Path(capsule_path)
        else:
            base = Path(session.workspace_dir) if session.workspace_dir else Path.home() / ".aihydro" / "exports"
            capsule_dir = base / f"capsule_{slug}_{today}"
        (capsule_dir / "data").mkdir(parents=True, exist_ok=True)
        (capsule_dir / "figures").mkdir(parents=True, exist_ok=True)
        (capsule_dir / "model").mkdir(parents=True, exist_ok=True)

        # README.md
        display = session.site_name or session.site_id or session_id
        name_str = ""
        if session.watershed:
            gname = session.watershed.get("data", {}).get("gauge_name", "")
            if gname:
                name_str = f" — {gname}"
        readme = [
            f"# Research Capsule: {display}{name_str}",
            f"**Session ID**: {session_id}",
        ]
        if session.site_id:
            readme.append(f"**Site**: {session.site_id}" +
                          (f" ({session.site_type})" if session.site_type else ""))
        readme += [f"**Exported**: {today}", f"**Platform**: AI-Hydro", "",
                   "## Computed Analyses"]
        for slot in session.computed():
            result = session.get(slot)
            computed_at = (result.get("meta", {}).get("computed_at", "")[:10]
                           if result else "") or "—"
            readme.append(f"- **{slot}** (computed {computed_at})")
        if session.pending():
            readme += ["", "## Pending", ", ".join(session.pending())]
        if session.notes:
            readme += ["", "## Researcher Notes"] + [f"- {n}" for n in session.notes]
        if session.interpretation:
            readme += ["", "## Scientific Summary", session.interpretation]
        else:
            readme += ["", "## Scientific Summary",
                       "_Not yet authored. Call `get_session_raw_state` then `write_research_interpretation` to generate._"]
        readme += ["", f"> Generated by AI-Hydro on {today}."]
        (capsule_dir / "README.md").write_text("\n".join(readme))
        files_written.append(str(capsule_dir / "README.md"))

        # methods.md — provenance table
        methods = ["# Methods", "", "## Provenance", "",
                   "| Analysis | Tool | Parameters | Data Source | Date |",
                   "|----------|------|-----------|-------------|------|"]
        for slot in session.computed():
            result = session.get(slot)
            if not result:
                continue
            meta = result.get("meta", {})
            tool = meta.get("tool", slot)
            params = "; ".join(f"{k}={v}" for k, v in meta.get("params", {}).items()) or "—"
            sources = ", ".join(
                s.get("name", s.get("url", "")) for s in meta.get("sources", [])
                if s.get("name") or s.get("url")
            ) or "—"
            date = meta.get("computed_at", "")[:10] or "—"
            methods.append(f"| {slot} | `{tool}` | {params} | {sources} | {date} |")
        methods += ["", "## Methods Prose", "",
                    "_Provenance table above contains all computational metadata. "
                    "Call `get_session_raw_state` then `write_research_interpretation` to generate "
                    "publication-quality methods prose, then paste it here._",
                    "", "<!-- Paste LLM-authored methods prose below -->"]
        (capsule_dir / "methods.md").write_text("\n".join(methods))
        files_written.append(str(capsule_dir / "methods.md"))

        # citations.bib
        (capsule_dir / "citations.bib").write_text(session.export_bibtex())
        files_written.append(str(capsule_dir / "citations.bib"))

        # session.json
        (capsule_dir / "session.json").write_text(session.to_json())
        files_written.append(str(capsule_dir / "session.json"))

        # Copy workspace files
        if session.workspace_dir:
            ws = Path(session.workspace_dir)
            for f in ws.iterdir():
                if not f.is_file():
                    continue
                if f.suffix in (".json", ".geojson", ".csv", ".tif", ".tiff"):
                    dest = capsule_dir / "data" / f.name
                    shutil.copy2(f, dest)
                    files_written.append(str(dest))
                elif f.suffix in (".png", ".html", ".svg"):
                    dest = capsule_dir / "figures" / f.name
                    shutil.copy2(f, dest)
                    files_written.append(str(dest))

        # model/ — copy trained model artifacts from session model slot
        if session.model:
            model_data = session.model.get("data", {})
            model_dir_str = model_data.get("model_dir")
            if model_dir_str:
                model_src = Path(model_dir_str)
                if model_src.is_dir():
                    for f in model_src.iterdir():
                        if f.is_file() and f.suffix in (".json", ".csv", ".pt", ".pth", ".txt"):
                            dest = capsule_dir / "model" / f.name
                            shutil.copy2(f, dest)
                            files_written.append(str(dest))
            # Write model metrics summary
            metrics = {k: v for k, v in model_data.items()
                       if k in ("nse", "kge", "rmse", "framework", "model_type",
                                "train_start", "train_end", "test_start", "test_end")}
            if metrics:
                (capsule_dir / "model" / "metrics.json").write_text(
                    json.dumps(metrics, indent=2)
                )
                files_written.append(str(capsule_dir / "model" / "metrics.json"))

        # environment.yml
        (capsule_dir / "environment.yml").write_text(_build_environment_yml(slug))
        files_written.append(str(capsule_dir / "environment.yml"))

        needs_interp = not session.interpretation
        return {
            "session_id": session_id,
            "site_name": session.site_name or None,
            "capsule_dir": str(capsule_dir),
            "files": files_written,
            "n_files": len(files_written),
            "computed": session.computed(),
            "_note": (
                "NEXT: call get_session_raw_state then write_research_interpretation "
                "to author the scientific interpretation, then export again to embed it in README.md."
                if needs_interp else
                "Scientific interpretation included. Add prose to methods.md."
            ),
        }
    except Exception as e:
        log.error("export_session failed: %s", e)
        return _tool_error_to_dict(e)


def _build_environment_yml(name: str) -> str:
    """
    Build a minimal, reproducible environment.yml.

    Strategy (in priority order):
    1. ``conda env export --from-history`` — only explicitly installed packages,
       avoiding the 400-line full-environment dump of ``--no-builds``.
    2. Minimal hand-crafted YAML — used when conda is unavailable or the
       ``--from-history`` output is suspiciously short (< 5 lines).
    """
    import subprocess as _sp

    def _curated() -> str:
        # Get installed versions of core packages for pinning
        versions: dict = {}
        try:
            import importlib.metadata as _imeta
            for pkg in ("aihydro-tools", "numpy", "pandas", "scipy",
                        "torch", "rasterio", "geopandas"):
                try:
                    versions[pkg] = _imeta.version(pkg)
                except Exception:
                    pass
        except Exception:
            pass

        def _pin(pkg: str, fallback: str = "") -> str:
            v = versions.get(pkg)
            return f"    - {pkg}=={v}" if v else (
                f"    - {pkg}>={fallback}" if fallback else f"    - {pkg}"
            )

        lines = [
            f"name: {name}",
            "channels:",
            "  - conda-forge",
            "  - defaults",
            "dependencies:",
            "  - python>=3.10",
            "  - pip",
            "  - pip:",
            _pin("aihydro-tools", "1.4.0"),
            "    - dataretrieval",
            "    - pynhd",
            "    - pygeohydro",
            "    - pygridmet",
            "    - py3dep",
            "    - pysheds",
            _pin("rasterio", "1.3"),
            _pin("geopandas", "0.14"),
            _pin("xarray", ""),
            _pin("numpy", "1.26"),
            _pin("pandas", "2.0"),
            _pin("scipy", "1.11"),
            _pin("torch", "2.0"),
            "    - matplotlib",
            "# Re-create with: conda env create -f environment.yml",
            "# Pin all versions for full reproducibility with: pip freeze > requirements.txt",
        ]
        return "\n".join(lines)

    try:
        r = _sp.run(
            ["conda", "env", "export", "--from-history"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            lines = r.stdout.splitlines()
            # from-history output is only meaningful if it lists actual packages
            # (sometimes it returns just name/channels/prefix with no deps)
            has_deps = any(
                line.strip() and not line.startswith(("name:", "channels:",
                                                       "dependencies:", "prefix:", "-"))
                for line in lines
            )
            if len(lines) >= 8 or has_deps:
                if lines and lines[0].startswith("name:"):
                    lines[0] = f"name: {name}"
                # Strip the absolute prefix line — breaks portability
                lines = [l for l in lines if not l.startswith("prefix:")]
                return "\n".join(lines)
    except Exception:
        pass

    return _curated()


# ============================================================================
# Admin: Chat ↔ Study rebinding (Wave 3)
# ============================================================================

@mcp.tool()
def aihydro_rebind_chat(
    study_id: str,
    chat_id: str | None = None,
) -> dict:
    """
    Rebind the current chat to a specific existing study.

    Use when the agent selected the wrong study, or when you want to resume a
    previous study from a fresh chat.

    Parameters
    ----------
    study_id : str
        The session/study ID to bind to (e.g. 'basin_26p9_78p1' or '01031500').
    chat_id : str | None
        The Cline chat ULID to bind. Normally injected automatically by the
        extension; pass explicitly only in edge cases.

    Returns
    -------
    dict with ``bound_to``, ``study_id``, and confirmation message.
    """
    try:
        from ai_hydro.session import HydroSession
        from ai_hydro.session.chat_binding import get_binding_store

        # Validate study exists (loads or creates)
        sid = _normalize_session_id(study_id)
        session = HydroSession.load(sid)  # raises if truly invalid path
        slots_done = session.computed()

        store = get_binding_store()
        if chat_id:
            store.bind(chat_id, sid)

        return {
            "bound_to": sid,
            "study_id": sid,
            "chat_id": chat_id,
            "study_slots": slots_done,
            "message": (
                f"Chat rebound to study '{sid}'. "
                f"Computed slots: {slots_done or 'none yet'}. "
                "All subsequent analysis tools will operate on this study."
            ),
        }
    except Exception as e:
        log.error("aihydro_rebind_chat failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def aihydro_chat_status(chat_id: str | None = None) -> dict:
    """
    Show what study (if any) is currently bound to this chat.

    Returns the bound study_id, its computed slots, and whether the binding
    came from a previous session or is brand new.  Useful for diagnostics and
    at the start of a fresh conversation to see if a study is already in scope.

    chat_id : str | None — injected automatically by the extension; pass
    explicitly when calling outside a chat context.
    """
    try:
        from ai_hydro.session.chat_binding import get_binding_store
        store = get_binding_store()
        bound = store.lookup_study(chat_id) if chat_id else None

        if bound:
            from ai_hydro.session import HydroSession
            try:
                session = HydroSession.load(bound)
                slots = session.computed()
                notes_count = len(session.notes or [])
                return {
                    "bound": True,
                    "study_id": bound,
                    "chat_id": chat_id,
                    "computed_slots": slots,
                    "notes": notes_count,
                    "message": (
                        f"This chat is bound to study '{bound}'. "
                        f"Computed: {slots or 'none yet'}."
                    ),
                }
            except Exception as load_err:
                return {
                    "bound": True,
                    "study_id": bound,
                    "chat_id": chat_id,
                    "computed_slots": [],
                    "warning": f"Study file unreadable: {load_err}",
                }
        else:
            return {
                "bound": False,
                "study_id": None,
                "chat_id": chat_id,
                "message": (
                    "No study is bound to this chat yet. "
                    "Run delineate_watershed or delineate_watershed_from_point "
                    "to auto-create one, or call start_session(session_id=...) explicitly."
                ),
            }
    except Exception as e:
        log.error("aihydro_chat_status failed: %s", e)
        return _tool_error_to_dict(e)


# ============================================================================
# C2: Feature Registry tools — addressable multi-geometry
# ============================================================================

@mcp.tool()
def register_feature(
    geojson: str,
    name: str = "",
    source: str = "map_annotation",
    session_id: str | None = None,
    set_active: bool = True,
    feature_id: str | None = None,
) -> dict:
    """
    Register a named geometry in the session's feature registry.

    Use this to give a stable id to any geometry (a map annotation, a
    delineated sub-basin, an uploaded polygon, etc.) so spatial tools like
    compute_twi and create_cn_grid can address it by name instead of requiring
    the raw GeoJSON every time.

    Parameters
    ----------
    geojson : str
        GeoJSON string (Polygon, MultiPolygon, or GeoJSON Feature). Pass the
        geometry string returned by map tools or from a workspace .geojson file.
    name : str
        Human-readable label — e.g. "Upper basin", "Annotation 1". Used as a
        lookup alias in addition to feature_id.
    source : str
        Provenance tag: "map_annotation" | "delineate_watershed" |
        "upload" | "on-the-fly". Defaults to "map_annotation".
    session_id : str | None
        Session to register into. Auto-resolved when omitted.
    set_active : bool
        If True (default), mark this as the active feature so subsequent
        spatial tools use it without an explicit feature= parameter.
    feature_id : str | None
        Explicit id to assign. If None, derived from name (slugified) or a
        random hex string.
    """
    try:
        session_id = _resolve_session(session_id, None)
        from ai_hydro.session import HydroSession
        from aihydro_core.features.registry import FeatureRegistry
        session = HydroSession.load(session_id)
        registry = FeatureRegistry(session)
        feat = registry.register(
            geojson=geojson,
            name=name,
            source=source,
            feature_id=feature_id,
            set_active=set_active,
        )
        return {
            "feature_id": feat.feature_id,
            "name": feat.name,
            "source": feat.source,
            "active": set_active,
            "session_id": session_id,
            "message": (
                f"Registered feature '{feat.feature_id}'"
                + (f" ({feat.name})" if feat.name else "")
                + (f" as active feature." if set_active else ".")
                + " Pass feature='{feat.feature_id}' to spatial tools to address this geometry."
            ).replace("'{feat.feature_id}'", f"'{feat.feature_id}'"),
        }
    except Exception as e:
        log.error("register_feature failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def list_features(session_id: str | None = None) -> dict:
    """
    List all geometry features registered in the session.

    Returns the id, name, source, and area (if known) for each feature, plus
    the active feature id. Useful for checking which geometries are addressable
    before calling compute_twi, create_cn_grid, etc.

    session_id : str | None — session to inspect. Auto-resolved when omitted.
    """
    try:
        session_id = _resolve_session(session_id, None)
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        feats = session.list_features()
        return {
            "session_id": session_id,
            "active_feature_id": session.get_active_feature_id(),
            "count": len(feats),
            "features": [
                {
                    "feature_id": f.feature_id,
                    "name": f.name,
                    "source": f.source,
                    "area_km2": f.area_km2,
                    "created_at": f.created_at[:10] if f.created_at else None,
                }
                for f in feats
            ],
        }
    except Exception as e:
        log.error("list_features failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def set_active_feature(
    feature_id: str,
    session_id: str | None = None,
) -> dict:
    """
    Set the active (default) geometry feature in the session.

    After calling this, spatial tools called without an explicit feature=
    parameter will operate on this geometry. Useful when switching focus
    between multiple registered features (e.g. comparing two sub-basins).

    Parameters
    ----------
    feature_id : str
        The id of the feature to make active. Must already be registered
        (call register_feature or list_features to see available ids).
    session_id : str | None
        Session to update. Auto-resolved when omitted.
    """
    try:
        session_id = _resolve_session(session_id, None)
        from ai_hydro.session import HydroSession
        from aihydro_core.features.registry import FeatureRegistry
        session = HydroSession.load(session_id)
        registry = FeatureRegistry(session)
        registry.set_active(feature_id)
        feat = session.get_feature(feature_id)
        return {
            "feature_id": feature_id,
            "name": feat.name if feat else "",
            "session_id": session_id,
            "message": (
                f"Active feature set to '{feature_id}'"
                + (f" ({feat.name})" if feat and feat.name else "")
                + ". Subsequent spatial tools will use this geometry by default."
            ),
        }
    except Exception as e:
        log.error("set_active_feature failed: %s", e)
        return _tool_error_to_dict(e)
