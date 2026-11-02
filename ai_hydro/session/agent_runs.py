"""Agent run records: what a host says a subagent did in a study (orchestration Stage 1).

A host that runs subagents keeps a record per child locally. Local records cannot reach a
capsule, because export runs on the server over ``~/.aihydro/sessions``. The host therefore
hands each finished child's record to the server (``aihydro_record_agent_run``, a host-audience
tool models cannot call), and the server stores it in the study's run-log database, in a table
of its own, and ``export_session`` writes it to ``agents/<id>.json``.

Properties:

* insert-only: the same record again is a no-op; a different body under an existing id is
  refused (``AGENT_RUN_CONFLICT``). Nothing here updates or deletes a row;
* every row carries ``record_digest``, a SHA-256 over the canonical form of the row (schema,
  id, session, ``asserted_by`` and the record), so the capsule copy can be re-hashed;
* ``asserted_by`` is always ``"client"``. The server cannot verify what the host reports; the
  digest shows the stored record is unchanged, not that it is true. What a child ran is
  independently visible in the sealed run records that name it (``extra.agent``);
* local absolute paths are scrubbed from the record before it is stored (same rule as run-log
  rows), and the prompt text is withheld from the default export (``prompt_sha256`` stays).

The table is created lazily, on the first insert, so a study that never had an agent keeps a
run-log database byte-for-byte as before.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from typing import Any

log = logging.getLogger("ai_hydro.session.agent_runs")

SCHEMA = "aihydro.agent_run/1"
MAX_RECORD_BYTES = 256 * 1024
_ID_MAX = 200

_DDL = ("CREATE TABLE IF NOT EXISTS agent_runs "
        "(agent_run_id TEXT PRIMARY KEY, recorded_at TEXT, record_digest TEXT, row_json TEXT)")


class AgentRunError(ValueError):
    """The record handed in is not storable (shape, size, or conflict). ``code`` names the refusal."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _digest(obj: Any) -> str:
    from aihydro_core.records.canonical import digest

    return digest(obj)


def _row_digest(row: dict) -> str:
    return _digest({k: v for k, v in row.items() if k != "record_digest"})


def build_row(session_id: str, record: Any, *, workspace_dir: str | None = None,
              recorded_at: str | None = None) -> dict:
    """Validate ``record`` and return the row to store (scrubbed, digested). Raises ``AgentRunError``."""
    if not isinstance(record, dict):
        raise AgentRunError("AGENT_RUN_INVALID", "record must be an object")
    agent_id = record.get("agent_run_id") or record.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip() or len(agent_id) > _ID_MAX:
        raise AgentRunError("AGENT_RUN_INVALID",
                            f"record needs a non-empty string agent_id (at most {_ID_MAX} characters)")
    try:
        raw = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AgentRunError("AGENT_RUN_INVALID", f"record is not plain JSON: {exc}") from exc
    if len(raw.encode("utf-8")) > MAX_RECORD_BYTES:
        raise AgentRunError("AGENT_RUN_INVALID", f"record exceeds {MAX_RECORD_BYTES} bytes")

    from ai_hydro.session.refs import scrub_value

    clean = scrub_value(json.loads(raw), workspace_dir)
    prompt = clean.get("prompt")
    if isinstance(prompt, str) and "prompt_sha256" not in clean:
        # hash the text as the host sent it, so the hash identifies the real prompt
        clean["prompt_sha256"] = hashlib.sha256(
            (record.get("prompt") or "").encode("utf-8")).hexdigest()
    if recorded_at is None:
        from datetime import datetime, timezone

        recorded_at = datetime.now(timezone.utc).isoformat()
    row = {"schema": SCHEMA, "agent_run_id": agent_id.strip(), "session_id": session_id,
           "asserted_by": "client", "recorded_at": recorded_at, "record": clean}
    row["record_digest"] = _row_digest(row)
    return row


def _body_digest(row: dict) -> str:
    """Digest of what the host said, ignoring when the server received it."""
    return _digest({"agent_run_id": row.get("agent_run_id"), "record": row.get("record")})


def insert(session_id: str, row: dict) -> str:
    """Insert-only store. ``"inserted"`` | ``"noop"`` (same body already stored).

    Raises ``AgentRunError('AGENT_RUN_CONFLICT')`` for a different body under an existing id.
    """
    from ai_hydro.session import store

    deadline = store._new_deadline()

    def _txn() -> str:
        conn = store._run_log_connect(session_id, deadline=deadline)
        try:
            conn.execute(_DDL)
            cur = conn.execute("SELECT row_json FROM agent_runs WHERE agent_run_id = ?",
                               (row["agent_run_id"],)).fetchone()
            if cur is not None:
                old = json.loads(cur[0])
                if _body_digest(old) == _body_digest(row):
                    return "noop"
                raise AgentRunError(
                    "AGENT_RUN_CONFLICT",
                    f"agent run {row['agent_run_id']!r} is already recorded with a different body; "
                    "agent run records are insert-only")
            conn.execute(
                "INSERT INTO agent_runs (agent_run_id, recorded_at, record_digest, row_json) VALUES (?,?,?,?)",
                (row["agent_run_id"], row["recorded_at"], row["record_digest"],
                 json.dumps(row, sort_keys=True, ensure_ascii=False)))
            conn.commit()
            return "inserted"
        finally:
            conn.close()

    return store._retry_when_locked(_txn, deadline=deadline)


def read_all(session_id: str, *, strict: bool = False) -> dict[str, dict]:
    """``{agent_run_id: row}`` in recording order. Never creates the database or the table."""
    from ai_hydro.session import store

    if not store._run_log_db_path(session_id).exists():
        return {}
    try:
        conn = sqlite3.connect(str(store._run_log_db_path(session_id)), timeout=5.0)
        try:
            has = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_runs'").fetchone()
            if not has:
                return {}
            rows = conn.execute(
                "SELECT agent_run_id, row_json FROM agent_runs ORDER BY recorded_at, agent_run_id").fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Failed to read agent runs for session %s: %s", session_id, exc)
        if strict:
            raise
        return {}
    out: dict[str, dict] = {}
    for rid, raw in rows:
        try:
            out[rid] = json.loads(raw)
        except ValueError:
            out[rid] = {"agent_run_id": rid, "unreadable": True}
    return out


def verify_row(row: dict) -> bool:
    """True iff the row's ``record_digest`` matches its content."""
    try:
        return isinstance(row.get("record_digest"), str) and row["record_digest"] == _row_digest(row)
    except Exception:
        return False


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def file_stem(agent_run_id: str) -> str:
    """A filesystem-safe name for ``agents/<stem>.json``; a digest suffix keeps distinct ids distinct."""
    stem = _SAFE.sub("_", agent_run_id).strip("._") or "agent"
    if stem != agent_run_id:
        stem = f"{stem[:60]}-{hashlib.sha256(agent_run_id.encode('utf-8')).hexdigest()[:8]}"
    return stem[:100]
