"""
Course MCP tools (Phase C of HTML-Preview course mode).

These tools let the agent become a teaching assistant for courses authored
as ``course.json`` manifests inside the workspace. The webview UI (HTML
Preview panel) writes two files the agent reads:

  ~/.aihydro/active_course.json          ← "what course is the user in"
  ~/.aihydro/course_progress/<id>.json   ← per-course progress snapshot

The agent never has to ask the user for paths — calling :func:`course_get_state`
returns everything it needs to suggest the next module, congratulate them on
recent completions, or unlock prerequisites the user has demonstrated mastery
of through conversation.

Disk schema (read-only contract; the webview is the writer):

active_course.json
    {
      "courseId":   str,
      "coursePath": str   (absolute path to course.json),
      "courseRoot": str   (folder containing course.json),
      "currentModuleId": str | null,
      "lastSeenAt": int   (epoch ms),
    }

course_progress/<id>.json
    {
      "courseId":         str,
      "startedAt":        int,
      "lastVisitedAt":    int,
      "currentModuleId":  str | null,
      "completed": {
          "<moduleId>": { "completedAt": int, "timeSpentMs": int? },
          ...
      },
    }

course.json (in the workspace)
    {
      "courseId": str, "title": str, "version"?: str,
      "abstract"?: str, "estimatedHours"?: number,
      "authors"?: [{"name": str, ...}],
      "modules": [
        { "id": str, "title": str, "path": str,
          "prerequisites"?: [str], "estimatedMinutes"?: number }, ...
      ],
    }
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from ai_hydro.mcp.app import mcp

log = logging.getLogger("ai_hydro.mcp.course")

# ── Disk paths ──────────────────────────────────────────────────────────────

_AIHYDRO_HOME = Path.home() / ".aihydro"
_ACTIVE_COURSE_FILE = _AIHYDRO_HOME / "active_course.json"
_NAV_INTENT_FILE = _AIHYDRO_HOME / "course_nav_intent.json"
_PROGRESS_DIR = _AIHYDRO_HOME / "course_progress"


def _safe_course_id(course_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", course_id) or "unknown"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("failed to read %s: %s", path, e)
        return None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{int(time.time()*1000)}")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load_active_pointer() -> dict[str, Any] | None:
    return _read_json(_ACTIVE_COURSE_FILE)


def _load_progress(course_id: str) -> dict[str, Any]:
    data = _read_json(_PROGRESS_DIR / f"{_safe_course_id(course_id)}.json")
    if not data:
        now = int(time.time() * 1000)
        return {
            "courseId": course_id,
            "startedAt": now,
            "lastVisitedAt": now,
            "currentModuleId": None,
            "completed": {},
        }
    # Defensive fills (mirrors the TS store's migrations)
    data.setdefault("completed", {})
    data.setdefault("currentModuleId", None)
    data.setdefault("startedAt", int(time.time() * 1000))
    data.setdefault("lastVisitedAt", data["startedAt"])
    data["courseId"] = course_id
    return data


def _save_progress(progress: dict[str, Any]) -> None:
    course_id = progress.get("courseId") or "unknown"
    _atomic_write_json(_PROGRESS_DIR / f"{_safe_course_id(course_id)}.json", progress)


def _load_manifest(course_path: str | None) -> dict[str, Any] | None:
    if not course_path:
        return None
    p = Path(course_path)
    if not p.exists():
        return None
    return _read_json(p)


def _missing_prerequisites(module: dict[str, Any], completed: dict[str, Any]) -> list[str]:
    prereqs = module.get("prerequisites") or []
    return [pid for pid in prereqs if pid not in completed]


def _resolve_course_path(course_id_or_path: str | None) -> tuple[str | None, str | None]:
    """Return (course_id, course_path) — either from an explicit argument or
    by falling back to the active-course pointer."""
    if course_id_or_path:
        if course_id_or_path.endswith(".json") and Path(course_id_or_path).exists():
            manifest = _read_json(Path(course_id_or_path))
            return ((manifest or {}).get("courseId"), course_id_or_path)
        # treat as a courseId — try to resolve via active pointer
        active = _load_active_pointer() or {}
        if active.get("courseId") == course_id_or_path:
            return (course_id_or_path, active.get("coursePath"))
        # Couldn't resolve path from id alone — return id only
        return (course_id_or_path, None)
    active = _load_active_pointer() or {}
    return (active.get("courseId"), active.get("coursePath"))


# ── Tool 1: course_get_state ────────────────────────────────────────────────


@mcp.tool()
def course_get_state() -> dict:
    """
    Snapshot of the active course: current module, completion %, locked
    modules, next_recommended id. Call at the start of any course
    conversation. Returns {active: false} if no course is open.
    """
    pointer = _load_active_pointer()
    if not pointer or not pointer.get("courseId"):
        return {
            "active": False,
            "message": "No active course. The user has not opened a course module in the HTML Preview panel yet.",
        }

    course_id = pointer["courseId"]
    manifest = _load_manifest(pointer.get("coursePath")) or {}
    progress = _load_progress(course_id)
    completed = progress.get("completed") or {}
    modules_in = manifest.get("modules") or []

    enriched_modules: list[dict[str, Any]] = []
    next_recommended: str | None = None
    for m in modules_in:
        mid = m.get("id")
        if not mid:
            continue
        missing = _missing_prerequisites(m, completed)
        is_completed = mid in completed
        is_locked = bool(missing)
        enriched_modules.append({
            "id": mid,
            "title": m.get("title"),
            "completed": is_completed,
            "locked": is_locked,
            "missing_prerequisites": missing,
            "estimated_minutes": m.get("estimatedMinutes"),
        })
        if next_recommended is None and not is_completed and not is_locked:
            next_recommended = mid

    total = len(enriched_modules)
    completed_count = sum(1 for m in enriched_modules if m["completed"])
    pct = round((completed_count / total) * 100) if total else 0

    current_id = pointer.get("currentModuleId") or progress.get("currentModuleId")
    current_module = next((m for m in enriched_modules if m["id"] == current_id), None)

    return {
        "active": True,
        "course_id": course_id,
        "title": manifest.get("title"),
        "version": manifest.get("version"),
        "abstract": manifest.get("abstract"),
        "estimated_hours": manifest.get("estimatedHours"),
        "authors": manifest.get("authors") or [],
        "current_module": current_module,
        "modules": enriched_modules,
        "completed_count": completed_count,
        "total": total,
        "completion_pct": pct,
        "next_recommended": next_recommended,
        "course_path": pointer.get("coursePath"),
        "course_root": pointer.get("courseRoot"),
        "last_visited_at": progress.get("lastVisitedAt"),
    }


# ── Tool 2: course_get_curriculum ───────────────────────────────────────────


@mcp.tool()
def course_get_curriculum(course_id_or_path: str | None = None) -> dict:
    """
    Full manifest + prerequisite_graph for a course. Use when you need the
    structure (not just progress). ``course_id_or_path``: courseId of the
    active course OR absolute path to course.json. Defaults to active course.
    """
    course_id, course_path = _resolve_course_path(course_id_or_path)
    manifest = _load_manifest(course_path)
    if manifest is None:
        return {
            "error": True,
            "message": (
                f"Could not load course manifest for '{course_id_or_path or '(active)'}'. "
                "Pass an absolute path to course.json, or ensure the user has opened a "
                "course module in the HTML Preview panel."
            ),
        }
    modules = manifest.get("modules") or []
    graph = {
        m.get("id"): list(m.get("prerequisites") or [])
        for m in modules
        if m.get("id")
    }
    return {
        "course_id": manifest.get("courseId") or course_id,
        "title": manifest.get("title"),
        "version": manifest.get("version"),
        "abstract": manifest.get("abstract"),
        "estimated_hours": manifest.get("estimatedHours"),
        "authors": manifest.get("authors") or [],
        "modules": modules,
        "prerequisite_graph": graph,
        "module_count": len(modules),
        "course_path": course_path,
    }


# ── Tool 3: course_set_progress ─────────────────────────────────────────────


@mcp.tool()
def course_set_progress(
    module_id: str,
    action: str = "complete",
    reason: str | None = None,
) -> dict:
    """
    Mutate progress for the active course. Requires explicit user agreement.

    action: complete | uncomplete | unlock_prereqs | set_current
      • unlock_prereqs marks ALL prerequisites of module_id as completed
        (use when user has prior knowledge and wants to skip ahead).
    reason: short string stored on the completion record for audit.
    """
    pointer = _load_active_pointer()
    if not pointer or not pointer.get("courseId"):
        return {"error": True, "message": "No active course — cannot mutate progress."}
    course_id = pointer["courseId"]
    manifest = _load_manifest(pointer.get("coursePath")) or {}
    modules = manifest.get("modules") or []
    target = next((m for m in modules if m.get("id") == module_id), None)
    if action != "set_current" and not target:
        return {
            "error": True,
            "message": f"Module '{module_id}' not found in course '{course_id}'.",
            "valid_module_ids": [m.get("id") for m in modules if m.get("id")],
        }

    progress = _load_progress(course_id)
    progress["lastVisitedAt"] = int(time.time() * 1000)

    if action == "complete":
        record: dict[str, Any] = {"completedAt": int(time.time() * 1000)}
        if reason:
            record["reason"] = reason
            record["agentGranted"] = True
        progress["completed"][module_id] = record
    elif action == "uncomplete":
        progress["completed"].pop(module_id, None)
    elif action == "unlock_prereqs":
        prereqs = list((target or {}).get("prerequisites") or [])
        ts = int(time.time() * 1000)
        for pid in prereqs:
            if pid not in progress["completed"]:
                progress["completed"][pid] = {
                    "completedAt": ts,
                    "reason": reason or f"unlocked by agent to access {module_id}",
                    "agentGranted": True,
                }
    elif action == "set_current":
        progress["currentModuleId"] = module_id
    else:
        return {
            "error": True,
            "message": f"Unknown action '{action}'. Use complete | uncomplete | unlock_prereqs | set_current.",
        }

    _save_progress(progress)
    return {
        "ok": True,
        "action": action,
        "module_id": module_id,
        "course_id": course_id,
        "progress": progress,
    }


# ── Tool 4: course_navigate ─────────────────────────────────────────────────


@mcp.tool()
def course_navigate(module_id: str, reason: str | None = None) -> dict:
    """
    Push the HTML Preview panel to open a specific course module.

    Webview enforces the prerequisite gate — call course_set_progress
    (action='unlock_prereqs') first if the target is locked.
    """
    pointer = _load_active_pointer()
    if not pointer or not pointer.get("courseId"):
        return {"error": True, "message": "No active course — cannot navigate."}
    course_id = pointer["courseId"]
    manifest = _load_manifest(pointer.get("coursePath")) or {}
    modules = manifest.get("modules") or []
    if not any(m.get("id") == module_id for m in modules):
        return {
            "error": True,
            "message": f"Module '{module_id}' not found in course '{course_id}'.",
            "valid_module_ids": [m.get("id") for m in modules if m.get("id")],
        }

    intent = {
        "courseId": course_id,
        "moduleId": module_id,
        "reason": reason or "",
        "timestamp": int(time.time() * 1000),
    }
    _atomic_write_json(_NAV_INTENT_FILE, intent)
    return {
        "ok": True,
        "module_id": module_id,
        "course_id": course_id,
        "message": (
            f"Navigation intent written. The HTML Preview panel will open "
            f"module '{module_id}' if it's currently visible to the user."
        ),
    }


# ── Tool 5: course_scaffold ─────────────────────────────────────────────────


_MODULE_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title} — AI-Hydro Interactive Module</title>
<meta name="viewport" content="width=device-width, initial-scale=1">

<link rel="preconnect" href="https://fonts.googleapis.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Quicksand:wght@600;700&family=Poppins:wght@400;500;600;700&family=Nunito:wght@400;600;700&family=JetBrains+Mono&display=swap" rel="stylesheet">

<script type="application/vnd.aihydro.module+json">
{{
  "id": "{module_id}",
  "title": "{title}",
  "version": "0.1.0",
  "authors": [{{ "name": "{author_name}", "affiliation": "{author_affiliation}" }}],
  "license": "CC-BY-4.0",
  "topic": "{topic}",
  "level": "{level}",
  "estimated_minutes": {estimated_minutes},
  "requires": {{ "executable": {executable_lower}, "python": [] }},
  "ai_hydro_preview_min_version": "0.1"
}}
</script>

<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --blue:#00A3FF; --cyan:#00DDFF; --bright:#00FFFF;
  --bg:#0a0a15;   --navy:#1a1a2e; --mid:#0f0f1e;
  --text:#FFFFFF; --sub:#7dd3fc;  --muted:#94a3b8;
  --border:rgba(125,211,252,0.15);
  --shadow:0 24px 64px rgba(0,0,0,0.55);
}}
body {{
  font-family:'Nunito',system-ui,sans-serif; font-size:16px; line-height:1.65;
  color:var(--sub);
  background:
    radial-gradient(ellipse at 10% 0%,   rgba(0,163,255,0.18) 0%, transparent 45%),
    radial-gradient(ellipse at 90% 100%, rgba(0,221,255,0.10) 0%, transparent 40%),
    var(--bg);
  min-height:100vh;
}}
main {{ max-width:1160px; margin:0 auto; padding:32px 24px 80px; }}
h1 {{ font-family:'Quicksand',sans-serif; font-weight:700; color:var(--text);
     font-size:clamp(34px,5vw,56px); line-height:1.05; letter-spacing:-0.03em; }}
h2 {{ font-family:'Poppins',sans-serif; font-weight:600; color:var(--text);
     font-size:22px; letter-spacing:-0.01em; margin:24px 0 10px; }}
p  {{ color:var(--sub); margin-bottom:12px; }}
strong {{ color:var(--text); }}
code {{ font-family:'JetBrains Mono',monospace; font-size:0.88em;
       background:rgba(0,221,255,0.10); padding:2px 7px; border-radius:5px; color:var(--cyan); }}
.hero {{
  border:1px solid rgba(0,221,255,0.25);
  border-radius:28px; padding:36px 38px; margin-bottom:22px;
  background:linear-gradient(135deg,rgba(0,163,255,0.09) 0%,rgba(0,221,255,0.04) 100%);
}}
.card {{
  background:rgba(26,26,46,0.82);
  border:1px solid var(--border); border-radius:24px;
  padding:26px 28px; margin:18px 0; box-shadow:var(--shadow);
}}
.placeholder {{
  border:1px dashed rgba(125,211,252,0.35); border-radius:14px;
  padding:14px 18px; color:var(--muted); font-style:italic;
}}
</style>
</head>
<body>
<main>
  <section class="hero" data-aihydro-editable="prose">
    <h1>{title}</h1>
    <p>{abstract}</p>
  </section>

  <section class="card" data-aihydro-editable="prose">
    <h2>Overview</h2>
    <p class="placeholder">TODO — author the module overview. What concept does
    this module teach? Why is it important? What will the student be able to
    do by the end?</p>
  </section>

  <section class="card" data-aihydro-editable="prose">
    <h2>Concept</h2>
    <p class="placeholder">TODO — explain the core concept with worked
    examples. Embed an interactive figure or executable cell here when
    helpful.</p>
  </section>

  <section class="card" data-aihydro-editable="prose">
    <h2>Try it</h2>
    <p class="placeholder">TODO — give the student a small task that
    exercises the concept (executable cell, parameter to adjust, dataset
    to inspect).</p>
  </section>

  <section class="card" data-aihydro-editable="prose">
    <h2>Wrap-up</h2>
    <p class="placeholder">TODO — summarise what was learned and preview
    how it connects to the next module.</p>
  </section>
</main>
</body>
</html>
"""


