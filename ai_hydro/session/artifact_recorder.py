"""
Study recorder: every file a tool writes and every layer it pushes becomes an
``artifact_manifest`` entry of the study that owns it.

There is exactly one call site, in the run-record layer
(``run_records._record_call``), after the call's rows are stored. Tools do not record
anything themselves: the low-level helpers they already use (``HydroSession.write_workspace_file``
and the ``push_*`` layer functions) only *note* what happened on the call's capture.

An entry (key ``file:<path>`` or ``layer:<layer_id>``)::

    {"artifact_id": "file:watershed_01031500.geojson", "kind": "workspace_artifact",
     "type": "workspace_file" | "map_layer",
     "path": "watershed_01031500.geojson",    # workspace-relative, posix; None for an inline layer
     "layer_id": "watershed_01031500",        # layers only
     "sha256": "...", "size_bytes": 1234,
     "study_id": "01031500", "run_id": "<run row id>", "tool": "delineate_watershed",
     "recorded_at": "..."}

Layer entries also carry the envelope the layer was stamped with (``chat_id``, ``source``,
``retrieved_at``), which ``map_get_state`` reads to answer for a chat or a project.

``study/by-artifact`` (``ai_hydro.session.study_index``) reads these. Files written before the
recorder existed have no entry and are resolved by a declared name heuristic that returns
candidates only.

Not recorded: files written inside subagent shards (their manifest is merged with the shard),
and files a tool writes without going through the helpers above (they can call
``run_records.note_file_written`` themselves).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from ai_hydro.session.study_index import MANIFEST_KIND, sha256_file, study_exists

log = logging.getLogger("ai_hydro.session.artifact_recorder")

#: Files larger than this are listed without a digest (``sha256`` None, ``hash_deferred`` true).
MAX_HASH_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_ENTRIES = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel(path: str, workspace: str | None) -> str | None:
    if not workspace:
        return None
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(workspace))
    except ValueError:
        return None
    return None if rel.startswith("..") else rel.replace(os.sep, "/")


def _file_facts(path: str) -> dict[str, Any]:
    facts: dict[str, Any] = {"sha256": None, "size_bytes": None}
    try:
        size = os.path.getsize(path)
    except OSError:
        return facts
    facts["size_bytes"] = size
    if size > MAX_HASH_BYTES:
        facts["hash_deferred"] = True
    else:
        facts["sha256"] = sha256_file(path)
    return facts


def record_artifacts(capture: Any, *, tool: str, call_session_id: str | None, run_id: str | None) -> int:
    """Write the call's noted files and layers into the owning studies' manifests.

    Returns the number of entries written. Raises nothing the caller must handle beyond what
    ``HydroSession`` raises; ``run_records`` catches and counts it.
    """
    from ai_hydro.session import HydroSession

    # study id -> [(kind, key, builder-args)]
    work: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    seen_files: set[tuple[str, str]] = set()
    for study_id, path in capture.files:
        sid = study_id or call_session_id
        if not sid or (sid, path) in seen_files:
            continue
        seen_files.add((sid, path))
        work.setdefault(sid, []).append(("file", {"path": path}))
    for layer in capture.layers:
        sid = call_session_id
        if not sid:
            continue
        work.setdefault(sid, []).append(("layer", layer))
        for p in layer.get("paths", []):
            if (sid, p) not in seen_files:
                seen_files.add((sid, p))
                work[sid].append(("file", {"path": p}))

    written = 0
    for sid, items in work.items():
        if not study_exists(sid):
            continue                      # never resurrect a cleared or unknown study
        session = HydroSession.load(sid)
        if getattr(session, "shard_id", None):
            continue
        manifest = session.artifact_manifest
        ws = session.workspace_dir
        before = written
        for kind, spec in items:
            if kind == "file":
                path = spec["path"]
                rel = _rel(path, ws)
                if rel is None:
                    continue              # outside the study's workspace
                key = f"file:{rel}"
                entry = {"type": "workspace_file", "path": rel, **_file_facts(path)}
            else:
                key = f"layer:{spec['layer_id']}"
                entry = {"type": "map_layer", "path": None, "layer_id": spec["layer_id"],
                         "name": spec.get("name"), "layer_type": spec.get("layer_type"),
                         "sha256": spec.get("content_sha256"), "size_bytes": None}
                env = spec.get("envelope") or {}
                entry.update({k: env[k] for k in ("chat_id", "source", "retrieved_at") if env.get(k)})
                for p in spec.get("paths", []):
                    rel = _rel(p, ws)
                    if rel is not None:
                        entry["path"] = rel
                        entry.update({k: v for k, v in _file_facts(p).items() if k != "size_bytes"})
                        break
            entry.update({
                "artifact_id": key, "kind": MANIFEST_KIND, "study_id": sid,
                "run_id": run_id, "tool": tool, "recorded_at": _now(),
            })
            if key not in manifest and len(manifest) >= MAX_MANIFEST_ENTRIES:
                log.warning("artifact manifest of %s is full (%d); %s not recorded", sid, len(manifest), key)
                continue
            manifest[key] = entry
            written += 1
        if written > before:
            session.save()
    return written
