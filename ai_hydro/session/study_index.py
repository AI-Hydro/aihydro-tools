"""
Study index: labels, visibility, listing and artifact lookup, read from the session files raw.

A *study* is a HydroSession file. This module answers "which studies exist, what do we call
them, and which one owns this file" without ``HydroSession.load`` (a full load per study is
far too slow to list 50 of them): each file is parsed once and the few fields needed are cached
per ``(path, mtime_ns, size)``.

Labels (PSMH section 3.4), in priority order::

    1. a user-set label (``extra["study_label"]``)
    2. site name with a gauge alias:  "Piscataquis River Near Dover-Foxcroft, Maine (USGS 01031500)"
    3. the outlet:                    "Basin at 24.20°N, 75.75°E"
    4. the id, marked:                "<id> (unnamed study)"

A name that is only the study id (a slug) is not a name. No label is ever a bare id.

Visibility: a study is **hidden**, never deleted, when it is archived or when its id matches
``^test-`` / ``^e2e-proof-`` and it belongs to no workspace. Hidden studies are left out of the
resolver's candidates and of ``study/list`` unless ``include_hidden`` is set.

Artifact lookup resolves a workspace file to a study in two declared ways:

* ``manifest``: the study's ``artifact_manifest`` (written by the study recorder) names the file,
  and the entry's sha256 still matches the file on disk.
* ``artifact_name_heuristic``: for files written before the recorder existed. The file sits in a
  study's ``workspace_dir`` and its name contains one of the study's identifiers. This only ever
  returns **candidates**; the caller decides, nothing is bound.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("ai_hydro.session.study_index")

HIDDEN_ID_PATTERN = re.compile(r"^(test-|e2e-proof-)")
MANIFEST_KIND = "workspace_artifact"
RULE_MANIFEST = "manifest"
RULE_HEURISTIC = "artifact_name_heuristic"

_GAUGE_TOKEN = re.compile(r"\d{8}")

# (path, mtime_ns, size) -> summary dict
_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Raw reading
# ---------------------------------------------------------------------------

def _sessions_dir() -> Path:
    # Read the module attribute at call time: tests and callers patch it.
    from ai_hydro.session import store as _store

    return Path(os.fspath(_store._SESSIONS_DIR))


def norm_path(path: str | os.PathLike | None) -> str | None:
    if not path:
        return None
    try:
        return os.path.realpath(os.path.expanduser(os.fspath(path)))
    except Exception:
        return None


def _outlet_of(raw: dict) -> tuple[float, float] | None:
    """Outlet (lat, lon) from the stored watershed slot, if any (raw 3-level slot shape)."""
    ws = raw.get("watershed")
    stack = [ws]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else node
        for lat_k, lon_k in (("outlet_lat", "outlet_lon"), ("gauge_lat", "gauge_lon")):
            lat, lon = data.get(lat_k), data.get(lon_k)
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                return float(lat), float(lon)
        stack.extend(v for v in node.values() if isinstance(v, dict))
    return None


def _gauge_name_of(raw: dict) -> str:
    stack = [raw.get("watershed")]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else node
        name = data.get("gauge_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        stack.extend(v for v in node.values() if isinstance(v, dict))
    return ""


def make_label(session_id: str, raw: dict) -> str:
    """The display label of a study (never a bare id). See the module docstring."""
    extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
    user = extra.get("study_label")
    if isinstance(user, str) and user.strip():
        return user.strip()

    site_id = str(raw.get("site_id") or raw.get("gauge_id") or "").strip()
    name = str(raw.get("site_name") or "").strip()
    if name and name in (session_id, site_id):
        name = ""                       # a slug is not a name
    if not name:
        name = _gauge_name_of(raw)
    if name:
        if site_id and site_id not in name:
            kind = "USGS " if str(raw.get("site_type") or "") == "usgs_gauge" else ""
            return f"{name} ({kind}{site_id})"
        return name

    outlet = _outlet_of(raw)
    if outlet:
        lat, lon = outlet
        return f"Basin at {abs(lat):.2f}°{'N' if lat >= 0 else 'S'}, {abs(lon):.2f}°{'E' if lon >= 0 else 'W'}"
    if site_id and site_id != session_id:
        return f"USGS gauge {site_id}" if str(raw.get("site_type") or "") == "usgs_gauge" else f"Site {site_id}"
    return f"{session_id} (unnamed study)"


def hidden_reason(session_id: str, raw: dict) -> str | None:
    """Why a study is hidden by default, or ``None`` when it is shown."""
    if raw.get("archived"):
        return "archived"
    if HIDDEN_ID_PATTERN.match(session_id) and not raw.get("workspace_dir"):
        return "test_study"
    return None


def _summarise(path: Path, raw: dict) -> dict[str, Any]:
    sid = str(raw.get("session_id") or path.stem)
    manifest = raw.get("artifact_manifest") if isinstance(raw.get("artifact_manifest"), dict) else {}
    keys = {sid}
    site_id = str(raw.get("site_id") or raw.get("gauge_id") or "").strip()
    if site_id:
        keys.add(site_id)
    keys.update(_GAUGE_TOKEN.findall(sid))
    return {
        "study_id": sid,
        "label": make_label(sid, raw),
        "site_id": site_id or None,
        "workspace_dir": raw.get("workspace_dir") or None,
        "workspace_norm": norm_path(raw.get("workspace_dir")),
        "archived": bool(raw.get("archived")),
        "hidden_reason": hidden_reason(sid, raw),
        "last_run": raw.get("updated_at") or raw.get("created_at"),
        "name_keys": sorted(k for k in keys if len(k) >= 3),
        "manifest": {k: v for k, v in manifest.items()
                     if isinstance(v, dict) and v.get("kind") == MANIFEST_KIND},
    }


def study_summaries(*, include_hidden: bool = False, workspace: str | None = None) -> list[dict[str, Any]]:
    """Summaries of every study file, newest ``last_run`` first.

    ``workspace`` keeps only studies whose ``workspace_dir`` is that directory. Shard files
    (``*.shard.json``), run-log databases and ``.data.`` slot files are not studies.
    """
    sdir = _sessions_dir()
    if not sdir.is_dir():
        return []
    want = norm_path(workspace) if workspace else None
    out: list[dict[str, Any]] = []
    for path in sdir.glob("*.json"):
        name = path.name
        if ".shard." in name or ".data." in name or name.startswith("_"):
            continue
        try:
            st = path.stat()
            sig = (st.st_mtime_ns, st.st_size)
            hit = _CACHE.get(str(path))
            if hit and hit[0] == sig:
                summary = hit[1]
            else:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                summary = _summarise(path, raw)
                _CACHE[str(path)] = (sig, summary)
        except Exception as exc:
            log.debug("study index skipped %s: %s", name, exc)
            continue
        if summary["hidden_reason"] and not include_hidden:
            continue
        if want is not None and summary["workspace_norm"] != want:
            continue
        out.append(summary)
    out.sort(key=lambda s: (str(s["last_run"] or ""), s["study_id"]), reverse=True)
    return out


def candidate(summary: dict[str, Any]) -> dict[str, Any]:
    """The public candidate shape: ``{study_id, label, last_run}``."""
    return {"study_id": summary["study_id"], "label": summary["label"], "last_run": summary["last_run"]}


def candidates_for(workspace: str | None, limit: int = 5) -> tuple[list[dict[str, Any]], str]:
    """``(candidates, scope)`` for a resolution error: the workspace's studies first, else the
    most recently active visible studies (offered only, never chosen)."""
    if workspace:
        here = study_summaries(workspace=workspace)
        if here:
            return [candidate(s) for s in here[:limit]], "workspace"
    return [candidate(s) for s in study_summaries()[:limit]], "recent"


def study_exists(study_id: str) -> bool:
    from ai_hydro.session import store as _store

    try:
        return (_sessions_dir() / f"{_store._safe_filename_component(study_id)}.json").exists()
    except Exception:
        return False


def label_of(study_id: str) -> str:
    """Label of one study by id (``"<id> (unnamed study)"`` when the file is missing)."""
    from ai_hydro.session import store as _store

    try:
        path = _sessions_dir() / f"{_store._safe_filename_component(study_id)}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return make_label(study_id, raw)
    except Exception:
        pass
    return f"{study_id} (unnamed study)"


# ---------------------------------------------------------------------------
# Artifact lookup
# ---------------------------------------------------------------------------

def sha256_file(path: str | os.PathLike) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _rel_to(path_norm: str, workspace_norm: str | None) -> str | None:
    if not workspace_norm:
        return None
    try:
        rel = os.path.relpath(path_norm, workspace_norm)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/")


def by_artifact(path: str, workspace: str | None = None) -> dict[str, Any]:
    """Resolve a workspace file to a study.

    Returns ``{path, resolved, study_id, rule, candidates}``. ``resolved`` is True only for the
    ``manifest`` rule with exactly one study; the heuristic returns candidates and never resolves.
    """
    result: dict[str, Any] = {"path": path, "resolved": False, "study_id": None, "rule": None, "candidates": []}
    if not isinstance(path, str) or not path.strip():
        result["error"] = "path is required"
        return result
    want_ws = norm_path(workspace) if workspace else None
    if os.path.isabs(os.path.expanduser(path)):
        file_norm = norm_path(path)
    elif want_ws:
        file_norm = norm_path(os.path.join(want_ws, path))
    else:
        file_norm = None
    rel_given = None if os.path.isabs(os.path.expanduser(path)) else path.replace(os.sep, "/").removeprefix("./")
    current_sha = sha256_file(file_norm) if file_norm else None

    studies = study_summaries(include_hidden=True)

    # 1. manifest
    matches: list[dict[str, Any]] = []
    stale = False
    for s in studies:
        if want_ws and s["workspace_norm"] != want_ws:
            continue
        rel = _rel_to(file_norm, s["workspace_norm"]) if file_norm else rel_given
        if rel is None:
            continue
        for entry in s["manifest"].values():
            if entry.get("path") != rel:
                continue
            if current_sha and entry.get("sha256") and entry["sha256"] != current_sha:
                stale = True        # the file was rewritten by another run or study since
                continue
            matches.append(s)
            break
    if matches:
        result["rule"] = RULE_MANIFEST
        result["candidates"] = [candidate(s) for s in matches]
        if len(matches) == 1:
            result["resolved"] = True
            result["study_id"] = matches[0]["study_id"]
        return result
    if stale:
        result["stale_manifest"] = True

    # 2. heuristic: same directory as a study's workspace, filename contains one of its identifiers
    if file_norm:
        folder = os.path.dirname(file_norm)
        name = os.path.basename(file_norm).lower()
    elif want_ws and rel_given:
        folder = want_ws
        name = os.path.basename(rel_given).lower()
    else:
        return result
    found = [s for s in study_summaries() if s["workspace_norm"] == folder
             and any(k.lower() in name for k in s["name_keys"])]
    if found:
        result["rule"] = RULE_HEURISTIC
        result["candidates"] = [candidate(s) for s in found]
    return result
