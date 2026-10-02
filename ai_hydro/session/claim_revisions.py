"""Sealed, insert-only claim revision store (ADR-001 / ADR-002, slice 2).

This module is the **only writer** of claim revisions. Each authority-bearing
change to a claim appends exactly one sealed
:class:`aihydro_core.records.ClaimRevision` to a per-session SQLite file,
``<sessions_dir>/<session_id>.claims.sqlite3``, next to the session JSON and run
log. The sessions directory is read from ``ai_hydro.session.store`` at call
time so tests (and the run-log store) agree on one location.

Guarantees
----------
* ``PRIMARY KEY (claim_id, revision)`` and plain ``INSERT``: a second write of
  the same ``(claim_id, revision)`` is refused, never replaced.
* ``BEFORE UPDATE`` / ``BEFORE DELETE`` triggers abort any attempt to change or
  remove a sealed row through SQL.
* Read-modify-insert runs inside ``BEGIN IMMEDIATE``, so concurrent writers get
  consecutive revision numbers instead of losing one.
* Every row read back is re-verified (seal, and chain link to its predecessor);
  a row that fails raises :class:`ClaimRevisionIntegrityError`. Callers that
  gate authority on the latest revision therefore fail closed.

What a seal is not: it proves integrity, not origin. A process running as the
same OS user can rewrite the SQLite file and re-seal. The approval signing work
(ADR-002b) addresses origin.

``revision_digest`` is ``digest(fields)`` where ``fields`` is
``ai_hydro.approval.records.claim_revision_fields(...)``
(``aihydro.claim_revision/2``, including ``evidence_versions``). It is the same
value an approval binds to.

No external anchor
------------------
The chain is self-contained. Truncating its tail, or editing the latest row
and re-sealing it, cannot be detected from the SQLite file alone. The anchor
that exists today is the registry: promotion stamps ``claim_revision_digest``
and ``claim_revision`` on the registry row, and ``check_registry_staleness``
compares them with this chain (``revision_chain_mismatch``).

Legacy claims (present in the session, no history) get revision 0 with
``cause.reason == "legacy_unrecorded"`` on first touch. That row records the
claim's state when first seen; nothing earlier is back-filled.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any, Dict, List, Optional

from aihydro_core.records import Actor, ClaimRevision, digest, verify_chain

REASONS = (
    "created", "redefined", "status_update", "promotion", "staleness",
    "evidence_drift", "out_of_band_edit", "legacy_unrecorded",
)

DEFAULT_ACTOR = {"kind": "package", "id": "aihydro-tools"}


class ClaimRevisionError(Exception):
    """Base class for claim revision store errors."""


class ClaimRevisionConflict(ClaimRevisionError):
    """A write targeted an existing ``(claim_id, revision)``."""


class ClaimRevisionIntegrityError(ClaimRevisionError):
    """A stored row failed seal or chain verification."""


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _db_path(session_id: str):
    from ai_hydro.session import store

    safe = store._safe_filename_component(session_id)
    path = store._SESSIONS_DIR / f"{safe}.claims.sqlite3"
    if store._contained_path(path) is None:
        raise ValueError(
            f"Refusing to resolve claim-revision db outside SESSIONS_DIR: session_id={session_id!r}")
    return path


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS claim_revisions (
        claim_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        revision_digest TEXT NOT NULL,
        record_digest TEXT NOT NULL,
        row_json TEXT NOT NULL,
        PRIMARY KEY (claim_id, revision))""",
    """CREATE TRIGGER IF NOT EXISTS claim_revisions_no_update
        BEFORE UPDATE ON claim_revisions
        BEGIN SELECT RAISE(ABORT, 'claim revisions are insert-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS claim_revisions_no_delete
        BEFORE DELETE ON claim_revisions
        BEGIN SELECT RAISE(ABORT, 'claim revisions are insert-only'); END""",
)


def _connect(session_id: str, *, create: bool) -> Optional[sqlite3.Connection]:
    path = _db_path(session_id)
    if not create and not path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)  # explicit transactions
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in _SCHEMA:
        conn.execute(stmt)
    return conn


def _insert_sealed(conn: sqlite3.Connection, rev: ClaimRevision) -> None:
    """Insert one sealed revision. Refuses (never replaces) an existing key."""
    if not rev.verify():
        raise ClaimRevisionIntegrityError("refusing to store an unsealed or altered revision")
    try:
        row_json = json.dumps(rev.to_dict(), sort_keys=True, allow_nan=False)
    except ValueError as exc:
        raise ClaimRevisionError(
            f"claim {rev.claim_id!r} content holds a non-finite number (NaN/inf) or another value "
            f"that cannot be stored as JSON; fix the claim field and retry: {exc}") from exc
    try:
        conn.execute(
            "INSERT INTO claim_revisions (claim_id, revision, revision_digest, record_digest, row_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (rev.claim_id, rev.revision, rev.revision_digest, rev.record_digest, row_json),
        )
    except sqlite3.IntegrityError as exc:
        raise ClaimRevisionConflict(
            f"claim {rev.claim_id!r} revision {rev.revision} already exists and is sealed") from exc


def _rows(conn: sqlite3.Connection, claim_id: str) -> List[ClaimRevision]:
    out = [ClaimRevision.from_dict(json.loads(r[0])) for r in conn.execute(
        "SELECT row_json FROM claim_revisions WHERE claim_id = ? ORDER BY revision", (claim_id,))]
    if out and not verify_chain(out):
        raise ClaimRevisionIntegrityError(f"claim {claim_id!r} revision chain failed verification")
    return out


