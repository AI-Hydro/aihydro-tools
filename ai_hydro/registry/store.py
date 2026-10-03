"""
Global claim registry — JSONL store with status updates.

File layout (``$AIHYDRO_HOME`` defaults to ``~/.aihydro``; resolved at call time):
    $AIHYDRO_HOME/registry/claims.jsonl      ← one JSON object per line
    $AIHYDRO_HOME/registry/claims.jsonl.tmp  ← atomic write temp (auto-removed)
    $AIHYDRO_HOME/registry/claims.jsonl.lock ← cross-process write lock

Each entry is a dict with at minimum:
    registry_id     — "reg.{session_frag}.{date}.{hash6}"
    claim_id        — original session claim_id
    session_id      — source session
    statement       — claim text
    status          — promoted | stale | retracted
    promoted_at     — ISO-8601 UTC
    evidence_versions  — {source_id: content_hash_hex} captured at promotion time
    staleness       — None or {reason, detected_at, stale_sources: [source_id, ...]}
    approval        — {record_digest, channel}: the human approval record
                      (ADR-002a) the promotion was bound to. Single use: a
                      second row citing the same record_digest is refused. Rows written before approval
                      records existed have no ``approval`` key; listings label
                      them ``approval: self_asserted`` at read time (rows are
                      never rewritten to add the label).

The file is rewritten atomically via write-then-rename so readers always see
a consistent snapshot.  Every read-modify-write runs under an exclusive
cross-process lock (``fcntl.flock`` on POSIX; see ``locking.py`` for the
Windows and no-lock fallbacks), so concurrent writers serialise instead of
losing updates.  This store is still not append-only: stale/retracted marks
rewrite the file.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .locking import file_lock
from .paths import aihydro_home

log = logging.getLogger("ai_hydro.registry")

# Explicit path overrides, ``None`` by default. Production resolves the paths
# at call time from ``$AIHYDRO_HOME`` (see ``registry_dir`` / ``claims_file``);
# a test may still pin either attribute to a temp location.
REGISTRY_DIR: Path | None = None
CLAIMS_FILE: Path | None = None


def registry_dir() -> Path:
    """Registry directory: explicit override, else ``$AIHYDRO_HOME/registry``."""
    return REGISTRY_DIR if REGISTRY_DIR is not None else aihydro_home() / "registry"


def claims_file() -> Path:
    """Registry claims file: explicit override, else ``<registry_dir>/claims.jsonl``."""
    return CLAIMS_FILE if CLAIMS_FILE is not None else registry_dir() / "claims.jsonl"


class ApprovalAlreadyConsumed(ValueError):
    """A different registry row already cites this approval record."""

    def __init__(self, record_digest: str, registry_id: str):
        self.record_digest, self.registry_id = record_digest, registry_id
        super().__init__(f"Approval {record_digest} already authorised promotion {registry_id}; "
                         "an approval is single-use, so a fresh approval is required.")


def _lock():
    """Exclusive cross-process lock held around every read-modify-write."""
    path = claims_file()
    return file_lock(path.with_name(path.name + ".lock"))


# ---------------------------------------------------------------------------
# Low-level IO
# ---------------------------------------------------------------------------

def _ensure_dir() -> None:
    claims_file().parent.mkdir(parents=True, exist_ok=True)


def _read_all() -> list[dict]:
    """Return every entry from the registry, oldest first."""
    path = claims_file()
    if not path.exists():
        return []
    entries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("Skipping malformed registry line: %.80s", line)
    return entries


def _write_all(entries: list[dict]) -> None:
    """Atomically rewrite the full registry file."""
    _ensure_dir()
    path = claims_file()
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    os.replace(tmp, path)  # atomic on POSIX and Windows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def append(entry: dict) -> None:
    """
    Append a new entry to the registry.

    Under the cross-process lock: reads current state, deduplicates by
    registry_id (returns early if the exact registry_id already exists), then
    rewrites atomically.
    """
    _ensure_dir()
    with _lock():
        entries = _read_all()
        if any(e.get("registry_id") == entry.get("registry_id") for e in entries):
            return  # idempotent: same registry_id already present
        # Single-use approvals, enforced under the same lock as the write so
        # two concurrent promotions cannot both consume one record.
        approval = entry.get("approval")
        if isinstance(approval, dict) and approval.get("record_digest"):
            for existing in entries:
                other = existing.get("approval")
                if isinstance(other, dict) and other.get("record_digest") == approval["record_digest"]:
                    raise ApprovalAlreadyConsumed(approval["record_digest"], existing.get("registry_id", "?"))
        entries.append(entry)
        _write_all(entries)


def all_entries() -> list[dict]:
    """Return all registry entries, oldest first."""
    return _read_all()


SELF_ASSERTED = "self_asserted"


def approval_label(entry: dict) -> Any:
    """Approval stamp of an entry, or ``"self_asserted"`` for legacy rows.

    Read-time only: stored rows are never rewritten to add the label.
    """
    approval = entry.get("approval")
    return approval if isinstance(approval, dict) and approval.get("record_digest") else SELF_ASSERTED


def with_approval_label(entry: dict) -> dict:
    """Shallow copy of ``entry`` whose ``approval`` field is always present."""
    out = dict(entry)
    out["approval"] = approval_label(entry)
    return out


def find_by_claim_id(claim_id: str) -> list[dict]:
    """Return all entries for a given claim_id (may have multiple on re-promotion)."""
    return [e for e in _read_all() if e.get("claim_id") == claim_id]


def find_by_session(session_id: str) -> list[dict]:
    """Return all entries promoted from a given session."""
    return [e for e in _read_all() if e.get("session_id") == session_id]


def mark_stale(registry_id: str, stale_sources: list[str], reason: str = "evidence_changed") -> bool:
    """
    Mark an existing registry entry as stale.

    Updates the entry in-place (rewrites the file) and sets:
        status       → "stale"
        staleness    → {reason, detected_at, stale_sources}

    Returns True if the entry was found and updated, False otherwise.
    """
    _ensure_dir()
    with _lock():
        entries = _read_all()
        updated = False
        for entry in entries:
            if entry.get("registry_id") == registry_id:
                entry["status"] = "stale"
                entry["staleness"] = {
                    "reason": reason,
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                    "stale_sources": stale_sources,
                }
                updated = True
        if updated:
            _write_all(entries)
    return updated


def mark_retracted(registry_id: str, reason: str = "") -> bool:
    """Mark an entry as retracted (researcher withdrew the claim)."""
    _ensure_dir()
    with _lock():
        entries = _read_all()
        updated = False
        for entry in entries:
            if entry.get("registry_id") == registry_id:
                entry["status"] = "retracted"
                entry["retracted_at"] = datetime.now(timezone.utc).isoformat()
                if reason:
                    entry["retraction_reason"] = reason
                updated = True
        if updated:
            _write_all(entries)
    return updated


def build_registry_id(session_id: str, claim_id: str, revision: str = "") -> str:
    """Build an ID; content revisions use a longer digest than legacy IDs."""
    import hashlib
    session_frag = session_id[:8] if session_id else "unknown"
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    raw = f"{session_id}:{claim_id}" + (f":{revision}" if revision else "")
    digest = hashlib.sha256(raw.encode()).hexdigest()[:24 if revision else 6]
    return f"reg.{session_frag}.{date_str}.{digest}"


def snapshot_evidence_versions(session: Any, evidence_spans: list[dict],
                               like: dict | None = None) -> dict[str, str]:
    """Fingerprint exact retained sources; raise on unresolved evidence.

    The v2 prefix distinguishes real content fingerprints from legacy run-ID
    self-hashes and dataset hashes that could have come from metadata alone.
    """
    from .evidence import resolve_source, evidence_fingerprint
    return {span["source_id"]: evidence_fingerprint(
                resolve_source(session, span), span.get("source_type"),
                like=(like or {}).get(span["source_id"]))
            for span in evidence_spans}


def check_evidence_staleness(
    session: Any,
    evidence_versions: dict[str, str],
    evidence_spans: list[dict],
) -> list[str]:
    """Changed, deleted, unresolvable and legacy evidence all need review.

    Never upgrade a legacy snapshot by hashing the current result: that would
    invent evidence of what was present at the time of original promotion.
    """
    from .evidence import EvidenceError, resolve_source, evidence_fingerprint, fingerprint_version
    stale: list[str] = []
    for span in evidence_spans:
        sid = span.get("source_id", "")
        stored = evidence_versions.get(sid, "")
        try:
            # recomputed in the stored fingerprint's own version
            current = evidence_fingerprint(resolve_source(session, span), span.get("source_type"), like=stored)
        except (EvidenceError, TypeError, ValueError, OSError):
            current = None
        if fingerprint_version(stored) is None or current != stored:
            if sid not in stale:
                stale.append(sid)
    for sid in evidence_versions:
        if not any(span.get("source_id") == sid for span in evidence_spans) and sid not in stale:
            stale.append(sid)
    if not evidence_spans and not stale:
        stale.append("<missing-evidence-spans>")
    return stale