def _slugify_id(s: str) -> str:
    """Lowercase, hyphenated, alnum-only id suitable for module ids + courseIds."""
    out = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip()).strip("-").lower()
    return out or "untitled"


def _detect_prereq_cycle(modules: list[dict]) -> list[str] | None:
    """Return the cycle as an ordered list of ids if any prereq cycle exists,
    else None. Uses iterative DFS (3-colour) for safety on large inputs."""
    graph: dict[str, list[str]] = {m["id"]: list(m.get("prerequisites") or []) for m in modules}
    WHITE, GRAY, BLACK = 0, 1, 2
    colour = {k: WHITE for k in graph}
    parent: dict[str, str | None] = {k: None for k in graph}
    for start in list(graph):
        if colour[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        parent[start] = None
        colour[start] = GRAY
        while stack:
            node, i = stack[-1]
            neighbours = graph.get(node, [])
            if i < len(neighbours):
                stack[-1] = (node, i + 1)
                nb = neighbours[i]
                if nb not in graph:
                    return [node, nb]  # prereq references unknown module
                if colour[nb] == GRAY:
                    # cycle: reconstruct from nb back to nb via parent chain
                    cycle = [nb, node]
                    p = parent[node]
                    while p and p != nb:
                        cycle.append(p)
                        p = parent[p]
                    cycle.append(nb)
                    cycle.reverse()
                    return cycle
                if colour[nb] == WHITE:
                    colour[nb] = GRAY
                    parent[nb] = node
                    stack.append((nb, 0))
            else:
                colour[node] = BLACK
                stack.pop()
    return None


@mcp.tool()
def course_scaffold(
    title: str,
    out_dir: str,
    modules: list[dict],
    course_id: str | None = None,
    abstract: str | None = None,
    estimated_hours: float | None = None,
    author_name: str = "Course Author",
    author_affiliation: str = "",
    topic: str = "hydrology",
    level: str = "intro",
    overwrite: bool = False,
) -> dict:
    """
    Scaffold a course on disk: course.json + one styled module.html per
    module (AI-Hydro design + edit-mode markers). Load the
    ``course-authoring`` skill first for the full workflow.

    ``modules`` items: {title (required), id?, prerequisites?,
    estimated_minutes?, abstract?, executable?}. Missing ids are
    auto-slugified. Order = curriculum order.

    Validates: required fields, duplicate ids, unknown prereq refs,
    prereq cycles. Returns error with ``cycle`` / ``conflicts`` /
    ``valid_module_ids`` to help fix the input.
    """
    if not title or not str(title).strip():
        return {"error": True, "message": "title is required."}
    if not modules or not isinstance(modules, list):
        return {"error": True, "message": "modules must be a non-empty list."}

    cid = course_id or _slugify_id(title)
    out = Path(out_dir).expanduser().resolve()

    # ── Normalise + validate modules ────────────────────────────────────
    normalised: list[dict] = []
    seen_ids: set[str] = set()
    for i, m in enumerate(modules):
        if not isinstance(m, dict):
            return {"error": True, "message": f"modules[{i}] must be an object."}
        mtitle = str(m.get("title") or "").strip()
        if not mtitle:
            return {"error": True, "message": f"modules[{i}].title is required."}
        mid = str(m.get("id") or _slugify_id(mtitle))
        if mid in seen_ids:
            return {
                "error": True,
                "message": f"duplicate module id '{mid}'.",
                "duplicate_id": mid,
            }
        seen_ids.add(mid)
        prereqs = m.get("prerequisites") or []
        if not isinstance(prereqs, list) or any(not isinstance(p, str) for p in prereqs):
            return {"error": True, "message": f"modules[{i}].prerequisites must be a list of strings."}
        normalised.append({
            "id": mid,
            "title": mtitle,
            "prerequisites": prereqs,
            "estimated_minutes": int(m.get("estimated_minutes") or m.get("estimatedMinutes") or 20),
            "abstract": str(m.get("abstract") or ""),
            "executable": bool(m.get("executable", True)),
            "folder": f"{i+1:02d}-{mid}",
        })

    # Verify all prereqs reference known module ids
    for nm in normalised:
        for p in nm["prerequisites"]:
            if p not in seen_ids:
                return {
                    "error": True,
                    "message": f"module '{nm['id']}' lists unknown prerequisite '{p}'.",
                    "valid_module_ids": sorted(seen_ids),
                }

    cycle = _detect_prereq_cycle(normalised)
    if cycle:
        return {
            "error": True,
            "message": "prerequisite cycle detected.",
            "cycle": cycle,
        }

    # ── Conflict check ─────────────────────────────────────────────────
    course_file = out / "course.json"
    targets = [course_file] + [
        out / nm["folder"] / "module.html" for nm in normalised
    ]
    if not overwrite:
        conflicts = [str(p) for p in targets if p.exists()]
        if conflicts:
            return {
                "error": True,
                "message": "target files already exist; pass overwrite=True to replace them.",
                "conflicts": conflicts,
            }

    # ── Write course.json ───────────────────────────────────────────────
    out.mkdir(parents=True, exist_ok=True)
    total_minutes = sum(nm["estimated_minutes"] for nm in normalised)
    course_payload: dict[str, Any] = {
        "courseId": cid,
        "title": title,
        "version": "0.1.0",
        "license": "CC-BY-4.0",
        "authors": [{
            "name": author_name,
            **({"affiliation": author_affiliation} if author_affiliation else {}),
        }],
        "abstract": abstract or "",
        "estimatedHours": estimated_hours if estimated_hours is not None else round(total_minutes / 60, 1),
        "modules": [
            {
                "id": nm["id"],
                "path": f"{nm['folder']}/module.html",
                "title": nm["title"],
                "abstract": nm["abstract"],
                "estimatedMinutes": nm["estimated_minutes"],
                **({"prerequisites": nm["prerequisites"]} if nm["prerequisites"] else {}),
            }
            for nm in normalised
        ],
    }
    _atomic_write_json(course_file, course_payload)

    # ── Write each module HTML skeleton ────────────────────────────────
    module_paths: list[str] = []
    for nm in normalised:
        folder = out / nm["folder"]
        folder.mkdir(parents=True, exist_ok=True)
        mhtml = _MODULE_HTML_TEMPLATE.format(
            title=_html_escape(nm["title"]),
            module_id=nm["id"],
            author_name=_json_escape(author_name),
            author_affiliation=_json_escape(author_affiliation),
            topic=_json_escape(topic),
            level=_json_escape(level),
            estimated_minutes=nm["estimated_minutes"],
            executable_lower=str(nm["executable"]).lower(),
            abstract=_html_escape(nm["abstract"] or "Module abstract — describe what the student will learn."),
        )
        module_file = folder / "module.html"
        module_file.write_text(mhtml, encoding="utf-8")
        module_paths.append(str(module_file))

    return {
        "ok": True,
        "course_id": cid,
        "course_path": str(course_file),
        "course_root": str(out),
        "module_paths": module_paths,
        "module_count": len(normalised),
        "next_step": (
            "Open course.json or any module.html in the HTML Preview panel to "
            "confirm the scaffold renders. Then use Read/Edit to author each "
            "module's body — replace .placeholder paragraphs with real content."
        ),
    }


def _html_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _json_escape(s: str) -> str:
    """Escape for safe embedding inside a JSON string in the manifest block."""
    return (
        (s or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
    )
