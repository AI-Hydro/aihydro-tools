"""
Chat <-> Study binding store (v2).

Many chats may be bound to one study; a chat is bound to at most one study.
There is no stored inverse: the chats of a study are derived from the forward
map, so binding a second chat to a study never steals the first chat's binding.

Storage (``$AIHYDRO_HOME`` or ``~/.aihydro``; atomic write-then-rename):

* ``chat_bindings.v2.json`` - the authority, schema 2::

    {
      "schema": 2,
      "bindings": {"01H...XYZ": {"study_id": "basin_26p9_78p1",
                                  "bound_at": "2026-10-28T00:00:00+00:00",
                                  "bound_by": "host"}, ...},
      "meta": {"imported_v1": "2026-10-28T00:00:00+00:00", ...}
    }

* ``chat_studies.json`` - the v1 file. It is read **once**, to seed v2 when v2
  does not exist yet (the original is first copied to ``chat_studies.json.v1.bak``).
  After that it is never read again. For one release it is rewritten as a
  derived v1-shaped projection (``chat_to_study`` / ``study_to_chat`` / ``meta``,
  active studies only) so an installed extension that reads ``chat_to_study``
  directly keeps its header chip. A v1 write by another (older) server after the
  import does not change what v2 resolves.

Chat ids must be ULIDs (the extension's task ids) and must not equal a study id;
a study id passed as a chat id was how the live store ended up with a
``basin_x -> basin_x`` entry.

Public API::

    store = ChatBindingStore()
    store.bind("01H...XYZ", "basin_26p9_78p1", bound_by="host")  # -> True if changed
    store.lookup_study("01H...XYZ")        # -> "basin_26p9_78p1" or None
    store.lookup_chats("basin_26p9_78p1")  # -> ["01H...XYZ", ...]
    store.lookup_chat("basin_26p9_78p1")   # -> most recently bound chat or None (v1 compat)
    store.unbind_chat("01H...XYZ")
    store.unbind_study("basin_26p9_78p1")  # unbinds every chat of the study
    store.all_bindings()                   # -> [(chat_id, study_id), ...]
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ai_hydro.session.chat_binding")

_BINDING_NAME = "chat_bindings.v2.json"
_V1_NAME = "chat_studies.json"
_V1_BACKUP_SUFFIX = ".v1.bak"

SCHEMA_VERSION = 2

#: Crockford base32, 26 characters, first character 0-7 (a 48-bit timestamp fits).
_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$", re.IGNORECASE)

_LOCK = threading.RLock()


def is_valid_chat_id(chat_id: Any) -> bool:
    """True for a ULID string (the form of every extension chat/task id)."""
    return isinstance(chat_id, str) and bool(_ULID_RE.match(chat_id))


class InvalidChatIdError(ValueError):
    """The chat id is not a ULID, or it is a study id."""


def _binding_file() -> Path:
    """Resolve the v2 binding file when requested, honouring ``AIHYDRO_HOME``."""
    from ai_hydro.registry.paths import aihydro_home

    return aihydro_home() / _BINDING_NAME


def _empty() -> dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "bindings": {}, "meta": {}}


_COMPACT_THRESHOLD = 500  # compact when > N bindings


class ChatBindingStore:
    """
    Persistent store for chat -> study bindings (many chats to one study).

    Uses atomic write-then-rename so the file is never half-written. *path* is
    the v2 file; the v1 file (import source and projection target) is its
    sibling ``chat_studies.json``.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _binding_file()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def v1_path(self) -> Path:
        return self._path.with_name(_V1_NAME)

    # ------------------------------------------------------------------
    # Internal I/O helpers
    # ------------------------------------------------------------------

    @property
    def lock_path(self) -> Path:
        return self._path.with_name(self._path.name + ".lock")

    def _lock(self):
        from ai_hydro.registry.locking import file_lock

        return file_lock(self.lock_path)

    def _read_v2(self, *, quarantine: bool) -> dict[str, Any] | None:
        """The parsed v2 file, ``None`` when absent. A file that cannot be parsed is never
        overwritten: with ``quarantine`` (caller holds the lock) it is renamed to
        ``<name>.corrupt-<ts>`` and the store starts empty; without it (a read) the damage is
        logged and the read sees an empty store. Reads never write."""
        if not self._path.exists():
            return None
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except Exception as exc:
            if quarantine:
                dest = self._path.with_name(
                    f"{self._path.name}.corrupt-{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}")
                try:
                    os.replace(self._path, dest)
                    log.warning("%s unreadable (%s); kept as %s and starting empty", self._path.name, exc, dest.name)
                except OSError as mv:
                    log.warning("%s unreadable (%s) and could not be set aside (%s)", self._path.name, exc, mv)
            else:
                log.warning("%s unreadable (%s); treating the store as empty", self._path.name, exc)
            return _empty()
        if not isinstance(data.get("bindings"), dict):
            data["bindings"] = {}
        if not isinstance(data.get("meta"), dict):
            data["meta"] = {}
        data.setdefault("schema", SCHEMA_VERSION)
        return data

    def _load(self) -> dict[str, Any]:
        """Read-only view. If v2 is absent but v1 exists, the one-time import runs once, under the lock."""
        data = self._read_v2(quarantine=False)
        if data is not None:
            return data
        if self.v1_path != self._path and self.v1_path.exists():
            with self._lock():
                data = self._read_v2(quarantine=True)
                if data is None:
                    data = self._import_v1() or _empty()
                return data
        return _empty()

    def _load_for_update(self) -> dict[str, Any]:
        """Caller holds the lock."""
        data = self._read_v2(quarantine=True)
        if data is None:
            data = self._import_v1() or _empty()
        return data

    def _import_v1(self) -> dict[str, Any] | None:
        """One-time import of the v1 file when v2 does not exist (caller holds the lock). Idempotent: it
        runs only while v2 is absent, and v2 is written as soon as the import succeeds."""
        v1 = self.v1_path
        if v1 == self._path or not v1.exists():
            return None
        try:
            raw = json.loads(v1.read_text(encoding="utf-8"))
            c2s = raw.get("chat_to_study") if isinstance(raw, dict) else None
            if not isinstance(c2s, dict):
                return None
        except Exception as exc:
            log.warning("v1 binding file unreadable at import (%s); skipping import", exc)
            return None
        data = _empty()
        skipped = 0
        stamp = _now_iso()
        for chat, study in c2s.items():
            if not isinstance(study, str) or not study or not is_valid_chat_id(chat) or chat == study:
                skipped += 1
                continue
            data["bindings"][chat] = {"study_id": study, "bound_at": stamp, "bound_by": "v1_import"}
        data["meta"]["imported_v1"] = stamp
        data["meta"]["imported_v1_entries"] = len(data["bindings"])
        data["meta"]["imported_v1_skipped"] = skipped
        try:
            backup = v1.with_name(v1.name + _V1_BACKUP_SUFFIX)
            if not backup.exists():
                shutil.copy2(v1, backup)
        except OSError as exc:
            log.warning("could not write %s (%s)", v1.name + _V1_BACKUP_SUFFIX, exc)
        try:
            self._save(data)
        except Exception as exc:
            log.warning("could not persist the v1 import: %s", exc)
        return data

    @staticmethod
    def _atomic_write(path: Path, text: str, prefix: str) -> None:
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=prefix)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _save(self, data: dict[str, Any]) -> None:
        """Atomic temp + fsync + rename (caller holds the lock); then the v1 projection."""
        data["schema"] = SCHEMA_VERSION
        self._atomic_write(self._path, json.dumps(data, indent=2, ensure_ascii=False), ".chat_bindings_")
        if self.v1_path == self._path:      # a caller named the v2 file like the v1 one: never overwrite it
            return
        try:
            self._write_v1_projection(data)
        except Exception as exc:    # the projection is a courtesy; v2 is the authority
            log.warning("v1 projection not written: %s", exc)

    def _write_v1_projection(self, data: dict[str, Any]) -> None:
        """Derived v1-shaped file (active studies only) for the installed extension, one release."""
        c2s: dict[str, str] = {}
        s2c: dict[str, str] = {}
        ordered = sorted(data["bindings"].items(), key=lambda kv: str(kv[1].get("bound_at", "")))
        active: dict[str, bool] = {}
        for chat, rec in ordered:
            study = rec.get("study_id")
            if not study:
                continue
            if study not in active:
                active[study] = _study_is_active(study)
            if not active[study]:
                continue
            c2s[chat] = study
            s2c[study] = chat           # last bound wins in the legacy inverse
        projection = {
            "chat_to_study": c2s,
            "study_to_chat": s2c,
            "meta": {"derived_from": _BINDING_NAME, "last_updated": _now_iso()},
        }
        self._atomic_write(self.v1_path, json.dumps(projection, indent=2, ensure_ascii=False), ".chat_studies_")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def validate(chat_id: str, study_id: str) -> None:
        """Raise ``InvalidChatIdError``/``ValueError`` when *chat_id* cannot be bound to *study_id*."""
        if not chat_id or not study_id:
            raise ValueError("chat_id and study_id must be non-empty strings")
        if chat_id == study_id:
            raise InvalidChatIdError(
                f"chat_id {chat_id!r} equals the study id; a study id is not a chat id")
        if not is_valid_chat_id(chat_id):
            raise InvalidChatIdError(
                f"chat_id {chat_id!r} is not a ULID (26 characters, Crockford base32); "
                "it must be the chat's own id, never a study id")

    def bind(self, chat_id: str, study_id: str, *, bound_by: str = "agent_explicit") -> bool:
        """
        Bind a chat to a study. Returns True when the binding changed.

        A chat has one study; rebinding replaces it. A study may have many chats,
        and binding another chat to it leaves the others bound.
        """
        self.validate(chat_id, study_id)
        with _LOCK, self._lock():
            data = self._load_for_update()
            current = data["bindings"].get(chat_id)
            if current and current.get("study_id") == study_id:
                return False
            data["bindings"][chat_id] = {
                "study_id": study_id, "bound_at": _now_iso(), "bound_by": bound_by,
            }
            data["meta"]["last_updated"] = _now_iso()
            if len(data["bindings"]) > _COMPACT_THRESHOLD:
                _compact(data)
            self._save(data)
        log.debug("Bound chat=%s -> study=%s (%s)", chat_id, study_id, bound_by)
        return True

    def lookup_study(self, chat_id: str) -> str | None:
        """Return the study bound to *chat_id*, or ``None``."""
        if not chat_id:
            return None
        rec = self._load()["bindings"].get(chat_id)
        return rec.get("study_id") if isinstance(rec, dict) else None

    def binding_info(self, chat_id: str) -> dict[str, Any] | None:
        """``{study_id, bound_at, bound_by}`` for *chat_id*, or ``None``."""
        rec = self._load()["bindings"].get(chat_id) if chat_id else None
        return dict(rec) if isinstance(rec, dict) else None

    def lookup_chats(self, study_id: str) -> list[str]:
        """Every chat bound to *study_id*, oldest binding first."""
        if not study_id:
            return []
        rows = [(rec.get("bound_at", ""), chat)
                for chat, rec in self._load()["bindings"].items()
                if isinstance(rec, dict) and rec.get("study_id") == study_id]
        return [chat for _at, chat in sorted(rows)]

    def lookup_chat(self, study_id: str) -> str | None:
        """The most recently bound chat of *study_id*, or ``None`` (v1-compatible accessor)."""
        chats = self.lookup_chats(study_id)
        return chats[-1] if chats else None

    def unbind_chat(self, chat_id: str) -> bool:
        """Remove the binding for *chat_id*. Returns True if something was removed."""
        with _LOCK, self._lock():
            data = self._load_for_update()
            rec = data["bindings"].pop(chat_id, None)
            if rec is None:
                return False
            data["meta"]["last_updated"] = _now_iso()
            self._save(data)
        log.debug("Unbound chat=%s (was -> study=%s)", chat_id, rec.get("study_id"))
        return True

    def unbind_study(self, study_id: str) -> bool:
        """Remove every binding of *study_id*. Returns True if something was removed."""
        with _LOCK, self._lock():
            data = self._load_for_update()
            chats = [c for c, rec in data["bindings"].items()
                     if isinstance(rec, dict) and rec.get("study_id") == study_id]
            for chat in chats:
                del data["bindings"][chat]
            if not chats:
                return False
            data["meta"]["last_updated"] = _now_iso()
            self._save(data)
        log.debug("Unbound study=%s (was bound from %d chats)", study_id, len(chats))
        return True

    def all_bindings(self) -> list[tuple[str, str]]:
        """Return all (chat_id, study_id) pairs as a list."""
        return [(c, rec["study_id"]) for c, rec in self._load()["bindings"].items()
                if isinstance(rec, dict) and rec.get("study_id")]

    def is_bound(self, chat_id: str) -> bool:
        """True if *chat_id* has an active study binding."""
        return self.lookup_study(chat_id) is not None


