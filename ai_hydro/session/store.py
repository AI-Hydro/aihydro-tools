"""
AI-Hydro Research Session (HydroSession)
==========================================

Persistent research state across MCP tool calls.

Identity model
--------------
Three identifier fields, distinct on purpose:

  session_id   : primary key. Any string the researcher or LLM chose
                 ("piscataquis-snowmelt-2020", UUID, a USGS gauge used as
                 shorthand). Immutable after creation — anything that
                 changes session identity creates a new session.

  site_id      : data-source ID for the primary monitoring point
                 (e.g. USGS gauge "01031500", GRDC station, MERIT outlet).
                 Empty for ungauged / multi-site studies. Set by the
                 data-fetching tools (delineate_watershed, fetch_streamflow_data),
                 not by the user.

  site_name    : human-readable display name (e.g.
                 "Piscataquis River near Dover-Foxcroft, ME"). Should match
                 the canonical name returned by the data source. Drift
                 from `watershed.data.gauge_name` is flagged by
                 ``validate_identity()`` and exposed via the
                 ``get_session_health`` MCP tool.

Storage
-------
~/.aihydro/sessions/<session_id>.json    (atomic write-then-rename)

The canonical identifier used in workspace filenames is given by
``session.canonical_id`` — preferring ``site_id`` when set, falling
back to a slug of ``session_id``. Tools should always call
``session.workspace_filename(prefix, ext)`` rather than building paths
by hand, so the same study's outputs land in a consistent namespace.

Slot model (C1 — aihydro-core)
-------------------------------
Slots now use a three-level keyed structure:

    _slots[product][feature_id][params_key] = result_dict

This means two features' results for the same product (e.g. TWI for two
map annotations) coexist without collision. Caching is keyed by
(product, feature_id, params_key) — identical geometry+params is a hit;
different geometry is always a miss.

Backward compatibility
----------------------
All existing tool code uses:
  - ``session.twi`` / ``session.watershed`` etc. (property getters/setters)
  - ``session.set(slot, value)`` / ``session.get(slot)``
  - ``session.record_result(slot, data)``

These all write to / read from the ``__legacy__`` sentinel feature ID so
that un-migrated tools continue working exactly as before. New tools use
the Store Protocol methods (``put_result``, ``get_result``) with explicit
feature IDs.

Old session files (``_hydro_slots_v2`` key absent) are migrated losslessly
on first load: each single-value slot becomes
``{slot: {"__legacy__": {"": old_value}}}``.

Store Protocol (aihydro_core.store.Store)
-----------------------------------------
HydroSession implements the Store Protocol from aihydro-core so that
FeatureRegistry and (C2) @feature_tool can operate on it without importing
the session layer. All Store methods are defined below.

Dynamic slots
-------------
Plugins can register their own result slots without editing core code:
    session.set("my_plugin_result", {...})
    session.get("my_plugin_result")  # → dict or None
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aihydro_core.primitives.errors import ToolError
from ai_hydro.registry.paths import home_path
from ai_hydro.session import basin_identity as _bi

log = logging.getLogger("ai_hydro.session")

SESSIONS_DIR = home_path("sessions")
_SESSIONS_DIR = SESSIONS_DIR  # backward compat alias
# Deprecated: no longer used for any write (see ai_hydro.registry.paths.rules_dir).
# Kept only so existing ``patch("...store._REPO_ROOT")`` test fixtures still resolve.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Sentinel feature ID used by backward-compat set()/get() path.
# Old tools write results here; new @feature_tool tools use real feature IDs.
_LEGACY_FEATURE_ID = "__legacy__"

# Params key used by backward-compat set()/get() (params unknown for old results).
_LEGACY_PARAMS_KEY = ""

# Lists longer than this are stripped from the session JSON on disk.
_ARRAY_STRIP_THRESHOLD = 50

# Common slot names corresponding to built-in MCP tools.
_COMMON_SLOTS = (
    "watershed",
    "streamflow",
    "signatures",
    "geomorphic",
    "camels",
    "forcing",
    "twi",
    "cn",
    "model",
)

STALENESS_THRESHOLD = {
    "computed": 365,
    "notes": 30,
    "interpretation": 14,
}

_NOTE_DEDUP_WINDOW_SEC = 3600


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #

def _lean_slot(val: Any) -> Any:
    """Return a disk-safe (lean) copy of a single result dict."""
    if val is None:
        return None
    if not isinstance(val, dict):
        return val
    if "data" not in val:
        lean: dict = {}
        for k, v in val.items():
            if isinstance(v, list) and len(v) > _ARRAY_STRIP_THRESHOLD:
                lean[f"{k}_n"] = len(v)
            else:
                lean[k] = v
        return lean
    lean_data: dict = {}
    for k, v in val["data"].items():
        if isinstance(v, list) and len(v) > _ARRAY_STRIP_THRESHOLD:
            lean_data[f"{k}_n"] = len(v)
        else:
            lean_data[k] = v
    return {**val, "data": lean_data}


def _lean_product_slot(by_feature: dict) -> dict:
    """Apply _lean_slot to every result dict in a v2 product slot."""
    return {
        feature_id: {params_key: _lean_slot(result) for params_key, result in by_key.items()}
        for feature_id, by_key in by_feature.items()
    }


def _slugify_for_filename(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(s)).strip("-_.")
    return s.lower() or "session"


def _safe_filename_component(s: str, default: str = "session") -> str:
    """
    Case-preserving, traversal-safe filename component.

    Neutralizes only the characters that enable path traversal or break the
    filesystem: path separators, NUL, and any run of '..' (which could escape
    SESSIONS_DIR). Everything else — case, spaces, unicode, punctuation — is
    preserved unchanged, so existing session files on disk keep resolving to
    the same path after this function is introduced.

    Deliberately NOT `_slugify_for_filename`: that lowercases and collapses
    punctuation to a single '-', which would make two distinct legitimate
    session_ids (e.g. "Foo_Bar" and "foo-bar") collide on the same file.
    """
    s = str(s)
    s = s.replace("/", "_").replace("\\", "_").replace("\x00", "_")
    s = re.sub(r"\.{2,}", "_", s)  # neutralize '..' runs without touching single dots
    s = s.strip().strip(".")
    return s or default


def _contained_path(candidate: Path) -> Path | None:
    """
    Return `candidate` if it resolves inside SESSIONS_DIR, else None.

    Resolves symlinks in both the candidate and SESSIONS_DIR itself before
    comparing, so a symlinked sessions directory doesn't defeat the check.
    """
    try:
        resolved = candidate.resolve()
        sessions_root = _SESSIONS_DIR.resolve()
        if os.path.commonpath([str(resolved), str(sessions_root)]) != str(sessions_root):
            return None
    except (OSError, ValueError):
        return None
    return candidate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Run-log store — SQLite (WAL), one row per run_id
#
# _run_log used to be a plain dict stored inside the session's JSON slot
# model, written via whole-session load->mutate->save. Three independent call
# sites (enforcement.post_run, mcp/helpers._session_store, and put_result()
# on every Store Protocol write) all did this, so two concurrent writers to
# the same session could silently drop each other's run record (last save()
# wins). Each run is now a single atomically-upserted SQLite row instead —
# concurrent writers race at the row level, not the whole-session-blob level,
# so no run is ever lost. get("_run_log")/set("_run_log", ...) below present
# the exact same dict-shaped view every existing reader/writer already uses;
# this is an internal storage swap, not an API change.
# --------------------------------------------------------------------------- #

def write_session_data_file(session_id: str, name: str, content: Any) -> str | None:
    """Write ``content`` as JSON next to the session file; return the path.

    Fallback home for arrays that the lean session JSON drops (long lists
    become ``<key>_n`` counts) when the session has no workspace directory, so
    that a later tool can read back exactly the series a fetch stored instead
    of re-acquiring it. Returns None when the write fails.
    """
    try:
        path = _SESSIONS_DIR / (
            f"{_safe_filename_component(session_id)}.data.{_safe_filename_component(name, 'data')}"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, default=str))
        return str(path)
    except Exception as exc:
        log.debug("Session data write skipped (%s): %s", name, exc)
        return None


def _run_log_db_path(session_id: str) -> Path:
    safe = _safe_filename_component(session_id)
    return _SESSIONS_DIR / f"{safe}.runlog.sqlite3"


# Lock contention on the run-log database must never lose a row silently
# (2026-10-02: concurrent writers lost ~1 row in 10 runs to "database is
# locked" raised by the journal-mode PRAGMA and swallowed by the writer).
_RUN_LOG_LOCK_WAIT_S = 30.0


def _is_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _is_schema_changed_error(exc: BaseException) -> bool:
    """A concurrent ``CREATE TABLE IF NOT EXISTS`` in another process invalidated
    a prepared statement; a fresh connection succeeds."""
    return isinstance(exc, sqlite3.OperationalError) and "schema has changed" in str(exc).lower()


def _new_deadline(budget_s: float | None = None) -> float:
    """Absolute ``time.monotonic()`` deadline for one run-log operation."""
    return time.monotonic() + (_RUN_LOG_LOCK_WAIT_S if budget_s is None else budget_s)


def _busy_timeout_s(deadline: float) -> float:
    """SQLite busy wait for the time left, never above the lock-wait cap and
    never zero (a zero timeout would fail on the first contended attempt)."""
    return max(0.05, min(_RUN_LOG_LOCK_WAIT_S, deadline - time.monotonic()))


def _retry_when_locked(fn, *, deadline: float | None = None):
    """Call ``fn`` and retry with backoff while SQLite reports lock contention.

    Never retries past ``deadline`` (absolute monotonic; default: a fresh
    ``_RUN_LOG_LOCK_WAIT_S`` budget). A schema-changed error is retried once.
    """
    if deadline is None:
        deadline = _new_deadline()
    delay = 0.005
    schema_retried = False
    while True:
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if _is_schema_changed_error(exc) and not schema_retried:
                schema_retried = True
                continue
            if not _is_lock_error(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(max(0.0, min(delay, deadline - time.monotonic())))
            delay = min(delay * 2, 0.25)


def _run_log_connect(session_id: str, *, deadline: float | None = None) -> sqlite3.Connection:
    if not _SESSIONS_DIR.is_dir():       # a read of an existing store makes no filesystem call that writes
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = _run_log_db_path(session_id)
    contained = _contained_path(path)
    if contained is None:
        raise ValueError(
            f"Refusing to resolve run-log db outside SESSIONS_DIR: session_id={session_id!r}"
        )
    if deadline is None:
        deadline = _new_deadline()
    conn = sqlite3.connect(str(contained), timeout=_busy_timeout_s(deadline))

    def _init() -> None:
        # Switching journal mode needs an exclusive lock and can return
        # SQLITE_BUSY immediately (the busy timeout is not applied), so only
        # switch when the database is not already in WAL mode, and retry.
        conn.execute(f"PRAGMA busy_timeout={int(_busy_timeout_s(deadline) * 1000)}")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, timestamp TEXT, entry_json TEXT)"
        )

    try:
        _retry_when_locked(_init, deadline=deadline)
    except Exception:
        conn.close()
        raise
    return conn


def _run_log_body_json(entry: dict) -> str:
    """Stable text of a run-log row without its ``record`` (for equality checks).

    ``json.dumps`` rather than ``==`` so a NaN inside ``key_outputs`` compares
    equal to itself.
    """
    return json.dumps(
        {k: v for k, v in entry.items() if k != "record"}, sort_keys=True, default=str
    )


def _run_log_sealed(entry: Any) -> bool:
    record = entry.get("record") if isinstance(entry, dict) else None
    return isinstance(record, dict) and bool(record.get("record_digest"))


def _run_log_record_problem(run_id: str, record: Any) -> str | None:
    """Why an incoming ``record`` must not be stored, or None when it is fine."""
    if not isinstance(record, dict):
        return "record is not an object"
    if record.get("run_id") != run_id:
        return f"record.run_id {record.get('run_id')!r} does not match row {run_id!r}"
    try:
        from aihydro_core.records import RunRecord
    except ImportError:  # core without records: cannot check, do not block
        return None
    if not RunRecord.from_dict(record).verify():
        return "record_digest does not verify"
    return None


_WS_CACHE: dict[str, tuple[int, str | None]] = {}
_WS_WARNED: set[str] = set()


def _session_workspace_dir(session_id: str) -> str | None:
    """Workspace dir recorded in the session file, read raw (no HydroSession load).

    Cached per (file, mtime): the scrub runs on every row write. When the file
    is unreadable the workspace cannot be named; that is logged once per session
    and the path falls back to ``~/`` or ``<abs>/basename``.
    """
    sf = _SESSIONS_DIR / f"{_safe_filename_component(session_id)}.json"
    key = str(sf)
    try:
        mtime = sf.stat().st_mtime_ns
        hit = _WS_CACHE.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        ws = json.loads(sf.read_text()).get("workspace_dir") or None
        _WS_CACHE[key] = (mtime, ws)
        return ws
    except Exception as exc:
        if key not in _WS_WARNED:
            _WS_WARNED.add(key)
            log.debug("workspace dir unavailable for row scrub (%s): %s", session_id, exc)
        return None


def _scrub_row_body(session_id: str, entry: dict) -> dict:
    """Privacy choke point: a row body (everything except ``record``) never
    stores an absolute local path. Applied before the body is digested into
    ``extra.entry_digest`` and before it is stored; idempotent."""
    try:
        from ai_hydro.session.refs import scrub_value

        ws = _session_workspace_dir(session_id)
        return {k: (v if k == "record" else scrub_value(v, ws)) for k, v in entry.items()}
    except Exception as exc:  # never block a write on the scrubber
        log.warning("run-log row scrub skipped: %s", exc)
        return entry


def _run_log_record(
    session_id: str, run_id: str, entry: dict, *, writer: str | None = None,
    deadline: float | None = None,
) -> str:
    """
    Store one run-log row, verbatim as JSON, and return what happened:
    ``inserted``, ``replaced``, ``noop``, ``refused``, ``stale``, ``skipped``
    or ``error``. Never raises — a logging failure must not invalidate the tool
    call that triggered it.

    The entry keeps whatever legacy shape the caller uses (tool_name /
    timestamp / key_outputs, or bare ``{metric: value}`` bench fixtures, which
    are resolved directly by json-path), so this round-trips any dict.

    Rows are insert-only once sealed. A row is *sealed* when its ``record``
    carries a ``record_digest`` (see ``ai_hydro.session.run_records``). For an
    existing sealed row:
      - an identical write, or a stale legacy write that lacks the ``record``
        but has the same other fields, is a no-op (the record is never
        dropped);
      - anything else — a different record, or different legacy fields — is
        refused and logged, never replaced.
    An unsealed row keeps the legacy upsert behaviour, except that attaching a
    ``record`` to a row whose other fields changed since the caller read it is
    reported as ``stale`` and not written, so the caller can rebuild the record
    against the current row.

    ``writer`` labels the source (``post_run``, ``put_result``, ...) for the
    per-call capture used by the recording middleware; ``None`` or
    ``"middleware"`` are not captured. When a writer's row is lost (``error``)
    that is captured too (``run_records.note_row_failed``) so the middleware
    can say so in the minimal row and in the tool result.

    ``deadline`` (absolute ``time.monotonic()``) bounds connect, retries and
    the final busy wait together; default is one fresh lock-wait budget.
    """
    if not run_id or not isinstance(entry, dict):
        return "skipped"
    if deadline is None:
        deadline = _new_deadline()
    status = "error"
    try:
        entry = _scrub_row_body(session_id, entry)
        incoming = entry.get("record")
        if incoming is not None:
            try:
                import aihydro_core.records  # noqa: F401
            except ImportError:
                # Cannot verify the seal, so do not store a sealed-looking
                # record: keep the legacy row, drop the record, say so.
                log.warning(
                    "aihydro_core.records unavailable: dropping unverifiable record "
                    "from run-log row %s in session %s", run_id, session_id)
                entry = {k: v for k, v in entry.items() if k != "record"}
                incoming = None
        if incoming is not None:
            problem = _run_log_record_problem(run_id, incoming)
            if problem:
                log.warning("Refused run-log record for %s in session %s: %s", run_id, session_id, problem)
                if writer and writer != "middleware":
                    try:
                        from ai_hydro.session import run_records
                        run_records.note_row_refused(session_id, run_id, writer, "refused")
                    except Exception:  # capture is best-effort bookkeeping
                        pass
                return "refused"
        def _txn() -> str:
            status_box = ["error"]
            conn = _run_log_connect(session_id, deadline=deadline)
            try:
                conn.execute(f"PRAGMA busy_timeout={int(_busy_timeout_s(deadline) * 1000)}")
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT entry_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                existing = None
                if row is not None:
                    try:
                        existing = json.loads(row[0]) if row[0] else {}
                    except json.JSONDecodeError:
                        existing = {}
                if existing is None:
                    status_box[0] = "inserted"
                elif _run_log_sealed(existing):
                    # Legacy sealed rows may still hold paths: compare scrubbed forms so a
                    # stale identical write stays a no-op (the stored row is never rewritten).
                    same_body = (_run_log_body_json(existing) == _run_log_body_json(entry)
                                 or _run_log_body_json(_scrub_row_body(session_id, existing))
                                 == _run_log_body_json(entry))
                    same_record = incoming is None or incoming.get("record_digest") == existing["record"]["record_digest"]
                    if same_body and same_record:
                        status_box[0] = "noop"
                    else:
                        log.warning(
                            "Refused write to sealed run-log row %s in session %s: %s",
                            run_id, session_id,
                            "different record" if not same_record else "other fields differ",
                        )
                        status_box[0] = "refused"
                elif (incoming is not None and _run_log_body_json(existing) != _run_log_body_json(entry)
                      and _run_log_body_json(_scrub_row_body(session_id, existing)) != _run_log_body_json(entry)):
                    status_box[0] = "stale"
                else:
                    status_box[0] = "replaced"
                if status_box[0] in ("inserted", "replaced"):
                    timestamp = str(entry.get("timestamp", "") or "")
                    conn.execute(
                        "INSERT OR REPLACE INTO runs (run_id, timestamp, entry_json) VALUES (?, ?, ?)",
                        (run_id, timestamp, json.dumps(entry, default=str)),
                    )
                conn.commit()
            finally:
                conn.close()
            return status_box[0]

        status = _retry_when_locked(_txn, deadline=deadline)
    except Exception as exc:
        log.warning("Failed to record run-log entry %s for session %s: %s", run_id, session_id, exc)
        if writer and writer != "middleware":
            try:
                from ai_hydro.session import run_records
                run_records.note_row_failed(session_id, run_id, writer, str(exc) or type(exc).__name__)
            except Exception:  # capture is best-effort bookkeeping
                pass
        return "error"
    if writer and writer != "middleware" and status in ("inserted", "replaced", "noop"):
        try:
            from ai_hydro.session import run_records
            run_records.note_row_written(session_id, run_id, writer)
        except Exception:  # capture is best-effort bookkeeping
            pass
    elif writer and writer != "middleware" and status in ("refused", "stale"):
        # The writer's body disagrees with the stored (sealed) row: keep the
        # sealed body, but say so (skeptic-slice3 R3).
        try:
            from ai_hydro.session import run_records
            run_records.note_row_refused(session_id, run_id, writer, status)
        except Exception:  # capture is best-effort bookkeeping
            pass
    return status


def _run_log_mark_unsealable(
    session_id: str, run_id: str, expected_body_json: str | None, reason: str,
    *, deadline: float | None = None,
) -> str:
    """Mark an UNSEALED row ``record_status: unsealable`` (with ``reason``).

    Only when the row still has no record and, when ``expected_body_json`` is
    given, its body (scrubbed or not) is still that text. A sealed row is never
    touched. Returns ``marked``, ``noop`` (already sealed, already marked or
    the row is gone), ``changed`` (body differs) or ``error``. Never raises.
    """
    if deadline is None:
        deadline = _new_deadline()

    def _txn() -> str:
        conn = _run_log_connect(session_id, deadline=deadline)
        try:
            conn.execute(f"PRAGMA busy_timeout={int(_busy_timeout_s(deadline) * 1000)}")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT entry_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                conn.commit()
                return "noop"
            try:
                existing = json.loads(row[0]) if row[0] else {}
            except json.JSONDecodeError:
                existing = {}
            if _run_log_sealed(existing) or existing.get("record_status") == "unsealable":
                conn.commit()
                return "noop"
            if expected_body_json is not None and _run_log_body_json(existing) != expected_body_json \
                    and _run_log_body_json(_scrub_row_body(session_id, existing)) != expected_body_json:
                conn.commit()
                return "changed"
            marked = {**_scrub_row_body(session_id, existing),
                      "record_status": "unsealable", "record_status_reason": str(reason)}
            conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, timestamp, entry_json) VALUES (?, ?, ?)",
                (run_id, str(marked.get("timestamp", "") or ""), json.dumps(marked, default=str)),
            )
            conn.commit()
            return "marked"
        finally:
            conn.close()

    try:
        return _retry_when_locked(_txn, deadline=deadline)
    except Exception as exc:
        log.warning("Failed to mark run-log row %s unsealable in session %s: %s", run_id, session_id, exc)
        return "error"


def _run_log_read_one(session_id: str, run_id: str, *, deadline: float | None = None,
                      strict: bool = False) -> dict | None:
    """One run-log row, or None. Never creates the database.

    ``strict``: raise on a read failure instead of returning None, so a caller
    that is about to write can tell "no such row" from "could not look".
    """
    if not run_id or not _run_log_db_path(session_id).exists():
        return None
    try:
        conn = _run_log_connect(session_id, deadline=deadline)
        try:
            row = conn.execute("SELECT entry_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return json.loads(row[0]) if row[0] else {}
    except Exception as exc:
        log.warning("Failed to read run-log row %s for session %s: %s", run_id, session_id, exc)
        if strict:
            raise
        return None


class RunLogUnreadable(RuntimeError):
    """The persisted run log exists but could not be read."""


def _run_log_read_all(session_id: str, *, strict: bool = False) -> dict:
    """Reconstruct the {run_id: entry} dict view every existing reader expects,
    with each entry exactly as it was written (see _run_log_record).

    A missing database is an empty log. An unreadable one is logged and
    returned as ``{}`` for ordinary readers; ``strict=True`` (export) raises
    ``RunLogUnreadable`` so an empty log is never mistaken for a real one."""
    path = _run_log_db_path(session_id)
    if not path.exists():
        return {}
    try:
        conn = _run_log_connect(session_id)
        try:
            rows = conn.execute(
                "SELECT run_id, entry_json FROM runs ORDER BY timestamp"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Failed to read run log for session %s: %s", session_id, exc)
        if strict:
            raise RunLogUnreadable(
                f"run log for session {session_id!r} cannot be read: {exc}") from exc
        return {}
    out: dict[str, dict] = {}
    for run_id, entry_json in rows:
        try:
            out[run_id] = json.loads(entry_json) if entry_json else {}
        except json.JSONDecodeError:
            out[run_id] = {}
    return out


def _run_log_migrate_legacy(session_id: str, legacy: dict) -> None:
    """
    One-time import of pre-SQLite _run_log entries found in a session's JSON
    file. Idempotent (INSERT OR IGNORE) — safe to call on every load().
    """
    if not legacy:
        return
    try:
        conn = _run_log_connect(session_id)
        try:
            for run_id, entry in legacy.items():
                if not isinstance(entry, dict):
                    continue
                timestamp = str(entry.get("timestamp", "") or "")
                conn.execute(
                    "INSERT OR IGNORE INTO runs (run_id, timestamp, entry_json) VALUES (?, ?, ?)",
                    (run_id, timestamp, json.dumps(entry, default=str)),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Failed to migrate legacy run log for session %s: %s", session_id, exc)


def _synopsis_from_result(result: dict | None) -> dict:
    """Extract a lean LLM-readable synopsis from one result dict."""
    if not result:
        return {}
    data = result.get("data", {})
    meta = result.get("meta", {})
    synopsis: dict = {}
    for k, v in data.items():
        if k.startswith("_"):
            continue
        if isinstance(v, list):
            if len(v) > _ARRAY_STRIP_THRESHOLD:
                synopsis[f"{k}_n"] = len(v)
            else:
                synopsis[k] = v
        elif isinstance(v, dict):
            synopsis[k] = {
                dk: (dv if not (isinstance(dv, list) and len(dv) > _ARRAY_STRIP_THRESHOLD)
                     else f"[{len(dv)} items]")
                for dk, dv in v.items()
            }
        else:
            synopsis[k] = v
    computed_at = meta.get("computed_at", "")
    synopsis["_computed_at"] = computed_at[:10] if computed_at else None
    synopsis["_tool"] = meta.get("tool")
    if meta.get("params"):
        synopsis["_params"] = meta["params"]
    return synopsis


def _latest_result_in_feature(by_key: dict) -> dict | None:
    """Return the most recently computed result from a feature's params-keyed dict."""
    valid = [(k, v) for k, v in by_key.items() if v is not None]
    if not valid:
        return None
    return max(
        valid,
        key=lambda kv: (kv[1] or {}).get("meta", {}).get("computed_at", ""),
    )[1]