def _make(session_id: str, claim_id: str, prev: Optional[ClaimRevision], fields: dict,
          cause: dict, actor: Optional[dict]) -> ClaimRevision:
    return ClaimRevision(
        session_id=session_id, claim_id=claim_id,
        revision=0 if prev is None else prev.revision + 1,
        supersedes=None if prev is None else prev.revision_digest,
        revision_digest=digest(fields), content=fields, cause=cause,
        actor=Actor.from_dict(actor or DEFAULT_ACTOR).to_dict(),
    ).seal()


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def record_change(session_id: str, claim_id: str, after: dict, *, tool: str, reason: str,
                  before: Optional[dict] = None, run_id: Optional[str] = None,
                  actor: Optional[dict] = None, bookkeeping: bool = False,
                  extra_cause: Optional[dict] = None) -> ClaimRevision:
    """Append the revision for one claim change; return the claim's latest revision.

    ``after`` and ``before`` are ``claim_revision_fields`` dicts. ``before`` is
    the claim's pre-change state, used only when the claim has no history yet:
    it becomes revision 0 (``legacy_unrecorded``) and the change is revision 1.
    With no history and no ``before`` the change itself is revision 0.

    A change whose ``revision_digest`` equals the latest one writes nothing
    (no authority-bearing field moved) unless ``bookkeeping`` is true, which is
    for events outside the digest such as promotion.
    """
    cause: Dict[str, Any] = {"tool": tool, "reason": reason, **(extra_cause or {})}
    if run_id:
        cause["run_id"] = run_id
    conn = _connect(session_id, create=True)
    with closing(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            chain = _rows(conn, claim_id)
            prev = chain[-1] if chain else None
            if reason == "legacy_unrecorded":       # baseline only, never a second one
                if prev is not None:
                    conn.execute("COMMIT")
                    return prev
            elif prev is None and before is not None:
                prev = _make(session_id, claim_id, None, before,
                             {"tool": tool, "reason": "legacy_unrecorded"}, actor)
                _insert_sealed(conn, prev)
            if prev is not None and reason != "legacy_unrecorded" and not bookkeeping \
                    and prev.revision_digest == digest(after):
                conn.execute("COMMIT")
                return prev
            rev = _make(session_id, claim_id, prev, after, cause, actor)
            _insert_sealed(conn, rev)
            conn.execute("COMMIT")
            return rev
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def ensure_baseline(session_id: str, claim_id: str, fields: dict, *, tool: str,
                    actor: Optional[dict] = None) -> ClaimRevision:
    """Make sure a claim has history: write ``legacy_unrecorded`` revision 0 if not."""
    return record_change(session_id, claim_id, fields, tool=tool,
                         reason="legacy_unrecorded", actor=actor)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

def history(session_id: str) -> Dict[str, Dict[str, Any]]:
    """``{claim_id: {"ok": True, "rows": [...]} | {"ok": False, "error": str}}``.

    Every chain is verified. One corrupt claim does not hide the others;
    ``latest`` and ``get_revision`` still raise for it (fail closed).
    """
    conn = _connect(session_id, create=False)
    if conn is None:
        return {}
    with closing(conn):
        ids = [r[0] for r in conn.execute("SELECT DISTINCT claim_id FROM claim_revisions ORDER BY claim_id")]
        out: Dict[str, Dict[str, Any]] = {}
        for cid in ids:
            try:
                out[cid] = {"ok": True, "rows": [r.to_dict() for r in _rows(conn, cid)]}
            except Exception as exc:        # corrupt row, bad JSON, broken chain
                out[cid] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return out


def latest(session_id: str, claim_id: str) -> Optional[dict]:
    """Latest verified revision row for the claim, or None when it has no history.

    Raises :class:`ClaimRevisionIntegrityError` if the chain does not verify.
    """
    conn = _connect(session_id, create=False)
    if conn is None:
        return None
    with closing(conn):
        chain = _rows(conn, claim_id)
        return chain[-1].to_dict() if chain else None


def get_revision(session_id: str, claim_id: str, revision: int) -> Optional[dict]:
    """The verified row ``(claim_id, revision)``, or None if the chain has no such revision.

    Verifies the whole chain first and raises if it does not verify.
    """
    conn = _connect(session_id, create=False)
    if conn is None:
        return None
    with closing(conn):
        for row in _rows(conn, claim_id):
            if row.revision == revision:
                return row.to_dict()
        return None


def revision_drift(current_fields: dict, latest_row: Optional[dict]) -> dict:
    """Pure read-time comparison of a claim's current fields with its latest revision.

    ``current_fields`` is ``claim_revision_fields(claim, evidence_versions)``;
    ``latest_row`` is the result of :func:`latest` (or None). Returns
    ``{"state": "no_history" | "in_sync" | "drifted", "drift": bool, ...}``. A
    claim edited outside the tools, or whose retained evidence changed, reads as
    ``drifted`` with the latest revision's number and digest and the current digest.
    """
    if latest_row is None:
        return {"state": "no_history", "drift": False}
    current = digest(current_fields)
    if current == latest_row["revision_digest"]:
        return {"state": "in_sync", "drift": False, "revision": latest_row["revision"],
                "revision_digest": current}
    changed = sorted(k for k in set(current_fields) | set(latest_row["content"])
                     if current_fields.get(k) != latest_row["content"].get(k))
    return {"state": "drifted", "drift": True, "revision": latest_row["revision"],
            "revision_digest": latest_row["revision_digest"], "current_digest": current,
            "changed_fields": changed}
