"""
Project registry v2: ``prj_<ULID>`` projects, one per workspace directory.

A *project* is a directory plus the studies researched in it. The link project -> study is a
relation, not containment: a study may belong to several projects. The backend mints the id (the
client never does).

Storage (``$AIHYDRO_HOME`` or ``~/.aihydro``)::

    projects/<prj_id>/project.json        schema "aihydro.project/2" (atomic write, revision checked)
    projects/<prj_id>/project.json.lock   cross-process lock for that project
    projects/.registry.lock               serialises creation (no two projects for one directory)

Slug-keyed legacy projects (``ProjectSession``, ``projects/<slug>/project.json``) are not touched
and never listed here: only ``prj_`` directories whose file carries the v2 schema are projects.

Writes
------
Every change is a read-modify-write under the project's ``file_lock`` with the temp + fsync +
rename helper the binding store uses. ``revision`` (an integer, +1 per write) makes a lost update
detectable: a caller that read revision N passes ``expected_revision=N`` and gets
``RevisionConflict`` when another writer got in first. A caller that passes none is serialised by
the lock and always lands on top of the latest state. An unreadable ``project.json`` is never
overwritten: reads skip it with a warning, writes refuse.

Default study (B4)
------------------
``default_study_id`` may be a *suggestion* (set by the M4 migration, or by an auto-create into a
project that had none) or *confirmed* (``default_confirmed_by`` is ``user`` or ``host_launch``).
Only a confirmed default resolves a study in the session chain; a suggestion is offered as a
candidate and nothing else.

Duplicates (D8)
---------------
Studies of one project that are about the same site (equal ``alias_key``, the slugified site id)
are *grouped when read*. Nothing is merged, renamed or deleted; run logs stay append-only.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ai_hydro.registry.ids import PROJECT_PREFIX, is_project_id, mint_project_id
from ai_hydro.registry.locking import atomic_write_text, file_lock

log = logging.getLogger("ai_hydro.session.project_registry")

SCHEMA = "aihydro.project/2"
PROJECT_FILE = "project.json"
CONFIRMERS = frozenset({"user", "host_launch"})
ADDED_BY = frozenset({"auto", "user", "migration"})
TITLE_SOURCES = frozenset({"auto", "fallback", "user"})
MAX_TITLE = 120


class ProjectRegistryError(Exception):
    """Base class; ``code`` is the stable machine-readable reason."""

    code = "PROJECT_ERROR"


class ProjectNotFound(ProjectRegistryError):
    code = "PROJECT_NOT_FOUND"


class ProjectUnreadable(ProjectRegistryError):
    code = "PROJECT_UNREADABLE"


class RootDirError(ProjectRegistryError):
    code = "ROOT_DIR_INVALID"


class StudyNotFound(ProjectRegistryError):
    code = "STUDY_NOT_FOUND"


class InvalidRequest(ProjectRegistryError):
    code = "INVALID_REQUEST"


class RevisionConflict(ProjectRegistryError):
    """``expected_revision`` no longer matches the file: another writer changed the project."""

    code = "REVISION_CONFLICT"

    def __init__(self, expected: int, current: dict[str, Any]) -> None:
        super().__init__(f"project is at revision {current.get('revision')}, expected {expected}")
        self.expected = expected
        self.current = current


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def dir_key(path: str | os.PathLike | None) -> str | None:
    """Comparison key of a directory: its realpath, case-folded where the filesystem usually is.

    ``None`` for an empty or unresolvable path. The key is for comparing; the record keeps the
    realpath as given by the first ``ensure_project``.
    """
    if not path:
        return None
    try:
        real = os.path.realpath(os.path.expanduser(os.fspath(path)))
    except Exception:
        return None
    return real.casefold() if sys.platform in ("darwin", "win32") else real


def _humanise(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").strip() or name


class ProjectRegistry:
    """File-backed registry of ``prj_`` projects. Cheap to construct; holds no state."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else None

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    @property
    def projects_dir(self) -> Path:
        if self._root is not None:
            return self._root
        from ai_hydro.registry.paths import aihydro_home

        return aihydro_home() / "projects"

    def project_dir(self, project_id: str) -> Path:
        if not is_project_id(project_id):
            raise InvalidRequest(f"{project_id!r} is not a project id (expected prj_<ULID>)")
        return self.projects_dir / project_id

    def project_file(self, project_id: str) -> Path:
        return self.project_dir(project_id) / PROJECT_FILE

    def _project_lock(self, project_id: str):
        return file_lock(self.project_dir(project_id) / (PROJECT_FILE + ".lock"))

    def _registry_lock(self):
        return file_lock(self.projects_dir / ".registry.lock")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _read(self, project_id: str, *, strict: bool) -> dict[str, Any] | None:
        """The parsed record, ``None`` when absent. Unreadable or foreign: raise when ``strict``, else ``None``."""
        path = self.project_file(project_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("project_id") != project_id:
                raise ValueError("not an aihydro.project/2 record for this id")
        except Exception as exc:
            if strict:
                raise ProjectUnreadable(f"{path.name} of {project_id} is unreadable ({exc}); it was not changed") from exc
            log.warning("%s of %s unreadable (%s); skipped", PROJECT_FILE, project_id, exc)
            return None
        data.setdefault("revision", 0)
        data.setdefault("studies", [])
        data.setdefault("root_dir_aliases", [])
        return data

    def get(self, project_id: str) -> dict[str, Any] | None:
        """The record of ``project_id``, or ``None``."""
        if not is_project_id(project_id):
            return None
        return self._read(project_id, strict=False)

    def list_all(self) -> list[dict[str, Any]]:
        """Every readable v2 project, oldest first."""
        base = self.projects_dir
        if not base.is_dir():
            return []
        out = []
        for d in base.iterdir():
            if d.is_dir() and is_project_id(d.name):
                rec = self._read(d.name, strict=False)
                if rec is not None:
                    out.append(rec)
        out.sort(key=lambda r: (str(r.get("created_at", "")), r["project_id"]))
        return out

    def find_by_dir(self, path: str | os.PathLike | None) -> dict[str, Any] | None:
        """The project whose ``root_dir`` or ``root_dir_aliases`` is ``path`` (realpath, case-normalised)."""
        want = dir_key(path)
        if want is None:
            return None
        for rec in self.list_all():
            roots = [rec.get("root_dir"), *rec.get("root_dir_aliases", [])]
            if any(dir_key(r) == want for r in roots if r):
                return rec
        return None

    def moved_candidates(self, path: str | os.PathLike | None, limit: int = 3) -> list[dict[str, Any]]:
        """Projects that may have moved to ``path``. Only offered, never attached automatically.

        A project qualifies when its root is gone from disk and either a study it holds lives in
        ``path`` (``reason`` = ``study_workspace_dir``) or the vanished root had the same folder name
        (``reason`` = ``same_folder_name``; a folder moved to another parent).
        """
        want = dir_key(path)
        if want is None:
            return []
        from ai_hydro.session.study_index import study_summaries

        here = {s["study_id"] for s in study_summaries(include_hidden=True) if dir_key(s["workspace_dir"]) == want}
        name = os.path.basename(want.rstrip(os.sep))
        out = []
        for rec in self.list_all():
            root = rec.get("root_dir")
            if not root or Path(root).is_dir():
                continue
            if here & {s.get("study_id") for s in rec["studies"]}:
                reason = "study_workspace_dir"
            elif os.path.basename((dir_key(root) or "").rstrip(os.sep)) == name:
                reason = "same_folder_name"
            else:
                continue
            out.append({"project_id": rec["project_id"], "title": rec.get("title"), "root_dir": root, "reason": reason})
        return out[:limit]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def _write(self, rec: dict[str, Any]) -> dict[str, Any]:
        """Bump revision/updated_at and write atomically. The caller holds the project lock."""
        rec["schema"] = SCHEMA
        rec["revision"] = int(rec.get("revision", 0)) + 1
        rec["updated_at"] = _now()
        atomic_write_text(self.project_file(rec["project_id"]),
                          json.dumps(rec, indent=2, ensure_ascii=False), ".project_")
        return rec

    def mutate(self, project_id: str, fn: Callable[[dict[str, Any]], bool | None], *,
               expected_revision: int | None = None) -> tuple[dict[str, Any], bool]:
        """Read-modify-write ``project_id`` under its lock. Returns ``(record, changed)``.

        ``fn`` edits the record in place and returns False when it made no change (then nothing is
        written and the revision stays). ``expected_revision`` is the revision the caller last read.
        """
        d = self.project_dir(project_id)
        if not d.is_dir():
            raise ProjectNotFound(f"no project {project_id}")
        with self._project_lock(project_id):
            rec = self._read(project_id, strict=True)
            if rec is None:
                raise ProjectNotFound(f"no project {project_id}")
            if expected_revision is not None and int(expected_revision) != int(rec["revision"]):
                raise RevisionConflict(int(expected_revision), rec)
            if fn(rec) is False:
                return rec, False
            return self._write(rec), True

    # ------------------------------------------------------------------
    # ensure / create
    # ------------------------------------------------------------------

    def ensure(self, root_dir: str | os.PathLike, *, attach_to: str | None = None) -> tuple[dict[str, Any], str]:
        """The project of ``root_dir``, created when there is none. Returns ``(record, how)``.

        ``how`` is ``found``, ``created`` or ``reattached``. ``attach_to`` names an existing project
        that moved here: its previous root becomes a ``root_dir_aliases`` entry and ``root_dir`` the
        new directory (refused when another project already owns this directory).
        """
        if not root_dir or not os.path.isabs(os.path.expanduser(os.fspath(root_dir))):
            raise RootDirError("root_dir must be an absolute directory path")
        real = os.path.realpath(os.path.expanduser(os.fspath(root_dir)))
        if not os.path.isdir(real):
            raise RootDirError(f"root_dir is not an existing directory: {real}")
        key = dir_key(real)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        with self._registry_lock():
            existing = self.find_by_dir(real)
            if attach_to is None:
                if existing is not None:
                    return existing, "found"
                return self._create(real), "created"
            if existing is not None and existing["project_id"] != attach_to:
                raise InvalidRequest(
                    f"{real} already belongs to project {existing['project_id']}; it cannot be attached to {attach_to}")
            if existing is not None:
                return existing, "found"

            def attach(rec: dict[str, Any]) -> None:
                old = rec.get("root_dir")
                if old and dir_key(old) != key and old not in rec["root_dir_aliases"]:
                    rec["root_dir_aliases"].append(old)
                rec["root_dir"] = real
                rec["root_dir_aliases"] = [a for a in rec["root_dir_aliases"] if dir_key(a) != key]

            rec, _changed = self.mutate(attach_to, attach)
            return rec, "reattached"

    def _create(self, real_root: str) -> dict[str, Any]:
        """Mint, populate from the studies already in the directory (M3/M4), write. Registry lock held."""
        pid = mint_project_id()
        now = _now()
        linked = self._studies_in_dir(real_root)
        rec: dict[str, Any] = {
            "schema": SCHEMA,
            "project_id": pid,
            "root_dir": real_root,
            "root_dir_aliases": [],
            "title": _humanise(Path(real_root).name),
            "description": "",
            "title_source": "fallback",
            "studies": [{"study_id": s["study_id"], "added_by": "migration", "added_at": now} for s in linked],
            "default_study_id": linked[0]["study_id"] if linked else None,
            "default_confirmed_by": None,       # a suggestion until a person confirms it (B4, D3)
            "memory_scope": "project_only",
            "legacy_slug": None,
            "migration": {"m4_linked": len(linked), "ran_at": now} if linked else None,
            "revision": 0,
            "created_at": now,
        }
        self.project_dir(pid).mkdir(parents=True, exist_ok=True)
        with self._project_lock(pid):
            return self._write(rec)

    @staticmethod
    def _studies_in_dir(real_root: str) -> list[dict[str, Any]]:
        """Visible studies whose ``workspace_dir`` is the directory, most recently active first (M3/M4).

        M3 asks for the studies a chat is bound to; the backend does not know a chat's cwd, so a
        binding is matched through the study's own ``workspace_dir``, which is this same set. The
        extension (M1) knows each chat's cwd and links the rest with ``add_project_study``.
        """
        from ai_hydro.session.study_index import study_summaries

        want = dir_key(real_root)
        return [s for s in study_summaries() if dir_key(s["workspace_dir"]) == want]

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def rename(self, project_id: str, title: str, *, expected_revision: int | None = None,
               title_source: str = "user") -> dict[str, Any]:
        title = " ".join(str(title or "").split())
        if not title:
            raise InvalidRequest("title must not be empty")
        if len(title) > MAX_TITLE:
            raise InvalidRequest(f"title is {len(title)} characters; the limit is {MAX_TITLE}")
        if is_project_id(title) or title.startswith(PROJECT_PREFIX):
            raise InvalidRequest("a project title is never a bare id")
        if title_source not in TITLE_SOURCES:
            raise InvalidRequest(f"title_source must be one of {sorted(TITLE_SOURCES)}")

        def edit(rec: dict[str, Any]) -> bool:
            if rec.get("title") == title and rec.get("title_source") == title_source:
                return False
            rec["title"], rec["title_source"] = title, title_source
            return True

        return self.mutate(project_id, edit, expected_revision=expected_revision)[0]

    def add_study(self, project_id: str, study_id: str, *, added_by: str = "user",
                  expected_revision: int | None = None, suggest_default: bool = False,
                  require_exists: bool = True) -> tuple[dict[str, Any], bool]:
        """Link a study to the project. Returns ``(record, newly_added)``.

        ``suggest_default`` makes the study the (unconfirmed) default when the project has none.
        """
        study_id = str(study_id or "").strip()
        if not study_id:
            raise InvalidRequest("study_id must not be empty")
        if added_by not in ADDED_BY:
            raise InvalidRequest(f"added_by must be one of {sorted(ADDED_BY)}")
        if require_exists:
            from ai_hydro.session.study_index import study_exists

            if not study_exists(study_id):
                raise StudyNotFound(f"no study {study_id!r} exists; nothing was added")
        added = False

        def edit(rec: dict[str, Any]) -> bool:
            nonlocal added
            changed = False
            if not any(s.get("study_id") == study_id for s in rec["studies"]):
                rec["studies"].append({"study_id": study_id, "added_by": added_by, "added_at": _now()})
                added = changed = True
            if suggest_default and not rec.get("default_study_id"):
                rec["default_study_id"], rec["default_confirmed_by"] = study_id, None
                changed = True
            return changed

        rec, _changed = self.mutate(project_id, edit, expected_revision=expected_revision)
        return rec, added

    def remove_study(self, project_id: str, study_id: str, *,
                     expected_revision: int | None = None) -> tuple[dict[str, Any], bool]:
        """Unlink a study (the study itself is untouched). Removing the default clears it and its confirmation."""
        removed = False

        def edit(rec: dict[str, Any]) -> bool:
            nonlocal removed
            kept = [s for s in rec["studies"] if s.get("study_id") != study_id]
            if len(kept) == len(rec["studies"]):
                return False
            rec["studies"] = kept
            removed = True
            if rec.get("default_study_id") == study_id:
                rec["default_study_id"], rec["default_confirmed_by"] = None, None
            return True

        rec, _changed = self.mutate(project_id, edit, expected_revision=expected_revision)
        return rec, removed

    def set_default(self, project_id: str, study_id: str | None, *, confirmed_by: str | None = "user",
                    expected_revision: int | None = None) -> dict[str, Any]:
        """Set (and confirm) the default study; ``study_id=None`` clears it.

        The study must exist; it is linked to the project when it is not yet (``added_by`` = ``user``).
        ``confirmed_by`` is ``user`` or ``host_launch``; only a confirmed default resolves.
        """
        if study_id is None:
            def clear(rec: dict[str, Any]) -> bool:
                if rec.get("default_study_id") is None and rec.get("default_confirmed_by") is None:
                    return False
                rec["default_study_id"], rec["default_confirmed_by"] = None, None
                return True

            return self.mutate(project_id, clear, expected_revision=expected_revision)[0]
        if confirmed_by not in CONFIRMERS:
            raise InvalidRequest(f"confirmed_by must be one of {sorted(CONFIRMERS)}")
        study_id = str(study_id).strip()
        from ai_hydro.session.study_index import study_exists

        if not study_exists(study_id):
            raise StudyNotFound(f"no study {study_id!r} exists; the default was not changed")

        def edit(rec: dict[str, Any]) -> bool:
            changed = False
            if not any(s.get("study_id") == study_id for s in rec["studies"]):
                rec["studies"].append({"study_id": study_id, "added_by": "user", "added_at": _now()})
                changed = True
            if rec.get("default_study_id") != study_id or rec.get("default_confirmed_by") != confirmed_by:
                rec["default_study_id"], rec["default_confirmed_by"] = study_id, confirmed_by
                changed = True
            return changed

        return self.mutate(project_id, edit, expected_revision=expected_revision)[0]

    def note_study_bound(self, workspace: str | None, study_id: str, project_id: str | None = None) -> dict[str, Any] | None:
        """A chat was bound to ``study_id``: add it to the chat's project (created for the workspace if needed).

        ``added_by`` is ``auto``. A project with no default gets this study as an *unconfirmed* suggestion.
        Returns the record, or ``None`` when there is no project to put it in (no valid workspace).
        """
        rec = self.get(project_id) if project_id else None
        created = False
        if rec is None and workspace and os.path.isdir(os.path.expanduser(workspace)):
            rec, how = self.ensure(workspace)
            created = how == "created"
        if rec is None:
            return None
        rec, _added = self.add_study(rec["project_id"], study_id, added_by="auto", suggest_default=True,
                                     require_exists=False)
        if created:
            # The project was made by this very bind, so the creation sweep (M4) found the study only because
            # the bind had just stamped its workspace_dir: it came in through the chat, not the migration.
            def relabel(r: dict[str, Any]) -> bool:
                link = next((s for s in r["studies"] if s.get("study_id") == study_id), None)
                if link is None or link.get("added_by") == "auto":
                    return False
                link["added_by"] = "auto"
                return True

            rec, _changed = self.mutate(rec["project_id"], relabel)
        return rec

    # ------------------------------------------------------------------
    # Default study (resolver)
    # ------------------------------------------------------------------

    @staticmethod
    def is_confirmed(rec: dict[str, Any]) -> bool:
        return bool(rec.get("default_study_id")) and rec.get("default_confirmed_by") in CONFIRMERS

    def confirmed_default(self, *, workspace: str | None = None, project_id: str | None = None) -> tuple[str, str] | None:
        """``(study_id, project_id)`` of the CONFIRMED default of the call's project, else ``None``.

        An unconfirmed suggestion, a default that is not a member of the project, and a default whose
        study is gone or hidden (archived, test) never resolve.
        """
        rec = self.get(project_id) if project_id else None
        if rec is None and workspace:
            rec = self.find_by_dir(workspace)
        if rec is None or not self.is_confirmed(rec):
            return None
        sid = rec["default_study_id"]
        if not any(s.get("study_id") == sid for s in rec["studies"]):
            return None
        from ai_hydro.session.study_index import study_summaries

        if not any(s["study_id"] == sid for s in study_summaries()):
            return None
        return sid, rec["project_id"]

    def suggested_default(self, *, workspace: str | None = None, project_id: str | None = None) -> dict[str, Any] | None:
        """The UNCONFIRMED default as a candidate row ``{study_id, label, last_run, suggested_default}``, else ``None``."""
        rec = self.get(project_id) if project_id else None
        if rec is None and workspace:
            rec = self.find_by_dir(workspace)
        if rec is None or not rec.get("default_study_id") or self.is_confirmed(rec):
            return None
        from ai_hydro.session.study_index import candidate, study_summaries

        sid = rec["default_study_id"]
        row = next((s for s in study_summaries() if s["study_id"] == sid), None)
        if row is None:
            return None
        return {**candidate(row), "suggested_default": True, "project_id": rec["project_id"]}

    # ------------------------------------------------------------------
    # Read-side view (resources, tool results)
    # ------------------------------------------------------------------

    def view(self, rec: dict[str, Any]) -> dict[str, Any]:
        """The record with computed labels and alias groups. Reads the study index only (no session loads)."""
        from ai_hydro.session.study_index import study_summaries

        index = {s["study_id"]: s for s in study_summaries(include_hidden=True)}
        rows: list[dict[str, Any]] = []
        for link in rec["studies"]:
            sid = link.get("study_id")
            s = index.get(sid)
            row: dict[str, Any] = {
                "study_id": sid,
                "label": s["label"] if s else f"{sid} (unnamed study)",
                "added_by": link.get("added_by"),
                "added_at": link.get("added_at"),
                "last_run": s["last_run"] if s else None,
                "site_id": s["site_id"] if s else None,
                "alias_key": s["alias_key"] if s else None,
                "exists": s is not None,
            }
            if s and s["hidden_reason"]:
                row["hidden"] = s["hidden_reason"]
            rows.append(row)
        rows.sort(key=lambda r: (str(r["last_run"] or ""), str(r["study_id"])), reverse=True)

        groups: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            if r["alias_key"] and r["exists"] and not r.get("hidden"):
                groups.setdefault(r["alias_key"], []).append(r)
        duplicates = []
        for key, members in sorted(groups.items()):
            if len(members) < 2:
                continue
            for m in members:
                m["alias_group"] = key
            site = members[0]["site_id"] or key
            duplicates.append({
                "alias_key": key,
                "n_studies": len(members),
                "study_ids": [m["study_id"] for m in members],
                "primary_study_id": members[0]["study_id"],      # most recently active; a pointer, not a merge
                "summary": f"{len(members)} studies for {site}",
                "merged": False,
            })

        default_id = rec.get("default_study_id")
        default = None
        if default_id:
            d = index.get(default_id)
            default = {
                "study_id": default_id,
                "label": d["label"] if d else f"{default_id} (unnamed study)",
                "confirmed": self.is_confirmed(rec),
                "confirmed_by": rec.get("default_confirmed_by"),
            }
        out = {k: rec.get(k) for k in (
            "schema", "project_id", "root_dir", "root_dir_aliases", "title", "title_source", "description",
            "memory_scope", "legacy_slug", "revision", "created_at", "updated_at")}
        out.update({
            "studies": rows,
            "n_studies": len(rows),
            "default_study": default,
            "duplicate_groups": duplicates,
        })
        return out


_registry: ProjectRegistry | None = None


def get_project_registry() -> ProjectRegistry:
    """The process-wide registry (it resolves ``AIHYDRO_HOME`` on every call, so tests can move it)."""
    global _registry
    if _registry is None:
        _registry = ProjectRegistry()
    return _registry