# --------------------------------------------------------------------------- #
# Slot artifact health (Science Wave 3, PR-4a)
# --------------------------------------------------------------------------- #

#: Worst first: a slot reports the worst health among its features' latest results.
SLOT_HEALTH_ORDER = ("missing_artifact", "digest_mismatch", "digest_missing", "legacy_convention", "ok")

_SLOT_HEALTH_FIX = {
    "missing_artifact": "the retained data file is gone or not readable from the session; re-run the tool "
                        "that produced this slot (clear_session([slot]) first if it answers from cache).",
    "digest_mismatch": "the retained data file changed after the result was recorded; do not cite "
                       "this slot, re-run the tool that produced it.",
    "digest_missing": "the slot points at a retained file but records no usable digest for it, so the file "
                      "cannot be verified; re-run the tool that produced it.",
    "legacy_convention": "computed before the current signature conventions (flow-quantile convention "
                         "and, for current producers, precipitation completeness); re-run "
                         "extract_hydrological_signatures before comparing it with anything.",
}

#: Keys only the signatures producer that reports precipitation completeness emits (aihydro-watershed
#: 0.2.0). A result carrying one is held to the completeness contract; an older producer's result is not
#: "legacy" merely because it predates a field it never had.
_COMPLETENESS_ERA_MARKERS = ("signature_definitions",)

