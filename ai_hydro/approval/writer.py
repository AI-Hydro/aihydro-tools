"""The ONLY writer of the human approval store (ADR-002a).

Imported solely by ``ai_hydro.approval.cli`` (the ``aihydro-approve`` entry
point) and by tests that build fixtures. It must never be imported, directly
or transitively, by the MCP server: ``tests/test_approval_authority.py``
enforces that both statically and by importing ``ai_hydro.mcp`` in a clean
interpreter.

Records are appended under a cross-process lock. Existing lines are never
rewritten or removed. The seal is an unkeyed digest: a same-OS-user process
can import this module or append a sealed line itself, which is why records
carry ``channel: "cli_same_user"`` (see ``records.py``).
"""
from __future__ import annotations

import json
import os

from aihydro_core.records import Actor, utc_now

from ai_hydro.registry.locking import file_lock
from ai_hydro.approval.records import (
    APPROVAL_SCHEMA,
    CHANNEL,
    approvals_dir,
    approvals_file,
    approvals_lock_file,
    seal_record,
    verify_record,
)


def write_approval(
    session_id: str,
    claim_id: str,
    claim_revision_digest: str,
    approver: Actor,
    statement: str,
) -> dict:
    """Append one sealed approval record and return it.

    ``approver`` must be ``Actor(kind="human", ...)``; any other kind raises
    ``ValueError`` so a model or tool actor can never be recorded as approving.
    """
    if not isinstance(approver, Actor) or approver.kind != "human":
        raise ValueError("approval records require Actor(kind='human')")
    record = seal_record({
        "schema": APPROVAL_SCHEMA,
        "claim_id": claim_id,
        "session_id": session_id,
        "claim_revision_digest": claim_revision_digest,
        "approver": approver.to_dict(),
        "channel": CHANNEL,
        "approved_at": utc_now(),
        "statement": statement,
    })
    if not verify_record(record):  # empty field etc.: refuse rather than store junk
        raise ValueError("approval record is incomplete (session_id, claim_id, digest and statement are required)")
    line = json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n"
    approvals_dir().mkdir(parents=True, exist_ok=True)
    with file_lock(approvals_lock_file()):
        # O_APPEND: the file is only ever extended, never opened for rewrite.
        fd = os.open(str(approvals_file()), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    return record
