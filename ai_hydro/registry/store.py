"""
Global claim registry — append-only JSONL store.

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
a consistent snapshot.  Concurrent writers are serialised by the OS rename
atomicity guarantee (POSIX) — the same pattern used by HydroSession.save().
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


def build_registry_id(session_id: str, claim_id: str) -> str:
    """Build a deterministic registry_id from session + claim identifiers."""
    import hashlib
    session_frag = session_id[:8] if session_id else "unknown"
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    raw = f"{session_id}:{claim_id}"
    hash6 = hashlib.sha256(raw.encode()).hexdigest()[:6]
    return f"reg.{session_frag}.{date_str}.{hash6}"


def snapshot_evidence_versions(session: Any, evidence_spans: list[dict]) -> dict[str, str]:
    """
    Compute a content_hash for each dataset-type evidence span.

    For each span with source_type == "dataset":
      1. Check session.artifact_manifest for a matching source entry.
      2. Fall back to hashing the session slot data for the source_id.
      3. If neither found, record an empty string (evidence not snapshotted).

    For run-type spans: the run_id IS the immutable version anchor — stored
    as-is so staleness checks can detect run replacement.
    """
    from ai_hydro.session.store import _hash_obj

    versions: dict[str, str] = {}
    for span in evidence_spans:
        src_type = span.get("source_type", span.get("type", ""))
        src_id = span.get("source_id", span.get("id", ""))
        if not src_id:
            continue

        if src_type == "dataset":
            # Check artifact_manifest first
            manifest: dict = getattr(session, "artifact_manifest", {}) or {}
            match = next(
                (v for v in manifest.values() if v.get("source") == src_id or src_id in str(v.get("source", ""))),
                None,
            )
            if match and match.get("content_hash"):
                versions[src_id] = match["content_hash"]
            else:
                # Hash slot data as fallback
                slot_names = [src_id, src_id.split("_")[-1], src_id.replace("_", "").lower()]
                for sname in slot_names:
                    slot_data = session.get(sname)
                    if slot_data is not None:
                        versions[src_id] = _hash_obj(slot_data)
                        break
                else:
                    versions[src_id] = ""  # not found — can't snapshot

        elif src_type == "run":
            # run_id is the immutable version anchor
            versions[src_id] = src_id  # self-referential: run IS its own hash

    return versions


def check_evidence_staleness(
    session: Any,
    evidence_versions: dict[str, str],
    evidence_spans: list[dict],
) -> list[str]:
    """
    Return source_ids whose current data differs from the stored version hash.

    Recomputes hashes for dataset spans using the same logic as
    snapshot_evidence_versions.  Returns a list of stale source_ids.
    """
    current = snapshot_evidence_versions(session, evidence_spans)
    stale: list[str] = []
    for src_id, stored_hash in evidence_versions.items():
        if not stored_hash:
            continue  # was not snapshotted — skip
        cur_hash = current.get(src_id, "")
        if cur_hash and cur_hash != stored_hash:
            stale.append(src_id)
    return stale
