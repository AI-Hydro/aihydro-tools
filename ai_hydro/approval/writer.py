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

``write_signed_approval`` (ADR-002b) writes schema ``aihydro.approval/2``: the
sealed body names the signer, and an SSHSIG over ``record_digest`` made by an
enrolled key rides outside the seal. It verifies the result against the trust
root before appending, so an unverifiable record is never stored.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from aihydro_core.records import Actor, utc_now

from ai_hydro.registry.locking import file_lock
from ai_hydro.approval.records import (
    APPROVAL_SCHEMA,
    APPROVAL_SCHEMA_V2,
    CHANNEL,
    approvals_dir,
    approvals_file,
    approvals_lock_file,
    seal_record,
    verify_record,
)
from ai_hydro.approval.signing import SigningError, resolve_key, sign_digest
from ai_hydro.approval.trust import NAMESPACE, SIG_FORMAT, check_approval, trust_root


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
    _append(record)
    return record


def _append(record: dict) -> None:
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


def enrolled_fingerprints() -> Optional[set]:
    """Fingerprints in the active trust root (None when there is none)."""
    from ai_hydro.approval.trust import _allowed_signer_lines, key_fingerprint
    import base64

    path, _ = trust_root()
    if path is None:
        return None
    return {key_fingerprint(base64.b64decode(line["blob_b64"])) for line in _allowed_signer_lines(path)}


def write_signed_approval(
    session_id: str,
    claim_id: str,
    claim_revision_digest: str,
    approver: Actor,
    statement: str,
    key: Optional[str] = None,
) -> dict:
    """Sign and append one ``aihydro.approval/2`` record; return it.

    Raises ``SigningError`` when no key is available or signing fails, and
    ``ValueError`` when the signed record would not verify (key not enrolled,
    outside its validity window, revoked): nothing is written in that case.
    """
    if not isinstance(approver, Actor) or approver.kind != "human":
        raise ValueError("approval records require Actor(kind='human')")
    choice = resolve_key(key, enrolled_fingerprints())
    pub = choice["pub"]
    record = seal_record({
        "schema": APPROVAL_SCHEMA_V2,
        "claim_id": claim_id,
        "session_id": session_id,
        "claim_revision_digest": claim_revision_digest,
        "approver": approver.to_dict(),
        "approved_at": utc_now(),
        "statement": statement,
        "signer": {"fingerprint": pub["fingerprint"], "key_type": pub["key_type"]},
    })
    with tempfile.TemporaryDirectory(prefix="aihydro-sign-") as tmp:
        armored = sign_digest(record["record_digest"], choice, Path(tmp))
    record["signature"] = {"format": SIG_FORMAT, "namespace": NAMESPACE, "armored": armored}
    if not verify_record(record):
        raise ValueError("approval record is incomplete (session_id, claim_id, digest and statement are required)")
    verdict = check_approval(record)
    if not verdict.ok:
        raise ValueError(f"signed approval would not verify, nothing written: {verdict.reason}")
    _append(record)
    return record
