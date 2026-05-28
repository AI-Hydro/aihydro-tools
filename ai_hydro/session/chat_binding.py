"""
Chat ↔ Study binding store.

Persists a flat bidirectional map between Cline chat ULIDs and
AI-Hydro study (HydroSession) IDs.

Storage: ``~/.aihydro/chat_studies.json`` (atomic write-then-rename)

Schema::

    {
      "chat_to_study": {"01H...XYZ": "basin_26p9_78p1", ...},
      "study_to_chat": {"basin_26p9_78p1": "01H...XYZ", ...},
      "meta": {"last_compacted": "2026-05-01T00:00:00Z"}
    }

Public API::

    store = ChatBindingStore()
    store.bind("01H...XYZ", "basin_26p9_78p1")
    store.lookup_study("01H...XYZ")          # → "basin_26p9_78p1" or None
    store.lookup_chat("basin_26p9_78p1")     # → "01H...XYZ" or None
    store.unbind_chat("01H...XYZ")
    store.unbind_study("basin_26p9_78p1")
    store.all_bindings()                      # → list of (chat_id, study_id) tuples
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ai_hydro.session.chat_binding")

_BINDING_FILE = Path.home() / ".aihydro" / "chat_studies.json"
_COMPACT_THRESHOLD = 500  # compact when > N entries in chat_to_study


class ChatBindingStore:
    """
    Thread-safe (single-process) persistent store for chat↔study bindings.

    Uses atomic write-then-rename so the file is never half-written.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _BINDING_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Internal I/O helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"chat_to_study": {}, "study_to_chat": {}, "meta": {}}
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            # Tolerate partially-written or old-format files
            data.setdefault("chat_to_study", {})
            data.setdefault("study_to_chat", {})
            data.setdefault("meta", {})
            return data
        except Exception as exc:
            log.warning("chat_studies.json unreadable (%s); starting fresh", exc)
            return {"chat_to_study": {}, "study_to_chat": {}, "meta": {}}

    def _save(self, data: dict[str, Any]) -> None:
        """Atomic write-then-rename so the file is never half-written."""
        text = json.dumps(data, indent=2, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent, suffix=".tmp", prefix=".chat_studies_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def bind(self, chat_id: str, study_id: str) -> None:
        """
        Bind a chat ULID to a study ID (bidirectional).

        If the chat was previously bound to a *different* study, the old
        binding is silently replaced. If the study was previously bound to a
        *different* chat, that old chat→study entry is removed too (a study
        can migrate to a new chat via ``aihydro_rebind_chat``).
        """
        if not chat_id or not study_id:
            raise ValueError("chat_id and study_id must be non-empty strings")
        data = self._load()
        c2s: dict = data["chat_to_study"]
        s2c: dict = data["study_to_chat"]

        # Clean up any stale reverse pointer for this study
        old_chat = s2c.get(study_id)
        if old_chat and old_chat != chat_id:
            c2s.pop(old_chat, None)

        # Clean up any stale forward pointer for this chat
        old_study = c2s.get(chat_id)
        if old_study and old_study != study_id:
            s2c.pop(old_study, None)

        c2s[chat_id] = study_id
        s2c[study_id] = chat_id
        data["meta"]["last_updated"] = _now_iso()

        if len(c2s) > _COMPACT_THRESHOLD:
            _compact(data)

        self._save(data)
        log.debug("Bound chat=%s → study=%s", chat_id, study_id)

    def lookup_study(self, chat_id: str) -> str | None:
        """Return the study bound to *chat_id*, or ``None``."""
        if not chat_id:
            return None
        return self._load()["chat_to_study"].get(chat_id)

    def lookup_chat(self, study_id: str) -> str | None:
        """Return the chat ULID bound to *study_id*, or ``None``."""
        if not study_id:
            return None
        return self._load()["study_to_chat"].get(study_id)

    def unbind_chat(self, chat_id: str) -> bool:
        """Remove the binding for *chat_id*. Returns True if something was removed."""
        data = self._load()
        study = data["chat_to_study"].pop(chat_id, None)
        if study is not None:
            data["study_to_chat"].pop(study, None)
            data["meta"]["last_updated"] = _now_iso()
            self._save(data)
            log.debug("Unbound chat=%s (was → study=%s)", chat_id, study)
            return True
        return False

    def unbind_study(self, study_id: str) -> bool:
        """Remove the binding for *study_id*. Returns True if something was removed."""
        data = self._load()
        chat = data["study_to_chat"].pop(study_id, None)
        if chat is not None:
            data["chat_to_study"].pop(chat, None)
            data["meta"]["last_updated"] = _now_iso()
            self._save(data)
            log.debug("Unbound study=%s (was bound from chat=%s)", study_id, chat)
            return True
        return False

    def all_bindings(self) -> list[tuple[str, str]]:
        """Return all (chat_id, study_id) pairs as a list."""
        return list(self._load()["chat_to_study"].items())

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
    if _store is None:
        _store = ChatBindingStore()
    return _store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _compact(data: dict[str, Any]) -> None:
    """
    Rebuild the bidirectional map from scratch (removes orphaned entries).
    Called automatically when the file grows past ``_COMPACT_THRESHOLD``.
    """
    c2s: dict = data["chat_to_study"]
    s2c_new: dict = {}
    for chat, study in list(c2s.items()):
        # Only keep entries where the study JSON exists on disk
        from ai_hydro.session.store import SESSIONS_DIR
        if (SESSIONS_DIR / f"{study}.json").exists():
            s2c_new[study] = chat
        else:
            # Stale — study was deleted; remove forward pointer too
            del c2s[chat]
    data["study_to_chat"] = s2c_new
    data["meta"]["last_compacted"] = _now_iso()
    log.info(
        "chat_studies.json compacted: %d active bindings remain", len(c2s)
    )