# ---------------------------------------------------------------------------
# Module-level singleton (shared across tool modules in-process)
# ---------------------------------------------------------------------------

_store: ChatBindingStore | None = None


def get_binding_store() -> ChatBindingStore:
    """Return the module-level singleton ``ChatBindingStore``."""
    global _store
    # Re-resolve when the singleton was created by this function and
    # AIHYDRO_HOME has since moved (tests, CI). A store injected by a caller
    # (custom path) is left alone.
    path = _binding_file()
    if _store is None or (getattr(_store, "_auto_path", False) and _store._path != path):
        _store = ChatBindingStore(path)
        _store._auto_path = True
    return _store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _study_file(study_id: str) -> Path | None:
    try:
        from ai_hydro.session import store as _s

        return _s._SESSIONS_DIR / f"{_s._safe_filename_component(study_id)}.json"
    except Exception:
        return None


def _study_is_active(study_id: str) -> bool:
    """The study file exists and is not archived (read raw; no HydroSession load)."""
    path = _study_file(study_id)
    if path is None or not path.exists():
        return False
    try:
        return not bool(json.loads(path.read_text(encoding="utf-8")).get("archived"))
    except Exception:
        return True     # unreadable is not the same as archived; keep the chip


def _compact(data: dict[str, Any]) -> None:
    """Drop bindings whose study file no longer exists. Called when the file passes ``_COMPACT_THRESHOLD``."""
    bindings: dict = data["bindings"]
    for chat, rec in list(bindings.items()):
        path = _study_file(rec.get("study_id", "")) if isinstance(rec, dict) else None
        if path is None or not path.exists():
            del bindings[chat]
    data["meta"]["last_compacted"] = _now_iso()
    log.info("%s compacted: %d active bindings remain", _BINDING_NAME, len(bindings))
