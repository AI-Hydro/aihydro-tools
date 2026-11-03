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
#: Rows per study. A host that ends more agents than this in one study is out of control; the
#: insert is refused (AGENT_RUN_LIMIT) so the table and every capsule stay bounded.
MAX_RUNS_PER_STUDY = 500

#: Fields of a record that reach a capsule. Anything else the host sent stays in the study's
#: database but is withheld from the export and named in the file's ``withheld_fields``; a new
#: field is exported only after it is added here on purpose. ``prompt`` is separate (flag).
EXPORT_FIELDS = (
    "agent_id", "agent_run_id", "parent_agent_id", "role", "definition_sha256", "team_id", "lease",
    "status", "end_state", "model_requested", "model_returned", "started_at", "ended_at",
    "budget", "usage", "approvals", "denied", "refused", "budget_stops", "tool_calls",
    "runs_created", "report_sha256", "prompt_sha256",
    "allowlist_sha256", "profile", "budget_stop",
)
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
    agent_id = record.get("agent_run_id")
    if agent_id is None:
        agent_id = record.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip() or len(agent_id) > _ID_MAX:
        raise AgentRunError("AGENT_RUN_INVALID",
                            f"record needs a non-empty string agent_id or agent_run_id (at most {_ID_MAX} characters)")
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


def insert_agent_run(session_id: str, row: dict) -> tuple[str, dict]:
    """Insert-only store. ``("inserted" | "noop", stored_row)``; ``noop`` means the same body is
    already stored, and ``stored_row`` is the row as stored (its digest, not the new row's).

    Raises ``AgentRunError('AGENT_RUN_CONFLICT')`` for a different body under an existing id.
    """
    from ai_hydro.session import store

    deadline = store._new_deadline()

    def _txn() -> tuple[str, dict]:
        conn = store._run_log_connect(session_id, deadline=deadline)
        try:
            conn.isolation_level = None            # explicit transaction: the lookup, count and insert are one unit
            conn.execute(_DDL)
            conn.execute("BEGIN IMMEDIATE")        # takes the write lock first, so two writers of one id serialise
            try:
                cur = conn.execute("SELECT row_json FROM agent_runs WHERE agent_run_id = ?",
                                   (row["agent_run_id"],)).fetchone()
                if cur is not None:
                    old = json.loads(cur[0])
                    conn.execute("ROLLBACK")
                    if _body_digest(old) == _body_digest(row):
                        return "noop", old
                    raise AgentRunError(
                        "AGENT_RUN_CONFLICT",
                        f"agent run {row['agent_run_id']!r} is already recorded with a different body; "
                        "agent run records are insert-only")
                n = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
                if n >= MAX_RUNS_PER_STUDY:
                    conn.execute("ROLLBACK")
                    raise AgentRunError(
                        "AGENT_RUN_LIMIT",
                        f"this study already holds {n} agent run records (limit {MAX_RUNS_PER_STUDY})")
                conn.execute(
                    "INSERT INTO agent_runs (agent_run_id, recorded_at, record_digest, row_json) VALUES (?,?,?,?)",
                    (row["agent_run_id"], row["recorded_at"], row["record_digest"],
                     json.dumps(row, sort_keys=True, ensure_ascii=False)))
                conn.execute("COMMIT")
                return "inserted", row
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
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


# ----------------------------------------------------------------------------- export filter
_SCALAR_MAPS = ("budget", "usage", "approvals", "budget_stops", "lease")
_ITEM_LISTS = ("tool_calls", "denied", "refused")
_ITEM_KEYS = ("tool", "name", "toolKey", "args_sha256", "argsSha256", "status", "run_id", "approval",
              "effect_class", "effectClass", "reason", "code", "at", "duration_ms", "server", "result")
_MAX_ITEMS = 200
_MAX_STR = 500


def _scalar(v: Any) -> bool:
    return v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str))


def _clip(v: Any) -> Any:
    return v[:_MAX_STR] if isinstance(v, str) else v


def export_record(record: dict, *, include_prompt: bool = False) -> tuple[dict, list[str], list[str]]:
    """``(exported part, withheld names, truncated names)`` of ``record`` for a capsule.

    Top-level fields come from ``EXPORT_FIELDS`` only. Inside them the same rule applies one level
    down: maps (``budget``, ``usage``, ``approvals``, ``budget_stops``, ``lease``) keep scalar values
    only; lists of calls (``tool_calls``, ``denied``, ``refused``) keep ``_ITEM_KEYS`` with scalar
    values (at most 200 items); ``runs_created`` is a list of strings; every other allowlisted field
    must be a scalar. Free strings stay (``reason``, ``approval``, ``name``, ``tool``, ``status`` and the
    like are the host's text, scrubbed for local paths when stored) but are cut at 500 characters; each
    field cut is named in the third list (``field`` or ``field.key``). Dropped parts are named in the second.
    """
    keep = set(EXPORT_FIELDS) | ({"prompt"} if include_prompt else set())
    out: dict[str, Any] = {}
    withheld: list[str] = []
    cut: list[str] = []

    def clip(name: str, v: Any) -> Any:
        if isinstance(v, str) and len(v) > _MAX_STR and name != "prompt":
            cut.append(name)
            return v[:_MAX_STR]
        return v

    for k in sorted(record):
        v = record[k]
        if k not in keep:
            withheld.append(k)
        elif k in _SCALAR_MAPS and isinstance(v, dict):
            out[k] = {kk: clip(f"{k}.{kk}", vv) for kk, vv in v.items() if _scalar(vv)}
            withheld += [f"{k}.{kk}" for kk, vv in v.items() if not _scalar(vv)]
        elif k in _ITEM_LISTS and isinstance(v, list):
            items = []
            for i, it in enumerate(v[:_MAX_ITEMS]):
                if isinstance(it, dict):
                    items.append({kk: clip(f"{k}.{kk}", vv) for kk, vv in it.items()
                                  if kk in _ITEM_KEYS and _scalar(vv)})
                    withheld += sorted({f"{k}.{kk}" for kk, vv in it.items()
                                        if kk not in _ITEM_KEYS or not _scalar(vv)})
                elif _scalar(it):
                    items.append(clip(k, it))
                else:
                    withheld.append(f"{k}[{i}]")
            if len(v) > _MAX_ITEMS:
                withheld.append(f"{k}[{_MAX_ITEMS}:]")
            out[k] = items
        elif k == "runs_created" and isinstance(v, list):
            out[k] = [clip(k, x) for x in v[:_MAX_ITEMS] if isinstance(x, str)]
            if len(v) > _MAX_ITEMS or any(not isinstance(x, str) for x in v):
                withheld.append(k + ".extra")
        elif _scalar(v):
            out[k] = clip(k, v)
        else:
            withheld.append(k)
    return out, sorted(set(withheld)), sorted(set(cut))
