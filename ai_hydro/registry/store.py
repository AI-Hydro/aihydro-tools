"""
Global claim registry — JSONL store with status updates.

File layout:
    ~/.aihydro/registry/claims.jsonl     ← one JSON object per line
    ~/.aihydro/registry/claims.jsonl.tmp ← atomic write temp (auto-removed)

Each entry is a dict with at minimum:
    registry_id     — "reg.{session_frag}.{date}.{hash6}"
    claim_id        — original session claim_id
    session_id      — source session
    statement       — claim text
    status          — promoted | stale | retracted
    promoted_at     — ISO-8601 UTC
    evidence_versions  — {source_id: content_hash_hex} captured at promotion time
    staleness       — None or {reason, detected_at, stale_sources: [source_id, ...]}

The file is rewritten atomically via write-then-rename so readers always see
a consistent snapshot.  Rename does not serialize concurrent read/modify/write operations. Multi-writer
transaction safety is separate outstanding work; this store is not append-only.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ai_hydro.registry")

REGISTRY_DIR = Path.home() / ".aihydro" / "registry"
CLAIMS_FILE = REGISTRY_DIR / "claims.jsonl"


# ---------------------------------------------------------------------------
# Low-level IO
# ---------------------------------------------------------------------------

def _ensure_dir() -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)


def _read_all() -> list[dict]:
    """Return every entry from the registry, oldest first."""
    if not CLAIMS_FILE.exists():
        return []
    entries = []
    with open(CLAIMS_FILE, encoding="utf-8") as fh:
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
    tmp = CLAIMS_FILE.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    os.replace(tmp, CLAIMS_FILE)  # atomic on POSIX and Windows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def append(entry: dict) -> None:
    """
    Append a new entry to the registry.

    Reads current state, deduplicates by registry_id (returns early if the
    exact registry_id already exists), then rewrites atomically.
    """
    _ensure_dir()
    entries = _read_all()
    if any(e.get("registry_id") == entry.get("registry_id") for e in entries):
        return  # idempotent: same registry_id already present
    entries.append(entry)
    _write_all(entries)


def all_entries() -> list[dict]:
    """Return all registry entries, oldest first."""
    return _read_all()


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


def snapshot_evidence_versions(session: Any, evidence_spans: list[dict]) -> dict[str, str]:
    """Fingerprint exact retained sources; raise on unresolved evidence.

    The v2 prefix distinguishes real content fingerprints from legacy run-ID
    self-hashes and dataset hashes that could have come from metadata alone.
    """
    from .evidence import resolve_source, fingerprint
    return {span["source_id"]: fingerprint(resolve_source(session, span))
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
    from .evidence import EvidenceError, resolve_source, fingerprint
    stale: list[str] = []
    for span in evidence_spans:
        sid = span.get("source_id", "")
        stored = evidence_versions.get(sid, "")
        try:
            current = fingerprint(resolve_source(session, span))
        except (EvidenceError, TypeError, ValueError, OSError):
            current = None
        if not str(stored).startswith("sha256-v2:") or current != stored:
            if sid not in stale:
                stale.append(sid)
    for sid in evidence_versions:
        if not any(span.get("source_id") == sid for span in evidence_spans) and sid not in stale:
            stale.append(sid)
    if not evidence_spans and not stale:
        stale.append("<missing-evidence-spans>")
    return stale
