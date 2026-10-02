"""Approval records in a capsule (ADR-002b, packet A3).

``export_session`` copies, for every promoted claim of the session, the human
approval record the promotion consumed and the registry row's stamp into
``approvals/``. A third party can then check the signatures against a key file
THEY supply (``replay.py --allowed-signers FILE``): the capsule never carries a
trust root, and the exporting machine's own trust root is not consulted.

Layout::

    approvals/index.json          {schema, session_id, claims: [entry, ...]}
    approvals/<hex>.json          one approval record, verbatim (signature included)

Entry ``status`` is ``record_carried`` (the approval record is in the capsule; it is not verified until replay), ``no_approval`` (legacy
self-asserted promotion or a promoted claim with no registry row: listed,
never implied approved) or ``approval_record_missing`` (the registry stamp
cites a record this machine could not find; replay reports it as a failure).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

APPROVALS_DIR = "approvals"
INDEX_FILE = f"{APPROVALS_DIR}/index.json"
INDEX_SCHEMA = "aihydro.capsule_approvals/1"


def _records_by_digest() -> dict:
    """Structurally intact approval records on this machine, keyed by ``record_digest``.

    Trust is deliberately not applied: the capsule carries what was consumed
    and the verifier decides under their own signer file.
    """
    from ai_hydro.approval.records import iter_approvals

    return {r["record_digest"]: r for r in iter_approvals()}


def collect_approvals(session: Any, session_id: str, capsule_dir: Path) -> list:
    """Write ``approvals/`` for the session's promoted claims; return the index entries.

    Writes nothing and returns ``[]`` when the session has no promoted claims.
    """
    from ai_hydro.registry.store import find_by_session

    rows = find_by_session(session_id)
    seen_claims = {r.get("claim_id") for r in rows}
    claims = getattr(session, "claims", None) or {}
    entries = []
    if rows:
        by_digest = _records_by_digest()
        out_dir = capsule_dir / APPROVALS_DIR
    for row in rows:
        stamp = row.get("approval") if isinstance(row.get("approval"), dict) else None
        entry = {
            "claim_id": row.get("claim_id"),
            "registry_id": row.get("registry_id"),
            "registry_status": row.get("status"),
            "registry_stamp": {
                "approval": stamp,
                "claim_revision_digest": row.get("claim_revision_digest"),
                "claim_revision": row.get("claim_revision"),
            },
        }
        digest = (stamp or {}).get("record_digest")
        if not digest:
            entry.update(status="no_approval", reason="legacy registry row without an approval stamp (self_asserted)")
        else:
            record = by_digest.get(digest)
            if record is None:
                entry.update(status="approval_record_missing", record_digest=digest,
                             reason="the registry stamp cites an approval record not found at export")
            else:
                out_dir.mkdir(parents=True, exist_ok=True)
                rel = f"{APPROVALS_DIR}/{digest.split(':')[-1]}.json"
                (capsule_dir / rel).write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
                entry.update(status="record_carried", record_digest=digest, approval_file=rel)
        entries.append(entry)
    for claim_id, claim in sorted(claims.items()):
        if isinstance(claim, dict) and claim.get("promoted") and claim_id not in seen_claims:
            entries.append({"claim_id": claim_id, "registry_id": claim.get("registry_id"),
                            "status": "no_approval",
                            "reason": "claim is marked promoted but has no registry row for this session"})
    if entries:
        (capsule_dir / INDEX_FILE).parent.mkdir(parents=True, exist_ok=True)
        (capsule_dir / INDEX_FILE).write_text(
            json.dumps({"schema": INDEX_SCHEMA, "session_id": session_id, "claims": entries},
                       indent=2, sort_keys=True), encoding="utf-8")
    return entries


def manifest_section(capsule_dir: Path, entries: list) -> dict:
    """Manifest ``approvals`` block: counts plus sha256 of the index and every record."""
    files = []
    if entries:
        paths = [INDEX_FILE] + sorted({e["approval_file"] for e in entries if e.get("approval_file")})
        files = [{"path": p, "sha256": hashlib.sha256((capsule_dir / p).read_bytes()).hexdigest()}
                 for p in paths]
    return {
        "schema": INDEX_SCHEMA,
        "n_promoted_claims": len(entries),
        "n_with_approval_record": sum(1 for e in entries if e["status"] == "record_carried"),
        "n_no_approval": sum(1 for e in entries if e["status"] == "no_approval"),
        "n_record_missing": sum(1 for e in entries if e["status"] == "approval_record_missing"),
        "files": files,
        "verification": "replay.py --allowed-signers FILE (a key file the verifier supplies)",
    }