_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}


def _file_digest(path: Path) -> str:
    """``sha256:<hex>`` of a file (same scheme as ``aihydro_core.records.digest_bytes``), hashed in
    chunks and cached by (path, size, mtime_ns) so repeated summaries do not re-read big series."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = _DIGEST_CACHE.get(key)
    if hit is not None:
        return hit
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = "sha256:" + h.hexdigest()
    if len(_DIGEST_CACHE) > 512:          # tools run in threads: return the local, never re-read the cache
        _DIGEST_CACHE.clear()
    _DIGEST_CACHE[key] = digest
    return digest


def _resolve_artifact(raw: str, roots: list[Path]) -> tuple[Path | None, str | None]:
    """``(path, None)`` for a ``_data_file`` inside one of ``roots``, else ``(None, reason)``.

    A relative path is resolved against each root in turn. The resolved path (symlinks followed) must
    stay inside a resolved root, so a link pointing out of the session/workspace dirs is refused."""
    cand = Path(raw)
    options = [cand] if cand.is_absolute() else [r / cand for r in roots]
    first_inside: Path | None = None
    for opt in options:
        try:
            resolved = opt.resolve()
        except (OSError, RuntimeError):
            continue
        for root in roots:
            try:
                resolved.relative_to(root.resolve())
            except (ValueError, OSError):
                continue
            if resolved.is_file():
                return resolved, None
            first_inside = first_inside or resolved
    if first_inside is not None:
        return None, "missing"
    if not cand.is_absolute():
        return None, "missing"
    return None, "outside_session_dir"


def _precipitation_reported(data: dict) -> bool:
    """The result states what happened to precipitation. ``completeness`` may legitimately be None
    (status unavailable / not_attempted / skip), so key presence or a status is what counts."""
    prec = data.get("_precipitation")
    return isinstance(prec, dict) and ("completeness" in prec or prec.get("status") is not None)


def _result_health(slot: str, val: Any, roots: list[Path] | None = None) -> tuple[str, str | None]:
    """``(health, reason)`` for one stored result; ``health`` is in ``SLOT_HEALTH_ORDER``.

    * ``missing_artifact``: the result points at a ``_data_file`` that does not exist, or that lies
      outside the session/workspace data dirs (reason ``outside_session_dir``).
    * ``digest_mismatch``: the file's digest differs from ``meta.retained_series.digest``.
    * ``digest_missing``: the slot declares ``meta.retained_series`` (or is the streamflow slot) but
      records no digest, or records another path than ``_data_file``: same rule as the consumer in
      ``extract_hydrological_signatures``.
    * ``legacy_convention``: a signatures result without ``flow_quantile_convention``; or, from a
      producer new enough to carry ``signature_definitions``, without a precipitation report.
    """
    if not isinstance(val, dict):
        return "ok", None
    roots = roots if roots is not None else [_SESSIONS_DIR]
    data = val.get("data") if isinstance(val.get("data"), dict) else {}
    meta = val.get("meta") if isinstance(val.get("meta"), dict) else {}
    data_file = data.get("_data_file")
    if data_file:
        path, why = _resolve_artifact(str(data_file), roots)
        name = Path(str(data_file)).name
        if path is None:
            return "missing_artifact", (f"_data_file {name} is outside the session/workspace data dirs "
                                        "(outside_session_dir)" if why == "outside_session_dir"
                                        else f"_data_file {name} does not exist")
        retained = meta.get("retained_series") if isinstance(meta.get("retained_series"), dict) else None
        if retained is not None or slot == "streamflow":
            retained = retained or {}
            want = retained.get("digest")
            if not want or retained.get("path") != data_file:
                return "digest_missing", f"_data_file {name} has no matching recorded digest/path"
            try:
                have = _file_digest(path)
            except OSError as exc:
                return "missing_artifact", f"_data_file {name} is unreadable ({type(exc).__name__})"
            if have != want:
                return "digest_mismatch", f"_data_file {name} digest differs from the recorded one"
    if slot == "signatures":
        missing = []
        if "flow_quantile_convention" not in data:
            missing.append("flow_quantile_convention")
        if any(m in data for m in _COMPLETENESS_ERA_MARKERS) and not _precipitation_reported(data):
            missing.append("_precipitation report")
        if missing:
            return "legacy_convention", "no " + " / no ".join(missing)
    return "ok", None


# --------------------------------------------------------------------------- #
# HydroSession
# --------------------------------------------------------------------------- #

def _json_safe(obj: Any) -> Any:
    """Return a strict-JSON-safe copy, replacing NaN/Inf with None."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Concurrent writers (T1-R)
