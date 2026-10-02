"""Read side of the human approval store (ADR-002a).

Everything here is safe to import from MCP tool modules: it can *find and
verify* approvals but contains no code path that creates one. The only
writer is ``ai_hydro.approval.writer``, imported solely by the
``aihydro-approve`` CLI (``ai_hydro.approval.cli``). A test
(``tests/test_approval_authority.py``) fails if anything reachable from the
MCP server imports the writer.

Store layout (``$AIHYDRO_HOME`` defaults to ``~/.aihydro``, resolved at call
time)::

    $AIHYDRO_HOME/approvals/approvals.jsonl       append-only, one sealed record per line
    $AIHYDRO_HOME/approvals/approvals.jsonl.lock  cross-process append lock

Record (schema ``aihydro.approval/1``)::

    {schema, claim_id, session_id, claim_revision_digest,
     approver: Actor(kind="human", id=...), approved_at, statement,
     record_digest}

``record_digest`` is ``aihydro_core.records.digest`` of the record without
that key. It proves the line is intact, not who wrote it: the store is a
filesystem trust boundary (see docs/evidence-integrity.md, "Human approval").

Claim revision digest
---------------------
``claim_revision_digest`` is ``digest`` over exactly these authority-relevant
fields of the session claim (normalised through ``ScientificClaim`` so storage
spelling and defaults do not matter), under the tag ``aihydro.claim_revision/1``:

    text          ScientificClaim.claim
    scope         ScientificClaim.scope  (basins, period, forcing, metric, model_versions)
    status        ScientificClaim.status
    confidence    ScientificClaim.confidence
    evidence_spans  every EvidenceSpan (source_type, source_id, metric_ref, page, passage_hash)
    limitations   ScientificClaim.limitations

Editing any of them changes the digest and so invalidates a prior approval.
Deliberately outside the digest: ``claim_type``, ``confidence_rationale``,
``contradictions``, ``citations``, ``prereg_id``, ``uncertainty_verified``,
timestamps and promotion bookkeeping (``promoted``, ``registry_id``).
"""
from __future__ import annotations

import json
import logging
import shlex
from pathlib import Path
from typing import Any, Iterable, Optional

from aihydro_core.records import digest

from ai_hydro.registry.locking import file_lock
from ai_hydro.registry.paths import aihydro_home

log = logging.getLogger("ai_hydro.approval")

APPROVAL_SCHEMA = "aihydro.approval/1"
REVISION_SCHEMA = "aihydro.claim_revision/1"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
CLI_NAME = "aihydro-approve"
_SEAL_KEY = "record_digest"
_REQUIRED = ("claim_id", "session_id", "claim_revision_digest", "approver", "approved_at", "statement")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def approvals_dir() -> Path:
    """``$AIHYDRO_HOME/approvals``, resolved now (not at import)."""
    return aihydro_home() / "approvals"


def approvals_file() -> Path:
    return approvals_dir() / "approvals.jsonl"


def approvals_lock_file() -> Path:
    return approvals_dir() / "approvals.jsonl.lock"


# ---------------------------------------------------------------------------
# Claim revision digest
# ---------------------------------------------------------------------------

def claim_revision_fields(claim: Any) -> dict:
    """The authority-relevant projection of a claim (dict or ScientificClaim)."""
    from ai_hydro.session.models import ScientificClaim

    model = claim if isinstance(claim, ScientificClaim) else ScientificClaim(**dict(claim))
    return {
        "schema": REVISION_SCHEMA,
        "text": model.claim,
        "scope": model.scope.model_dump(),
        "status": model.status,
        "confidence": model.confidence,
        "evidence_spans": [span.model_dump() for span in model.evidence_spans],
        "limitations": list(model.limitations),
    }


def claim_revision_digest(claim: Any) -> str:
    """``sha256:<hex>`` binding an approval to the claim's current revision."""
    return digest(claim_revision_fields(claim))


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def seal_record(record: dict) -> dict:
    """Return ``record`` with ``record_digest`` computed over the other keys."""
    body = {k: v for k, v in record.items() if k != _SEAL_KEY}
    return {**body, _SEAL_KEY: digest(body)}


def verify_record(record: Any) -> bool:
    """True iff ``record`` is a well-formed, intact, human-approver record."""
    if not isinstance(record, dict) or record.get("schema") != APPROVAL_SCHEMA:
        return False
    if any(not record.get(k) for k in _REQUIRED):
        return False
    approver = record.get("approver")
    if not isinstance(approver, dict) or approver.get("kind") != "human" or not approver.get("id"):
        return False
    seal = record.get(_SEAL_KEY)
    if not isinstance(seal, str):
        return False
    try:
        return digest({k: v for k, v in record.items() if k != _SEAL_KEY}) == seal
    except Exception:  # unencodable value: treat as tampered
        return False


def iter_approvals() -> Iterable[dict]:
    """Yield every intact approval record, oldest first. Invalid lines are skipped."""
    path = approvals_file()
    if not path.exists():
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Skipping malformed approval line: %.80s", line)
                continue
            if verify_record(record):
                yield record
            else:
                log.warning("Skipping approval record that failed verification: %.80s", line)


def find_approval(session_id: str, claim_id: str, claim_revision: str) -> Optional[dict]:
    """Latest intact approval for exactly this claim *revision*, or None."""
    found = None
    for record in iter_approvals():
        if (record["session_id"] == session_id and record["claim_id"] == claim_id
                and record["claim_revision_digest"] == claim_revision):
            found = record
    return found


def get_approval(record_digest: str) -> Optional[dict]:
    """Intact approval record with this ``record_digest``, or None."""
    for record in iter_approvals():
        if record[_SEAL_KEY] == record_digest:
            return record
    return None


def approve_command(session_id: str, claim_id: str) -> str:
    """The exact shell command a human runs to approve this claim."""
    return f"{CLI_NAME} {shlex.quote(session_id)} {shlex.quote(claim_id)}"


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------

class ApprovalRequiredError(Exception):
    """Promotion refused: no human approval record for this claim revision."""

    code = APPROVAL_REQUIRED

    def __init__(self, session_id: str, claim_id: str, claim_revision: str, reason: str):
        self.session_id, self.claim_id, self.claim_revision = session_id, claim_id, claim_revision
        self.command = approve_command(session_id, claim_id)
        super().__init__(reason)

    def to_dict(self) -> dict:
        return {
            "error": True,
            "code": APPROVAL_REQUIRED,
            "message": str(self),
            "claim_id": self.claim_id,
            "claim_revision_digest": self.claim_revision,
            "approval_command": self.command,
            "recovery": (
                "Promotion needs a human approval record bound to this exact claim revision. "
                "Ask the researcher to run, in their own interactive terminal: "
                f"`{self.command}`. Then request promotion again. Editing the claim afterwards "
                "invalidates the approval. Agents cannot create approvals."
            ),
            "next_tools": [],
        }