#
# Tools run in worker threads, so two calls on one study overlap (a long fetch, then a note). A
# session is a read-modify-write of one JSON file: without care the later save replaces the
# earlier one's changes. No lock is held while a tool works.  ``save()`` instead
#   * takes a short, bounded write guard (in-process lock + a sidecar flock for other processes)
#     around "compare, merge, write" only, never across a tool's network or compute;
#   * if the file changed since this object loaded it, three-way merges (what we loaded / what we
#     hold / what is on disk), so each writer's own changes survive; our value wins only where
#     both changed the same leaf;
#   * raises ``SessionBusyError`` (code SESSION_BUSY, retryable) when the guard cannot be taken in
#     ``AIHYDRO_SESSION_WRITE_WAIT_S`` seconds, naming the tool that holds it.
# ---------------------------------------------------------------------------
_SESSION_WRITE_WAIT_S = 10.0
_MERGE_DEPTH = 3                 # slots: product -> feature -> params_key; deeper values replace wholesale
_FORCE_MERGE = object()          # "the disk stamp is unknown: merge on the next save"
_NO_MERGE = object()             # a freshly built object (not loaded): its first save is authoritative, as before


def _session_write_wait_s() -> float:
    try:
        return max(0.1, float(os.environ.get("AIHYDRO_SESSION_WRITE_WAIT_S", _SESSION_WRITE_WAIT_S)))
    except ValueError:
        return _SESSION_WRITE_WAIT_S


class SessionBusyError(ToolError):
    """A study's write guard could not be taken in time. Retryable; names who holds it."""

    def __init__(self, session_id: str, holder: str | None, waited_s: float, running: list[str] | None = None):
        self.session_id = session_id
        self.holder = holder
        names = running or ([holder] if holder else [])
        who = f" by {holder}" if holder else ""
        super().__init__(
            code="SESSION_BUSY",
            message=(f"Study {session_id!r} is being written{who}; waited {waited_s:.0f}s, so this call's "
                     "result was NOT saved to the study. What is kept: everything already saved, and fetched "
                     "data held in aihydro-data's cache (a retry re-reads it, not the network). Uncached "
                     "computation from this call is recomputed on the retry."),
            details={"retryable": True, "session_id": session_id, "held_by": holder,
                     "running_tools": names, "waited_s": round(waited_s, 1)},
            recovery="Retry in a few seconds; wait_for_job(job_id) if a long job is running.",
            alternatives=["wait_for_job", "get_session_summary"],
        )


_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_HOLDERS: dict[str, str] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _stamp_of(st: os.stat_result) -> tuple[int, int, int]:
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _current_tool_name() -> str | None:
    try:
        from ai_hydro.mcp.app import ACTIVE_TOOL_NAME

        return ACTIVE_TOOL_NAME.get()
    except Exception:
        return None


_HELD = threading.local()


@contextlib.contextmanager
def _session_write_guard(target: Path, session_id: str):
    """Exclusive, bounded, re-entrant guard for writing ``target``; raises ``SessionBusyError`` on timeout.

    Re-entrant per thread, so a tool that holds it across load-check-save (``session_write_guard``) can
    call ``save()`` inside.
    """
    from ai_hydro.registry.locking import LockTimeout, file_lock_bounded

    key = str(target)
    held = getattr(_HELD, "keys", None)
    if held is None:
        held = _HELD.keys = {}
    if held.get(key, 0) > 0:
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    wait = _session_write_wait_s()
    try:
        import asyncio

        asyncio.get_running_loop()
        wait = min(wait, 0.25)       # backstop: a save on the loop thread must not hold the loop
    except RuntimeError:
        pass
    started = time.monotonic()
    with _WRITE_LOCKS_GUARD:
        tlock = _WRITE_LOCKS.setdefault(key, threading.Lock())

    def busy() -> SessionBusyError:
        running: list[str] = []
        try:
            from ai_hydro.mcp.offload import running_tools

            running = sorted({r["tool"] for r in running_tools()
                              if r.get("session_id") in (None, session_id)})
        except Exception:
            pass
        return SessionBusyError(session_id, _WRITE_HOLDERS.get(key), time.monotonic() - started, running)

    if not tlock.acquire(timeout=wait):
        raise busy()
    try:
        left = max(0.05, wait - (time.monotonic() - started))
        try:
            with file_lock_bounded(target.with_name(target.name + ".write.lock"), left):
                _WRITE_HOLDERS[key] = _current_tool_name() or "unknown"
                held[key] = 1
                try:
                    yield
                finally:
                    held.pop(key, None)
                    _WRITE_HOLDERS.pop(key, None)
        except LockTimeout:
            raise busy() from None
    finally:
        tlock.release()


@contextlib.contextmanager
def session_write_guard(session_id: str, shard_id: str | None = None):
    """Hold a study's write guard across a whole load -> check -> mutate -> save.

    For tools whose correctness is a check-then-set (lock a plan, promote or revise a claim): the guard
    is held for local work only, never across network or long compute, and waits are bounded
    (``SESSION_BUSY``). ``HydroSession.load`` and ``save`` inside it are re-entrant.
    """
    with _session_write_guard(HydroSession._path(session_id, shard_id), session_id):
        yield


class SessionConflictError(ToolError):
    """A save would replace a locked / sealed record that another call stored in the meantime."""

    def __init__(self, session_id: str, path: str):
        self.session_id, self.path = session_id, path
        super().__init__(
            code="SESSION_CONFLICT",
            message=(f"Study {session_id!r}: another call already stored a locked or sealed record at "
                     f"{path!r}; this call's different value was not saved. A locked record is never replaced."),
            details={"retryable": False, "session_id": session_id, "path": path},
            recovery="Reload the study (get_session_summary), look at the stored record, and decide whether "
                     "your change is still wanted; a locked plan or sealed record is revised, not overwritten.",
        )


def _is_protected(v: Any) -> bool:
    return isinstance(v, dict) and (v.get("locked") is True or v.get("sealed") is True)


def _is_atomic(v: Any) -> bool:
    """A record with an integrity marker is one unit: the later whole record wins, never a field mix."""
    return isinstance(v, dict) and any(k in v for k in ("digest", "sealed", "locked", "content_hash", "revision_digest"))


def _merge3(base: Any, ours: Any, theirs: Any, depth: int = 0, path: str = "") -> Any:
    """Three-way merge of JSON-shaped state; ``ours`` (the later save) wins where both sides changed one leaf.

    Never mixes fields of a record with an integrity marker (digest/sealed/locked...), and never
    replaces a locked or sealed record another call stored with a different one (``_Conflict``).
    """
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if _is_protected(theirs):
        raise _Conflict(path)
    if _is_atomic(ours) or _is_atomic(theirs):
        return ours
    if isinstance(ours, dict) and isinstance(theirs, dict) and depth < _MERGE_DEPTH:
        b = base if isinstance(base, dict) else {}
        out: dict = {}
        for k in list(theirs) + [k for k in ours if k not in theirs]:
            in_o, in_t, in_b = k in ours, k in theirs, k in b
            sub = f"{path}/{k}"
            if in_o and in_t:
                out[k] = _merge3(b.get(k), ours[k], theirs[k], depth + 1, sub)
            elif in_o:                                   # absent on disk
                if not in_b or ours[k] != b[k]:
                    out[k] = ours[k]                     # we added or changed it
                # else: they deleted a value we did not touch -> stays deleted
            else:                                        # absent in ours
                if not in_b:
                    out[k] = theirs[k]                   # they added it
                elif theirs[k] != b[k]:
                    out[k] = theirs[k]                   # we deleted it but they changed it: keep theirs
                # else: we deleted, they did not touch -> deleted
        return out
    if isinstance(ours, list) and isinstance(theirs, list):
        b = base if isinstance(base, list) else []
        kept = [x for x in theirs if not (x in b and x not in ours)]      # their list, minus what we removed
        added = [x for x in ours if x not in b and x not in theirs]       # plus what we added
        return kept + added
    return ours


class _Conflict(Exception):
    def __init__(self, path: str):
        self.path = path or "/"
        super().__init__(self.path)


def _basin_digest_of(raw: dict) -> str | None:
    try:
        ws = (raw.get("watershed") or {}).get(_LEGACY_FEATURE_ID, {}).get(_LEGACY_PARAMS_KEY)
        return _bi.watershed_slot_digest(ws) if isinstance(ws, dict) else None
    except Exception:
        return None


def _apply_basin_change(base: dict, merged: dict) -> None:
    """If the merge changed the session basin, drop what was computed for the old one (as ``put_result`` does).

    A merge can put a basin-dependent result computed for basin X next to a watershed Y that another
    call stored (or the reverse); neither may stay on disk or reach raw readers.
    """
    prev, new = _basin_digest_of(base), _basin_digest_of(merged)
    if not (prev and new and prev != new):
        return
    expected = _bi.make_identity(new, None)
    dropped: list[str] = []
    for slot in _bi.BASIN_DEPENDENT_SLOTS:
        by_feature = merged.get(slot)
        if not isinstance(by_feature, dict):
            continue
        by_key = by_feature.get(_LEGACY_FEATURE_ID)
        if not isinstance(by_key, dict):
            continue
        for k in list(by_key):
            if by_key[k] is not None and _bi.judge(by_key[k], expected) != _bi.VERIFIED:
                del by_key[k]
                if slot not in dropped:
                    dropped.append(slot)
        if not by_key:
            del by_feature[_LEGACY_FEATURE_ID]
        if not by_feature:
            del merged[slot]
    ws = (merged.get("watershed") or {}).get(_LEGACY_FEATURE_ID, {}).get(_LEGACY_PARAMS_KEY) or {}
    data = ws.get("data") if isinstance(ws.get("data"), dict) else {}
    cleared = None
    if merged.get("site_id") and _bi.norm_gauge(merged["site_id"]) != _bi.norm_gauge(data.get("gauge_id")):
        cleared = merged["site_id"]
        merged["site_id"], merged["site_type"], merged["site_name"] = "", "", ""
    extra = merged.setdefault("extra", {})
    log_ = extra.setdefault("basin_changes", [])
    if not (log_ and log_[-1].get("to") == new):
        log_.append({"at": _now(), "from": prev, "to": new, "dropped_slots": sorted(dropped),
                     "cleared_site_id": cleared, "by": "merge"})
        del log_[:-20]


def _merge_session_raw(base_text: str | None, ours: dict, theirs_text: str, session_id: str = "") -> dict:
    """Merge the object's state (``ours``) over what is on disk, relative to what it loaded."""
    try:
        base = json.loads(base_text) if base_text else {}
    except ValueError:
        base = {}
    try:
        theirs = json.loads(theirs_text)
    except ValueError:
        return ours                                      # unreadable file: our state replaces it, as before
    ours_n = json.loads(json.dumps(ours, allow_nan=False))
    if not isinstance(base, dict) or not isinstance(theirs, dict):
        return ours_n
    try:
        merged = _merge3(base, ours_n, theirs, 0)
    except _Conflict as c:
        raise SessionConflictError(session_id, c.path) from None
    _apply_basin_change(base, merged)
    ua, ub = ours_n.get("updated_at"), theirs.get("updated_at")
    if isinstance(ua, str) and isinstance(ub, str):
        merged["updated_at"] = max(ua, ub)
    return merged



class HydroSession:
    """Persistent research state for a single study across tool calls.

    Implements the aihydro_core.store.Store Protocol so FeatureRegistry and
    the (C2) @feature_tool decorator can address it directly.
    """

    def __init__(self, session_id: str, shard_id: str | None = None) -> None:
        self.session_id: str = session_id
        self.shard_id: str | None = shard_id
        self.site_name: str = ""
        self.site_id: str = ""
        self.site_type: str = ""
        self.workspace_dir: str | None = None
        self.working_geometry_path: str | None = None

        # Three-level slot store: product → feature_id → params_key → result_dict.
        # Old tools write to _LEGACY_FEATURE_ID via set()/get() backward compat.
        # New @feature_tool tools write with real feature IDs via put_result().
        self._slots: dict[str, dict[str, dict[str, dict | None]]] = {}

        # Feature registry: feature_id → Feature.to_dict() (aihydro_core)
        self._features: dict[str, dict] = {}
        self.active_feature_id: str | None = None

        self.notes: list[str] = []
        self.interpretation: str = ""
        self.interpretation_at: str | None = None
        self.created_at: str = _now()
        self.updated_at: str = self.created_at
        self.archived: bool = False
        self._citations: set[str] = set()
        self.artifact_manifest: dict[str, dict] = {}
        self.claims: dict[str, dict] = {}
        self.assumptions: dict[str, dict] = {}
        self.extra: dict[str, Any] = {}  # free-form metadata for phase extensions
        self._site_name_history: list[dict] = []
        # Basin-dependent slots served unstamped by get() on this instance (see basin_identity).
        self._unverified_reads: set[str] = set()
        # What the file looked like when this object loaded it (stat stamp + text); save() merges
        # when another writer changed it since. ``None`` stamp = the file did not exist.
        self._disk_stamp: Any = _NO_MERGE
        self._base_text: str | None = None

    # ------------------------------------------------------------------
    # Backward-compat: gauge_id → site_id
    # ------------------------------------------------------------------

    @property
    def gauge_id(self) -> str:
        return self.site_id or self.session_id

    # ------------------------------------------------------------------
    # Canonical identity
    # ------------------------------------------------------------------

    @property
    def canonical_id(self) -> str:
        if self.site_id:
            return _slugify_for_filename(self.site_id)
        return _slugify_for_filename(self.session_id)

    def workspace_filename(self, prefix: str, ext: str = "json") -> str:
        prefix = prefix.strip("_")
        ext = ext.lstrip(".")
        return f"{prefix}_{self.canonical_id}.{ext}"

    # ------------------------------------------------------------------
    # Identity mutation with audit
    # ------------------------------------------------------------------

    def set_site_name(self, name: str, reason: str | None = None) -> None:
        name = (name or "").strip()
        prev = (self.site_name or "").strip()
        if name == prev:
            return
        if prev:
            self._site_name_history.append({
                "prev": prev, "next": name,
                "at": _now(), "reason": reason or "",
            })
        self.site_name = name

    def validate_identity(self) -> list[dict]:
        warnings: list[dict] = []

        if self.watershed and self.site_name:
            canonical = (self.watershed.get("data", {}) or {}).get("gauge_name") or ""
            canonical = canonical.strip()
            if canonical:
                def _norm(s: str) -> str:
                    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()
                if _norm(self.site_name) != _norm(canonical) and (
                    _norm(canonical) not in _norm(self.site_name)
                    and _norm(self.site_name) not in _norm(canonical)
                ):
                    warnings.append({
                        "code": "site_name_drift", "severity": "warn",
                        "message": (
                            f"site_name '{self.site_name}' disagrees with the "
                            f"canonical gauge_name '{canonical}' from the watershed slot."
                        ),
                        "site_name": self.site_name, "canonical_name": canonical,
                    })

        if self.site_type == "usgs_gauge" and self.site_id:
            # Legacy label check only: 7-digit strings stay acceptable as labels
            # here but are never identities (ai_hydro.identity.is_usgs_site_id, O3).
            if not (self.site_id.isdigit() and 7 <= len(self.site_id) <= 10):
                warnings.append({
                    "code": "site_id_format", "severity": "warn",
                    "message": (
                        f"site_type is 'usgs_gauge' but site_id '{self.site_id}' "
                        "is not a 7-10 digit USGS station number."
                    ),
                })

        if (self.session_id.isdigit()
                and 7 <= len(self.session_id) <= 10
                and self.site_id
                and self.site_id != self.session_id):
            warnings.append({
                "code": "session_id_gauge_mismatch", "severity": "info",
                "message": (
                    f"session_id '{self.session_id}' looks like a USGS gauge "
                    f"but the analysed site is '{self.site_id}'."
                ),
            })

        if self.workspace_dir and not Path(self.workspace_dir).is_dir():
            warnings.append({
                "code": "workspace_missing", "severity": "warn",
                "message": f"workspace_dir '{self.workspace_dir}' no longer exists.",
            })

        if self.interpretation and not self.site_name and self.site_id:
            warnings.append({
                "code": "site_name_unset", "severity": "info",
                "message": "An interpretation exists but site_name is empty.",
            })

        return warnings

    # ------------------------------------------------------------------
    # Backward-compat slot access  (set / get)
    #
    # Both write to / read from __legacy__ sentinel feature so all existing
    # tool code keeps working unchanged through the C1→C2 migration window.
    # ------------------------------------------------------------------

    def set(self, slot: str, value: dict | None) -> None:
        """Backward-compat: store a result under the __legacy__ feature.

        _run_log is special-cased: it's SQLite-backed (see _run_log_record),
        not stored in self._slots, so concurrent writers upsert individual
        rows instead of racing on a whole-session load->mutate->save.
        """
        if slot == "_run_log":
            self._set_run_log(value)
            return
        self.put_result(slot, _LEGACY_FEATURE_ID, _LEGACY_PARAMS_KEY, value)

    def _set_run_log(self, run_log_dict: dict | None) -> None:
        """
        Upsert every entry in run_log_dict into the SQLite run-log store.

        Per-row atomic upsert: even if two processes race (each read a
        slightly different snapshot via get() then both call set() with
        their own accumulated dict), neither call ever deletes a row it
        doesn't know about — the final state is the union of every writer's
        entries, not whichever writer saved last.

        Rows carrying a sealed v2 ``record`` are insert-only (see
        ``_run_log_record``): re-sending a stale snapshot of such a row is a
        no-op and never drops its record.
        """
        if not isinstance(run_log_dict, dict):
            return
        for run_id, entry in run_log_dict.items():
            if not isinstance(entry, dict):
                continue
            _run_log_record(self.session_id, run_id, entry)

    def get(self, slot: str) -> dict | None:
        """
        Backward-compat: return the active feature's latest result, or __legacy__.

        Resolution order:
        1. active_feature_id's most recent result (new @feature_tool path)
        2. __legacy__ most recent result (old tool path)
        3. None

        _run_log is special-cased: reads go straight to the SQLite store
        (see _run_log_read_all) and return the full {run_id: {...}} dict,
        matching what every existing caller already expects.
        """
        if slot == "_run_log":
            return _run_log_read_all(self.session_id) or None

        by_feature = self._slots.get(slot)
        if not by_feature:
            return None
        return self._resolve_slot(slot, by_feature, track=True)[1]

    def _resolve_slot(self, slot: str, by_feature: dict, *, track: bool = False
                      ) -> tuple[str | None, dict | None]:
        """``(feature_id, result)`` that ``get(slot)`` serves: the one slot loader.

        The active feature's latest result first, then ``__legacy__``'s. For a
        basin-dependent slot a result stamped for another basin is skipped (see
        ``ai_hydro.session.basin_identity``); an unstamped one is served and reported by
        ``unverified_slots``.
        """
        gated = slot in _bi.BASIN_DEPENDENT_SLOTS
        candidates = []
        if self.active_feature_id and self.active_feature_id != _LEGACY_FEATURE_ID:
            candidates.append(self.active_feature_id)
        candidates.append(_LEGACY_FEATURE_ID)
        for fid in candidates:
            result = _latest_result_in_feature(by_feature.get(fid, {}))
            if result is None:
                continue
            if gated:
                verdict = _bi.judge(result, self._expected_identity(fid))
                if verdict == _bi.STALE:
                    continue
                if verdict == _bi.UNVERIFIED and track:
                    self._unverified_reads.add(slot)
            return fid, result
        return None, None

    # ------------------------------------------------------------------
    # Basin identity (T1-B): which basin a stored result belongs to
    # ------------------------------------------------------------------

    def basin_identity(self) -> dict:
        """Identity of the session's own basin: its watershed geometry digest and ``site_id``."""
        by_key = self._slots.get("watershed", {}).get(_LEGACY_FEATURE_ID, {})
        ws = _latest_result_in_feature(by_key)
        digest = (_bi.identity_of(ws) or {}).get("geometry_digest") or _bi.watershed_slot_digest(ws)
        return _bi.make_identity(digest, self.site_id)

    def _expected_identity(self, feature_id: str) -> dict:
        """Identity a result stored under ``feature_id`` must carry to be this feature's."""
        if feature_id == _LEGACY_FEATURE_ID:
            return self.basin_identity()
        d = self._features.get(feature_id)
        if not isinstance(d, dict):
            return {}
        gauge = (d.get("metadata") or {}).get("gauge_id")
        return _bi.make_identity(_bi.geometry_digest(d.get("geojson")), gauge)

    def drop_slot(self, slot: str) -> bool:
        """Remove a whole product slot (every feature); True if it held anything."""
        return bool(self._slots.pop(slot, None))

    def identity_note(self) -> dict:
        """``{"basin_identity": {...}}`` when this call read a basin-dependent slot that carries
        no basin stamp, else ``{}``; consuming tools merge it into their result."""
        if not self._unverified_reads:
            return {}
        return {"basin_identity": _bi.unverified_note(self._unverified_reads)}

    def unverified_slots(self) -> list[str]:
        """Basin-dependent slots served by ``get`` that carry no basin stamp (sessions written
        before basin identity was recorded). They keep loading; they are flagged, never trusted
        silently."""
        out = []
        for slot in sorted(_bi.BASIN_DEPENDENT_SLOTS & set(self._slots)):
            fid, res = self._resolve_slot(slot, self._slots[slot], track=False)
            if res is not None and _bi.judge(res, self._expected_identity(fid)) == _bi.UNVERIFIED:
                out.append(slot)
        return out

    def _watershed_replaced(self, new_value: dict | None) -> None:
        """Called before a new session ``watershed`` is stored: stamp it, and when it is a
        different geometry than the one it replaces drop every result computed for the old
        basin, along with ``site_id`` (the old gauge), so nothing of it answers for the new one."""
        if not isinstance(new_value, dict):
            return
        prev = self.basin_identity()
        prev_digest = prev.get("geometry_digest")
        new_digest = _bi.watershed_slot_digest(new_value)
        data = new_value.get("data") if isinstance(new_value.get("data"), dict) else {}
        _bi.stamp(new_value, _bi.make_identity(new_digest, data.get("gauge_id")))
        changed = bool(prev_digest and new_digest and prev_digest != new_digest)
        expected = _bi.make_identity(new_digest, None)
        dropped: list[str] = []
        for slot in _bi.BASIN_DEPENDENT_SLOTS & set(self._slots):
            by_key = self._slots[slot].get(_LEGACY_FEATURE_ID)
            if not by_key:
                continue
            for key in [k for k, r in by_key.items()
                        if r is not None and (changed or _bi.judge(r, expected) == _bi.STALE)]:
                del by_key[key]
                if slot not in dropped:
                    dropped.append(slot)
            if not by_key:
                del self._slots[slot][_LEGACY_FEATURE_ID]
        cleared_site = None
        if changed and self.site_id and _bi.norm_gauge(self.site_id) != _bi.norm_gauge(data.get("gauge_id")):
            cleared_site = self.site_id
            self.site_id = ""
            self.site_type = ""
            self.site_name = ""
        if changed:
            log_ = self.extra.setdefault("basin_changes", [])
            log_.append({"at": _now(), "from": prev_digest, "to": new_digest,
                         "dropped_slots": sorted(dropped), "cleared_site_id": cleared_site})
            del log_[:-20]

    # ------------------------------------------------------------------
    # Provenance (unchanged API — backward compat)
    # ------------------------------------------------------------------

    def add_artifact(
        self,
        artifact_id: str,
        data: Any,
        fetch_parameters: dict,
        source: str,
        artifact_type: str = "timeseries",
        units: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Record a data artifact with provenance hashes (existing API)."""
        from ai_hydro.session.store import _hash_obj
        param_hash = _hash_obj(fetch_parameters)
        content_hash = _hash_obj(data)
        self.artifact_manifest[artifact_id] = {
            "artifact_id": artifact_id,
            "type": artifact_type,
            "source": source,
            "fetch_parameters": fetch_parameters,
            "parameter_hash": param_hash,
            "content_hash": content_hash,
            "units": units,
            "created_at": _now(),
            **(metadata or {}),
        }
        return artifact_id

    def record_result(
        self,
        slot: str,
        data: dict,
        uncertainty: dict | None = None,
        artifacts_used: list[str] | None = None,
        metric_ref: str | None = None,
        tool_name: str | None = None,
        *,
        feature_id: str | None = None,
    ) -> None:
        """
        Record a result in a session slot.

        feature_id (keyword-only): if given, stores under that feature id rather
        than __legacy__. New tools (C2+) pass this explicitly; old tools omit it.
        """
        val: dict[str, Any] = {
            "data": data,
            "meta": {
                "computed_at": _now(),
                "tool": tool_name,
                "metric_ref": metric_ref,
            },
        }
        if uncertainty:
            val["uncertainty"] = uncertainty
        if artifacts_used:
            val["artifacts_used"] = artifacts_used

        fid = feature_id or self.active_feature_id or _LEGACY_FEATURE_ID
        self.put_result(slot, fid, _LEGACY_PARAMS_KEY, val)

    def _hash(self, obj: Any) -> str:
        return _hash_obj(obj)

    # ------------------------------------------------------------------
    # Store Protocol — feature registry
    # ------------------------------------------------------------------

    def put_feature(self, feature: Any) -> None:  # feature: aihydro_core Feature
        """Register or update a Feature in the session's feature registry."""
        self._features[feature.feature_id] = feature.to_dict()

    def get_feature(self, feature_id: str) -> Any:  # → Feature | None
        """Look up a Feature by id."""
        d = self._features.get(feature_id)
        if d is None:
            return None
        try:
            from aihydro_core.primitives.geometry import Feature
            return Feature.from_dict(d)
        except Exception:
            return None

    def list_features(self) -> list[Any]:  # → list[Feature]
        """Return all registered features."""
        try:
            from aihydro_core.primitives.geometry import Feature
            return [Feature.from_dict(d) for d in self._features.values()]
        except Exception:
            return []

    def get_active_feature_id(self) -> str | None:
        """Return the active (default) feature id."""
        return self.active_feature_id

    def set_active_feature_id(self, feature_id: str) -> None:
        """Set the active (default) feature id."""
        self.active_feature_id = feature_id

    # ------------------------------------------------------------------
    # Store Protocol — keyed result store
    # ------------------------------------------------------------------

    def _run_key_outputs(self, value: dict | None) -> dict:
        if not isinstance(value, dict):
            return {}
        data = value.get("data") if isinstance(value.get("data"), dict) else value
        if not isinstance(data, dict):
            return {}
        out: dict[str, Any] = {}
        for key, item in data.items():
            if str(key).startswith("_"):
                continue
            if isinstance(item, list):
                out[f"{key}_n"] = len(item)
            elif isinstance(item, dict):
                out[key] = {k: (len(v) if isinstance(v, list) else v) for k, v in list(item.items())[:20]}
            elif isinstance(item, (str, int, float, bool)) or item is None:
                out[key] = item
            if len(out) >= 40:
                break
        return out

    def _record_put_result_run(self, product: str, value: dict | None) -> None:
        """
        Record a run-log entry for every Store Protocol write.

        Writes a single atomically-upserted SQLite row (see _run_log_record)
        instead of mutating an in-memory _run_log slot — this call fires on
        every put_result(), so under the old dict-slot design it was the
        highest-frequency of the three run-log writers and the most exposed
        to the whole-session load->mutate->save race.
        """
        if product == "_run_log" or not isinstance(value, dict):
            return
        raw_meta = value.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        tool_name = meta.get("tool") or value.get("tool_name") or product
        timestamp = meta.get("computed_at") or value.get("timestamp") or _now()
        key_outputs = self._run_key_outputs(value)
        digest_src = json.dumps(
            {"product": product, "tool": tool_name, "timestamp": timestamp, "outputs": key_outputs},
            sort_keys=True,
            default=str,
        )
        run_id = value.get("run_id") or f"{product}.{hashlib.sha1(digest_src.encode('utf-8')).hexdigest()[:12]}"
        entry = {
            "run_id": run_id,
            "tool_name": tool_name,
            "session_id": self.session_id,
            "timestamp": timestamp,
            "key_outputs": key_outputs,
            "slot": product,
        }
        from ai_hydro.session.evidence import capture_result_evidence
        entry["evidence"] = capture_result_evidence(value)
        status = _run_log_record(self.session_id, run_id, entry, writer="put_result")
        if status in ("inserted", "replaced", "noop") and isinstance(raw_meta, dict):
            # The stored result carries the id of the run-log row that
            # describes it, so a later tool that consumes this slot can name
            # the run it consumed (parents / input_refs in its own record).
            raw_meta["run_id"] = run_id

    def put_result(
        self,
        product: str,
        feature_id: str,
        params_key: str,
        value: dict | None,
    ) -> None:
        """Store a result under (product, feature_id, params_key).

        A basin-dependent result is stamped with the identity of the basin it is stored
        for (``meta.basin_identity``); a new session watershed invalidates what was
        computed for the old one (``_watershed_replaced``).
        """
        if product == "watershed" and feature_id == _LEGACY_FEATURE_ID:
            self._watershed_replaced(value)
        elif product in _bi.BASIN_DEPENDENT_SLOTS and isinstance(value, dict):
            inherited = sorted(self._unverified_reads - {product})
            if inherited:
                # Computed from a slot that cannot be tied to a basin: this result cannot be
                # either, so the unverified state is carried forward, not laundered into a stamp.
                _bi.stamp_unverified(value, inherited)
            else:
                _bi.stamp(value, self._expected_identity(feature_id))
        self._slots.setdefault(product, {}).setdefault(feature_id, {})[params_key] = value
        self._record_put_result_run(product, value)

    def get_result(
        self,
        product: str,
        feature_id: str,
        params_key: str,
    ) -> dict | None:
        """Retrieve a result. Returns None on cache miss.

        A basin-dependent result is served only when its stamp matches the basin it is
        requested for: another basin's result and an unstamped one (written before basin
        identity existed) are a miss, so the caller recomputes and stamps.
        """
        res = self._slots.get(product, {}).get(feature_id, {}).get(params_key)
        if res is not None and product in _bi.BASIN_DEPENDENT_SLOTS \
                and _bi.judge(res, self._expected_identity(feature_id)) != _bi.VERIFIED:
            return None
        return res

    def list_results(self, product: str) -> dict[str, list[str]]:
        """Return {feature_id: [params_key, ...]} for a product."""
        by_feature = self._slots.get(product, {})
        return {fid: list(by_key.keys()) for fid, by_key in by_feature.items()}

    # ------------------------------------------------------------------
    # Store Protocol — provenance (new API)
    # ------------------------------------------------------------------

    def store_artifact(self, art: Any) -> None:  # art: aihydro_core Artifact
        """Record an Artifact (aihydro_core.primitives.Artifact) in the manifest."""
        self.artifact_manifest[art.artifact_id] = art.to_dict()

    def add_citations(self, keys: list[str]) -> None:
        """Accumulate citation keys (no auto-save — caller must call save())."""
        self._citations.update(keys)

    def commit(self) -> None:
        """Persist state (Store Protocol alias for save())."""
        self.save()

    # ------------------------------------------------------------------
    # Notes with dedup
    # ------------------------------------------------------------------

    def add_note(self, note: str) -> bool:
        note = (note or "").strip()
        if not note:
            return False
        for existing in self.notes[-5:]:
            if existing.strip() == note:
                if self.notes[-1].strip() == note:
                    return False
        if len(self.notes) >= 500:
            self.notes = self.notes[-499:]
        self.notes.append(note)
        return True

    # ------------------------------------------------------------------
    # Backward-compat property accessors for the 9 common slots
    #
    # Getters call self.get() → returns active or __legacy__ result.
    # Setters call self.set() → stores under __legacy__ (old tools).
    # ------------------------------------------------------------------

    @property
    def watershed(self) -> dict | None:
        return self.get("watershed")

    @watershed.setter
    def watershed(self, v: dict | None) -> None:
        self.set("watershed", v)

    @property
    def streamflow(self) -> dict | None:
        return self.get("streamflow")

    @streamflow.setter
    def streamflow(self, v: dict | None) -> None:
        self.set("streamflow", v)

    @property
    def signatures(self) -> dict | None:
        return self.get("signatures")

    @signatures.setter
    def signatures(self, v: dict | None) -> None:
        self.set("signatures", v)

    @property
    def geomorphic(self) -> dict | None:
        return self.get("geomorphic")

    @geomorphic.setter
    def geomorphic(self, v: dict | None) -> None:
        self.set("geomorphic", v)

    @property
    def camels(self) -> dict | None:
        return self.get("camels")

    @camels.setter
    def camels(self, v: dict | None) -> None:
        self.set("camels", v)

    @property
    def forcing(self) -> dict | None:
        return self.get("forcing")

    @forcing.setter
    def forcing(self, v: dict | None) -> None:
        self.set("forcing", v)

    @property
    def twi(self) -> dict | None:
        return self.get("twi")

    @twi.setter
    def twi(self, v: dict | None) -> None:
        self.set("twi", v)

    @property
    def cn(self) -> dict | None:
        return self.get("cn")

    @cn.setter
    def cn(self, v: dict | None) -> None:
        self.set("cn", v)

    @property
    def model(self) -> dict | None:
        return self.get("model")

    @model.setter
    def model(self, v: dict | None) -> None:
        self.set("model", v)

    # ------------------------------------------------------------------
    # Persistence — atomic write-then-rename
    # ------------------------------------------------------------------

    @classmethod
    def _path(cls, session_id: str, shard_id: str | None = None) -> Path:
        safe_session = _safe_filename_component(session_id)
        if shard_id:
            safe_shard = _safe_filename_component(shard_id)
            candidate = _SESSIONS_DIR / f"{safe_session}.{safe_shard}.shard.json"
        else:
            candidate = _SESSIONS_DIR / f"{safe_session}.json"
        contained = _contained_path(candidate)
        if contained is None:
            # Should be unreachable given the sanitizer above, but fail closed
            # rather than resolve outside SESSIONS_DIR under any edge case.
            raise ValueError(
                f"Refusing to resolve session path outside SESSIONS_DIR: "
                f"session_id={session_id!r} shard_id={shard_id!r}"
            )
        return contained

    @classmethod
    def _legacy_raw_path(cls, session_id: str, shard_id: str | None = None) -> Path | None:
        """
        Read-only discovery of a session file written before path
        sanitization was introduced (raw, un-sanitized session_id/shard_id
        interpolated directly into the filename). `save()` never writes here
        — only `load()` falls back to it, and only if it still resolves
        inside SESSIONS_DIR, so this cannot be used to read outside the
        sessions directory.
        """
        if shard_id:
            candidate = _SESSIONS_DIR / f"{session_id}.{shard_id}.shard.json"
        else:
            candidate = _SESSIONS_DIR / f"{session_id}.json"
        return _contained_path(candidate)

    @classmethod
    def exists(cls, session_id: str) -> bool:
        """True iff a saved session file exists for ``session_id`` (current or legacy name).

        ``load`` cannot answer this: it returns an empty session for an unknown id, and the next
        ``save()`` turns that into a real study (R-01). Tools that write into a study must check
        here (through ``resolve_existing_study``) before loading.
        """
        try:
            path = cls._path(session_id)
            if path.exists():
                return True
            legacy = cls._legacy_raw_path(session_id)
            return legacy is not None and legacy.exists()
        except Exception:
            return False

    @classmethod
    def load(cls, session_id: str, shard_id: str | None = None) -> HydroSession:
        """Load an existing session or return a new empty one (see ``exists``)."""
        path = cls._path(session_id, shard_id)
        if not path.exists():
            legacy_path = cls._legacy_raw_path(session_id, shard_id)
            if legacy_path is not None and legacy_path != path and legacy_path.exists():
                path = legacy_path
            else:
                return cls(session_id, shard_id)
        base_text: str | None = None
        stamp: Any = None
        try:
            with open(path) as f:
                base_text = f.read()
                if path == cls._path(session_id, shard_id):
                    stamp = _stamp_of(os.fstat(f.fileno()))
            raw = json.loads(base_text)
        except json.JSONDecodeError as exc:
            log.error("Session file at %s is corrupted: %s", path, exc)
            raise RuntimeError(
                f"Session file '{path}' is corrupted JSON ({exc}). "
                "Affected session_id: " + session_id
            ) from exc

        session = cls(session_id, shard_id)
        session._base_text = base_text
        session._disk_stamp = stamp

        # Keys that are stored at the top level but are NOT product slots.
        _META_KEYS = {
            "session_id", "site_name", "site_id", "site_type",
            "workspace_dir", "working_geometry_path", "notes", "created_at",
            "updated_at", "interpretation", "interpretation_at", "archived",
            "_citations", "artifact_manifest", "claims", "assumptions",
            "_site_name_history",
            "gauge_id",          # legacy key
            "_hydro_slots_v2",   # C1 schema version marker
            "_features",         # C1 feature registry
            "active_feature_id", # C1 active feature
            "extra",             # Phase 3.4+ free-form extension metadata
        }

        is_v2 = raw.get("_hydro_slots_v2", False)

        for key, val in raw.items():
            if key in _META_KEYS:
                continue
            if key == "_run_log":
                # _run_log moved to a SQLite-backed store (see
                # _run_log_migrate_legacy) and is no longer kept in
                # self._slots / re-serialized into session JSON. Any
                # pre-migration data found here is imported once, idempotently.
                if is_v2 and isinstance(val, dict):
                    legacy_flat = val.get(_LEGACY_FEATURE_ID, {}).get(_LEGACY_PARAMS_KEY, {}) or {}
                elif isinstance(val, dict):
                    legacy_flat = val
                else:
                    legacy_flat = {}
                if legacy_flat:
                    _run_log_migrate_legacy(session_id, legacy_flat)
                continue
            if isinstance(val, dict) or val is None:
                if is_v2:
                    # New format: already a 3-level dict — load directly
                    session._slots[key] = val if isinstance(val, dict) else {}
                else:
                    # Old format: migrate single-value slot → 3-level
                    # {data, meta} → {__legacy__: {"": {data, meta}}}
                    session._slots[key] = {_LEGACY_FEATURE_ID: {_LEGACY_PARAMS_KEY: val}}

        # Metadata fields
        session.site_name = raw.get("site_name", "")
        session.site_id = raw.get("site_id", "") or raw.get("gauge_id", "")
        session.site_type = raw.get("site_type", "")
        session.workspace_dir = raw.get("workspace_dir")
        session.working_geometry_path = raw.get("working_geometry_path")
        session.notes = raw.get("notes", [])
        session.interpretation = raw.get("interpretation", "")
        session.interpretation_at = raw.get("interpretation_at")
        session.archived = raw.get("archived", False)
        session.created_at = raw.get("created_at", session.created_at)
        session.updated_at = raw.get("updated_at", session.updated_at)
        session._citations = set(raw.get("_citations", []))
        session.artifact_manifest = raw.get("artifact_manifest", {})
        session.claims = raw.get("claims", {})
        session.assumptions = raw.get("assumptions", {})
        session.extra = raw.get("extra", {})
        session._site_name_history = raw.get("_site_name_history", [])

        # C1: feature registry (absent in old sessions → empty, active → None)
        session._features = raw.get("_features", {})
        session.active_feature_id = raw.get("active_feature_id")

        return session

    def save(self, *, sync_context: bool = False) -> None:
        """Persist atomically (write temp, rename over target).

        ``sync_context`` makes sure ``.aihydrorules/research.md`` is the retirement stub (T-R1). The file
        is no longer a digest and is never rewritten once it is the stub, so a save cannot change the
        agent's instructions mid-task (the F-TS6 / session-study M3 problem). The digest is the
        ``aihydro://project/{prj}/context`` resource.
        """
        # A call the client gave up on must not write its result afterwards (the run record says it was
        # cancelled). Raising also stops the abandoned worker at its save instead of letting it run on.
        from ai_hydro.core.cancel import check_cancelled

        check_cancelled()
        self.updated_at = _now()
        try:
            for w in self.validate_identity():
                if w.get("severity") in ("warn", "error"):
                    log.warning("[session %s] %s: %s",
                                self.session_id, w.get("code"), w.get("message"))
        except Exception:
            pass
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        target = self._path(self.session_id, self.shard_id)
        ours = _json_safe(self._to_raw())
        with _session_write_guard(target, self.session_id):
            try:
                cur = _stamp_of(target.stat())
            except OSError:
                cur = None
            merged = False
            if self._disk_stamp is not _NO_MERGE and cur != self._disk_stamp:
                if cur is not None:                      # another writer saved since we loaded
                    try:
                        theirs_text = target.read_text()
                    except OSError:
                        theirs_text = ""
                    if theirs_text:
                        ours = _merge_session_raw(self._base_text, ours, theirs_text, self.session_id)
                        merged = True
            tmp = target.with_suffix(
                f"{target.suffix}.tmp.{os.getpid()}.{int(time.time() * 1000)}.{threading.get_ident()}"
            )
            payload = json.dumps(ours, indent=2, allow_nan=False)
            with open(tmp, "w") as f:
                f.write(payload)
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, target)
            if merged:
                # The in-memory object does not hold the other writer's changes: keep merging on
                # the next save, with this object's own state as the baseline.
                self._base_text = json.dumps(_json_safe(self._to_raw()), indent=2, allow_nan=False)
                self._disk_stamp = _FORCE_MERGE
            else:
                self._base_text = payload
                try:
                    self._disk_stamp = _stamp_of(target.stat())
                except OSError:
                    self._disk_stamp = _FORCE_MERGE
        if sync_context and not self.shard_id:
            try:
                self.write_research_context()
            except Exception as exc:
                log.warning("research.md write failed (session saved OK): %s", exc)

    def archive(self) -> None:
        self.archived = True
        self.save(sync_context=True)

    def _to_raw(self) -> dict:
        raw: dict[str, Any] = {
            "session_id": self.session_id,
            "site_name": self.site_name,
            "site_id": self.site_id,
            "site_type": self.site_type,
            "workspace_dir": self.workspace_dir,
            "working_geometry_path": self.working_geometry_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived": self.archived,
            "notes": self.notes,
            "interpretation": self.interpretation,
            "interpretation_at": self.interpretation_at,
            "_citations": sorted(self._citations),
            "artifact_manifest": self.artifact_manifest,
            "claims": self.claims,
            "assumptions": self.assumptions,
            "extra": self.extra,
            "_site_name_history": self._site_name_history,
            # C1 additions
            "_hydro_slots_v2": True,
            "_features": self._features,
            "active_feature_id": self.active_feature_id,
        }
        # Serialize 3-level slots, applying lean to innermost result dicts
        for slot, by_feature in self._slots.items():
            raw[slot] = _lean_product_slot(by_feature)
        return raw

    # ------------------------------------------------------------------
    # Workspace file writing
    # ------------------------------------------------------------------

    def write_workspace_file(self, filename: str, content: Any) -> str | None:
        if not self.workspace_dir:
            return None
        out_path = Path(self.workspace_dir) / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            if isinstance(content, str):
                f.write(content)
            else:
                json.dump(content, f, indent=2)
        try:                                   # the study recorder records it once the call is sealed
            from ai_hydro.session.run_records import note_file_written
            if not self.shard_id:              # shard writes are merged with the shard, not recorded here
                note_file_written(out_path, self.session_id)
        except Exception:
            pass
        return str(out_path)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def computed(self) -> list[str]:
        """Products that have at least one non-None result across any feature."""
        result = []
        for product, by_feature in self._slots.items():
            if any(
                v is not None
                for by_key in by_feature.values()
                for v in by_key.values()
            ):
                result.append(product)
        return result

    def _artifact_roots(self) -> list[Path]:
        roots = [_SESSIONS_DIR]
        if self.workspace_dir:
            roots.append(Path(self.workspace_dir).expanduser())
        return roots

    def computed_healthy(self) -> list[str]:
        """``computed()`` without the slots whose artifact or conventions are unhealthy."""
        health = self.slot_health()
        return [s for s in self.computed() if health.get(s, {}).get("health", "ok") == "ok"]

    def degraded(self) -> list[dict]:
        """``[{"slot", "health", "reason", "fix"}]`` for every slot whose artifact or conventions are unhealthy
        (the ``degraded`` list of ``summary``); empty when all are ok."""
        return [
            {"slot": slot, "health": h["health"], "reason": h["reason"], "fix": _SLOT_HEALTH_FIX[h["health"]]}
            for slot, h in self.slot_health().items() if h["health"] != "ok"
        ]

    def slot_health(self) -> dict[str, dict]:
        """``{slot: {"health": ..., "reason": ..., "feature_id": ...}}`` for every computed slot.

        Health is the worst across the slot's features (latest result each); see ``_result_health``.
        """
        out: dict[str, dict] = {}
        roots = self._artifact_roots()
        for slot, by_feature in self._slots.items():
            worst: tuple[str, str | None, str] | None = None
            for fid, by_key in by_feature.items():
                result = _latest_result_in_feature(by_key)
                if result is None:
                    continue
                health, reason = _result_health(slot, result, roots)
                if worst is None or SLOT_HEALTH_ORDER.index(health) < SLOT_HEALTH_ORDER.index(worst[0]):
                    worst = (health, reason, fid)
            if worst is not None:
                out[slot] = {"health": worst[0], "reason": worst[1], "feature_id": worst[2]}
        return out

    def pending(self) -> list[str]:
        """Common slots not yet computed for the active/legacy feature, or computed but degraded
        (see ``slot_health``): either way the tool needs running."""
        health = self.slot_health()
        return [s for s in _COMMON_SLOTS
                if self.get(s) is None or health.get(s, {}).get("health", "ok") != "ok"]

    def is_stale(self, field: str) -> bool:
        if self.archived:
            return True
        if field in self._slots:
            result = self.get(field)
            if not result:
                return False
            ts_str = result.get("meta", {}).get("computed_at")
            if not ts_str:
                return False
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - ts
                return delta.days > STALENESS_THRESHOLD["computed"]
            except Exception:
                return False
        if field == "interpretation":
            if not self.interpretation_at:
                return False
            try:
                ts = datetime.fromisoformat(self.interpretation_at.replace("Z", "+00:00"))
                return (datetime.now(timezone.utc) - ts).days > STALENESS_THRESHOLD["interpretation"]
            except Exception:
                return False
        if field == "notes":
            try:
                ts = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
                return (datetime.now(timezone.utc) - ts).days > STALENESS_THRESHOLD["notes"]
            except Exception:
                return False
        return False

    def summary(self) -> dict:
        warnings = self.validate_identity()
        health = self.slot_health()
        degraded = [
            {"slot": slot, "health": h["health"], "reason": h["reason"],
             "fix": _SLOT_HEALTH_FIX[h["health"]]}
            for slot, h in health.items() if h["health"] != "ok"
        ]
        out: dict[str, Any] = {
            "session_id": self.session_id,
            "site_name": self.site_name,
            "site_id": self.site_id,
            "site_type": self.site_type,
            "canonical_id": self.canonical_id,
            "archived": self.archived,
            "computed": [s for s in self.computed() if health.get(s, {}).get("health", "ok") == "ok"],
            "slot_health": {slot: h["health"] for slot, h in health.items()},
            "pending": self.pending(),
            "notes": self.notes,
            "has_interpretation": bool(self.interpretation),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        # C1: surface registered features if any exist beyond __legacy__
        real_features = {k: v for k, v in self._features.items()
                         if k != _LEGACY_FEATURE_ID}
        if real_features:
            out["features"] = [
                {"feature_id": k, "name": v.get("name", ""), "source": v.get("source", "")}
                for k, v in real_features.items()
            ]
            out["active_feature_id"] = self.active_feature_id
        if degraded:
            out["degraded"] = degraded
        if warnings:
            out["identity_warnings"] = warnings
        unverified = self.unverified_slots()
        if unverified:
            out["basin_identity"] = _bi.unverified_note(unverified)
        return out

    def to_json(self) -> str:
        # Export form (capsule session.json, export_session): local absolute
        # paths become location-independent refs so no home dir leaves the machine.
        from ai_hydro.session.refs import portable

        return json.dumps(portable(self._to_raw(), self.workspace_dir), indent=2)

    # ------------------------------------------------------------------
    # Citation management
    # ------------------------------------------------------------------

    def get_citations(self) -> set[str]:
        return set(self._citations)

    def export_bibtex(self) -> str:
        from ai_hydro.citations import build_bibtex
        return build_bibtex(self._citations)

    def cite_all(self) -> str:
        return self.export_bibtex()

    # ------------------------------------------------------------------
    # Synopsis for LLM
    #
    # Single-feature sessions: flat dict (identical to pre-C1 output).
    # Multi-feature sessions: nested by feature_id per slot.
    # ------------------------------------------------------------------

    def synopsis_for_llm(self) -> dict:
        """Concise per-slot summaries for LLM reasoning."""
        out: dict = {}
        for slot in self.computed():
            by_feature = self._slots.get(slot, {})

            # Separate real features from legacy sentinel
            real = {k: v for k, v in by_feature.items() if k != _LEGACY_FEATURE_ID}
            legacy = by_feature.get(_LEGACY_FEATURE_ID, {})

            if real:
                # Multi-feature: nest by feature_id
                out[slot] = {}
                for fid, by_key in real.items():
                    result = _latest_result_in_feature(by_key)
                    if result:
                        name = self._features.get(fid, {}).get("name", fid)
                        key = name if name else fid
                        out[slot][key] = _synopsis_from_result(result)
                # Also include legacy if present
                result = _latest_result_in_feature(legacy)
                if result:
                    out[slot][_LEGACY_FEATURE_ID] = _synopsis_from_result(result)
            else:
                # Single-feature (legacy only): flat, same as before
                result = _latest_result_in_feature(legacy)
                if result:
                    out[slot] = _synopsis_from_result(result)

        return out

    def raw_session_data(self) -> dict:
        return self.synopsis_for_llm()

    # ------------------------------------------------------------------
    # research.md sync
    # ------------------------------------------------------------------

    def write_research_context(self) -> None:
        """Retired (T-R1): ``.aihydrorules/research.md`` is now a stub, written once and never refreshed.

        The study digest is served by ``aihydro://project/{prj}/context?chat=``
        (``ai_hydro.session.study_context``). The method keeps its name for its callers.
        """
        from ai_hydro.session.study_context import write_research_md_stub

        write_research_md_stub(self.workspace_dir)

    def _display_name_str(self) -> str:
        if self.watershed:
            name = self.watershed.get("data", {}).get("gauge_name", "")
            if name and name not in (self.site_name, self.site_id, self.session_id):
                return f" ({name})"
        return ""


# --------------------------------------------------------------------------- #
# Module-level helpers (used internally and by helpers.py)
# --------------------------------------------------------------------------- #

def _hash_obj(obj: Any) -> str:
    """Deterministic SHA-256 hash of a serialisable object (16-char hex)."""
    import hashlib
    try:
        s = json.dumps(obj, sort_keys=True, default=str)
    except Exception:
        s = str(obj)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Teach aihydro-core's jobs block where session-scoped run dirs live.
# This is the DOWNWARD edge (tools → core): the session layer registers a
# provider into core, so core never has to import this module. Replaces the
# old `from ai_hydro.session.store import _SESSIONS_DIR` reach-up inside
# aihydro_core.jobs._resolve_artifact_dir.
# --------------------------------------------------------------------------- #
def _session_run_dir_candidates(job_id: str) -> list[Path]:
    """Map a job_id → candidate run dirs derived from every session's workspace."""
    out: list[Path] = []
    if _SESSIONS_DIR.exists():
        for sf in _SESSIONS_DIR.glob("*.json"):
            try:
                ws = json.loads(sf.read_text()).get("workspace_dir")
                if ws:
                    out.append(Path(ws) / "runs" / job_id)
            except (OSError, json.JSONDecodeError):
                pass
    return out


try:
    from aihydro_core.jobs import register_artifact_dir_provider as _register_adp
    _register_adp(_session_run_dir_candidates)
except Exception:   # pragma: no cover - core jobs block is optional at import time
    pass
