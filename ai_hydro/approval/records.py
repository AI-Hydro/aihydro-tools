"""Read side of the human approval store (ADR-002a).

Everything here is safe to import from MCP tool modules: it can *find and
verify* approvals but contains no code path that creates one. The only
writer is ``ai_hydro.approval.writer``, imported solely by the
``aihydro-approve`` CLI (``ai_hydro.approval.cli``). A test
(``tests/test_approval_authority.py``) fails if anything reachable from the
MCP server imports the writer.

What this does and does not guarantee (channel ``cli_same_user``)
------------------------------------------------------------------
It blocks *unintended or naive* self-approval: a promotion cannot be conferred
through a tool argument or any MCP tool, and an agent that merely shells out to
``aihydro-approve`` without a terminal is refused. It does NOT stop a process
running as the same OS user: such a process can append a correctly sealed
record itself (the seal is an unkeyed digest, so it proves integrity, not
origin) or drive the CLI through a pseudo-terminal. Both were demonstrated by
the slice-1 review. Records therefore carry ``channel: "cli_same_user"`` and
registry stamps repeat it; it names how the record was produced, not that a
human was verified. Closing the gap is ADR-002b: schema ``aihydro.approval/2`` carries an SSHSIG
over ``record_digest`` made by an enrolled key; the verifier (``trust.py``)
derives the channel (``ssh_sig[_sk]_{system,user}_trust`` / ``cli_same_user``) and
refuses a v2 line without a valid, enrolled, unrevoked signature. v1 records
are accepted as ``cli_same_user`` only under an explicit opt-out (fail closed by default).

Store layout (``$AIHYDRO_HOME`` defaults to ``~/.aihydro``, resolved at call
time)::

    $AIHYDRO_HOME/approvals/approvals.jsonl       append-only, one sealed record per line
    $AIHYDRO_HOME/approvals/approvals.jsonl.lock  cross-process append lock

Record (schema ``aihydro.approval/1``)::

    {schema, claim_id, session_id, claim_revision_digest,
     approver: Actor(kind="human", id=...), channel, approved_at, statement,
     record_digest}

``record_digest`` is ``aihydro_core.records.digest`` of the record without
that key. Schema ``aihydro.approval/2`` drops ``channel``, adds
``signer {fingerprint, key_type}`` (sealed) and ``signature {format, namespace,
armored}`` (outside the seal: it signs ``record_digest``).

Single use
----------
An approval authorises exactly one promotion. Promotion stamps the registry row
with ``approval.record_digest``; the registry refuses a second row citing the
same record and promotion refuses an already-consumed approval, so another
promotion (for example after retained evidence changed) needs a fresh approval.

Claim revision digest
---------------------
``claim_revision_digest`` is ``digest`` over the tagged object
``aihydro.claim_revision/2``. The rule: every authority-bearing field that is
copied into the registry row, plus the retained evidence it rests on, is bound.
Fields (claim normalised through ``ScientificClaim`` so storage spelling and
defaults do not matter):

    text, claim_type, status, confidence, confidence_rationale
    scope           basins, period, forcing, metric, model_versions
    evidence_spans  every EvidenceSpan
    evidence_versions  source_id -> ``sha256-v3`` (v2 for older bindings) fingerprint of the retained
                    record each span resolves to (registry/evidence.py), as
                    computed at promotion time; ``unresolved:<code>`` if a span
                    cannot be resolved
    limitations, prereg_id, uncertainty_verified

Editing any claim field, or mutating a retained run/dataset/passage the claim
cites, changes the digest and so invalidates a prior approval. Outside the
digest: ``contradictions``, ``citations``, timestamps and promotion bookkeeping
(``promoted``, ``registry_id``) - none is copied into the registry row.
"""
from __future__ import annotations

import json
import logging
import shlex
from pathlib import Path
from typing import Any, Iterable, Optional

from aihydro_core.records import digest

from ai_hydro.approval.trust import check_approval
from ai_hydro.registry.locking import file_lock
from ai_hydro.registry.paths import aihydro_home

log = logging.getLogger("ai_hydro.approval")

APPROVAL_SCHEMA_V1 = "aihydro.approval/1"
APPROVAL_SCHEMA_V2 = "aihydro.approval/2"
APPROVAL_SCHEMA = APPROVAL_SCHEMA_V1      # schema the legacy (unsigned) writer emits
_SCHEMAS = (APPROVAL_SCHEMA_V1, APPROVAL_SCHEMA_V2)
REVISION_SCHEMA = "aihydro.claim_revision/2"
CHANNEL = "cli_same_user"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
CLI_NAME = "aihydro-approve"
_SEAL_KEY = "record_digest"
_SIG_KEY = "signature"      # v2 only: outside the seal, because it signs the seal
_REQUIRED = ("claim_id", "session_id", "claim_revision_digest", "approver", "channel",
             "approved_at", "statement")
_REQUIRED_V2 = ("claim_id", "session_id", "claim_revision_digest", "approver",
                "approved_at", "statement", "signer", "signature")


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

def evidence_binding(session: Any, spans: Iterable[Any], like: dict | None = None,
                     like_seals: dict | None = None) -> tuple:
    """``(evidence_versions, evidence_seals)`` for the spans' retained sources.

    ``evidence_seals`` maps a run source to the ``record_digest`` its row carried
    when the claim was bound (``like_seals`` keeps an earlier binding; a source
    bound while unsealed stays unbound, so a later seal is revision-neutral).
    A bound seal that was removed or swapped makes the source unresolved
    (``EVIDENCE_SEAL_INVALID``, ``seal_removed_or_replaced``).
    """
    from ai_hydro.registry.evidence import EvidenceError, bind_seal, evidence_fingerprint, resolve_source

    versions, seals = {}, {}
    for span in spans:
        span = span if isinstance(span, dict) else span.model_dump()
        sid = span["source_id"]
        try:
            row = resolve_source(session, span, (like_seals or {}).get(sid))
            versions[sid] = evidence_fingerprint(row, span.get("source_type"), like=(like or {}).get(sid))
            bound = bind_seal(row, span.get("source_type"), sid, like, like_seals)
            if bound:
                seals[sid] = bound
        except EvidenceError as exc:
            versions[sid] = f"unresolved:{exc.code}"
    return versions, seals


def evidence_fingerprints(session: Any, spans: Iterable[Any], like: dict | None = None,
                          like_seals: dict | None = None) -> dict:
    """``source_id -> sha256-v3`` of the retained record each span resolves to
    (v2 for a source whose entry in ``like`` is v2: a stored fingerprint is
    recomputed in its own version, so old bindings do not drift).

    Same fingerprints promotion computes through ``verified_versions``
    (``resolve_source`` then ``fingerprint``), without validating the metric.
    A span that cannot be resolved binds as ``unresolved:<code>``; promotion
    refuses such a claim on evidence grounds before it ever checks approval.
    """
    return evidence_binding(session, spans, like, like_seals)[0]


def claim_revision_fields(claim: Any, evidence_versions: dict, evidence_seals: dict | None = None) -> dict:
    """The authority-bearing projection of a claim plus its bound evidence.

    ``claim`` is the stored session claim dict (``prereg_id`` and
    ``uncertainty_verified`` live only there, not on ``ScientificClaim``).
    ``evidence_versions`` is required so a caller cannot forget to bind it.
    """
    from ai_hydro.session.models import ScientificClaim

    raw = dict(claim) if isinstance(claim, dict) else claim.model_dump()
    model = ScientificClaim(**raw)
    fields = {
        "schema": REVISION_SCHEMA,
        "text": model.claim,
        "claim_type": model.claim_type,
        "status": model.status,
        "confidence": model.confidence,
        "confidence_rationale": model.confidence_rationale,
        "scope": model.scope.model_dump(),
        "evidence_spans": [span.model_dump() for span in model.evidence_spans],
        "evidence_versions": dict(evidence_versions),
        "limitations": list(model.limitations),
        "prereg_id": raw.get("prereg_id"),
        "uncertainty_verified": bool(raw.get("uncertainty_verified")),
    }
    if evidence_seals:       # omitted when empty, so revisions made before it keep their digest
        fields["evidence_seals"] = dict(evidence_seals)
    return fields


def claim_revision_digest(claim: Any, evidence_versions: dict, evidence_seals: dict | None = None) -> str:
    """``sha256:<hex>`` binding an approval to the claim's current revision."""
    return digest(claim_revision_fields(claim, evidence_versions, evidence_seals))


def latest_bound_state(session_id: Any, claim_id: str) -> tuple:
    """``(evidence_versions, evidence_seals)`` of the claim's latest stored
    revision, or ``({}, {})``. Never raises."""
    try:
        from ai_hydro.session import claim_revisions

        last = claim_revisions.latest(session_id, claim_id) if session_id else None
        content = (last or {}).get("content", {})
        return dict(content.get("evidence_versions") or {}), dict(content.get("evidence_seals") or {})
    except Exception:
        return {}, {}


def latest_bound_versions(session_id: Any, claim_id: str) -> dict:
    """``evidence_versions`` of the claim's latest stored revision, or ``{}``.

    Used only to pick each fingerprint's version; a missing or unreadable chain
    means a new binding (current version). Never raises.
    """
    try:
        from ai_hydro.session import claim_revisions

        last = claim_revisions.latest(session_id, claim_id) if session_id else None
        return dict((last or {}).get("content", {}).get("evidence_versions") or {})
    except Exception:
        return {}


def session_claim_revision(session: Any, claim_id: str) -> tuple:
    """``(claim, evidence_versions, fields, digest)`` for a claim in a loaded session."""
    claim = session.claims[claim_id]
    from ai_hydro.session.models import ScientificClaim
    spans = ScientificClaim(**dict(claim)).evidence_spans
    bound_v, bound_s = latest_bound_state(getattr(session, "session_id", None), claim_id)
    ev, seals = evidence_binding(session, spans, bound_v, bound_s)
    fields = claim_revision_fields(claim, ev, seals)
    return claim, ev, fields, digest(fields)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def _body(record: dict) -> dict:
    """The sealed part: everything except the seal and (v2) the signature over it."""
    skip = {_SEAL_KEY, _SIG_KEY} if record.get("schema") == APPROVAL_SCHEMA_V2 else {_SEAL_KEY}
    return {k: v for k, v in record.items() if k not in skip}


def seal_record(record: dict) -> dict:
    """Return ``record`` with ``record_digest`` computed over the sealed body."""
    body = _body(record)
    sig = {_SIG_KEY: record[_SIG_KEY]} if _SIG_KEY in record and record.get("schema") == APPROVAL_SCHEMA_V2 else {}
    return {**body, _SEAL_KEY: digest(body), **sig}


def seal_record_generic(record: dict) -> dict:
    """Seal any flat record (used for revocation lines): digest over all other keys."""
    body = {k: v for k, v in record.items() if k != _SEAL_KEY}
    return {**body, _SEAL_KEY: digest(body)}


def verify_record(record: Any) -> bool:
    """True iff ``record`` is a well-formed, intact, human-approver record.

    Structure and seal only. Whether it is *accepted* (signature, trust root,
    revocation, require_signed) is ``check_approval``; ``find_approval`` applies both.
    """
    if not isinstance(record, dict) or record.get("schema") not in _SCHEMAS:
        return False
    v2 = record["schema"] == APPROVAL_SCHEMA_V2
    if any(not record.get(k) for k in (_REQUIRED_V2 if v2 else _REQUIRED)):
        return False
    if v2 and not (isinstance(record["signer"], dict) and isinstance(record["signature"], dict)):
        return False
    approver = record.get("approver")
    if not isinstance(approver, dict) or approver.get("kind") != "human" or not approver.get("id"):
        return False
    seal = record.get(_SEAL_KEY)
    if not isinstance(seal, str):
        return False
    try:
        return digest(_body(record)) == seal
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


def approval_consumers() -> dict:
    """``record_digest -> registry_id`` for every registry row that cites an approval."""
    from ai_hydro.registry.store import all_entries

    out = {}
    for entry in all_entries():
        approval = entry.get("approval")
        if isinstance(approval, dict) and approval.get("record_digest"):
            out.setdefault(approval["record_digest"], entry.get("registry_id"))
    return out


def find_approval(session_id: str, claim_id: str, claim_revision: str,
                  *, unconsumed_only: bool = False) -> Optional[dict]:
    """Latest intact approval for exactly this claim *revision*, or None.

    With ``unconsumed_only`` an approval already cited by a registry row is
    skipped (an approval authorises one promotion).
    """
    consumed = approval_consumers() if unconsumed_only else {}
    found = None
    for record in iter_approvals():
        if (record["session_id"] == session_id and record["claim_id"] == claim_id
                and record["claim_revision_digest"] == claim_revision
                and record[_SEAL_KEY] not in consumed and check_approval(record).ok):
            found = record
    return found


def get_approval(record_digest: str) -> Optional[dict]:
    """Intact approval record with this ``record_digest``, or None."""
    for record in iter_approvals():
        if record[_SEAL_KEY] == record_digest and check_approval(record).ok:
            return record
    return None


def verified_channel(record: dict) -> Optional[str]:
    """The channel the *verifier* derives for ``record`` (``ssh_sig[_sk]_system_trust`` /
    ``_user_trust`` / ``_supplied``, or ``cli_same_user``), or None when not accepted.
    Never read from the record; ``*_user_trust`` is integrity-only, not human verification."""
    verdict = check_approval(record)
    return verdict.channel if verdict.ok else None


def approval_stamp(record: dict) -> dict:
    """What a registry row should cite.

    ``{record_digest, channel, trust_root, principal, signer {fingerprint,
    key_type}, policy}``. ``channel`` is verifier-derived and names the trust
    root (``ssh_sig_sk_system_trust`` ... ``cli_same_user``); ``signer``,
    ``trust_root`` and ``principal`` are None for legacy v1 records; ``policy``
    is ``unsigned_opt_out`` when verified under an explicit opt-out. All
    None (except the digest) when the record is not accepted.
    """
    v = check_approval(record)
    return {"record_digest": record[_SEAL_KEY],
            "channel": v.channel if v.ok else None,
            "trust_root": v.trust_root if v.ok else None,
            "principal": v.principal if v.ok else None,
            "signer": v.signer if v.ok else None,
            "policy": v.policy if v.ok else None}


def approve_command(session_id: str, claim_id: str) -> str:
    """The exact shell command a human runs to approve this claim."""
    return f"{CLI_NAME} {shlex.quote(session_id)} {shlex.quote(claim_id)}"


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------

class ApprovalRequiredError(Exception):
    """Promotion refused: no human approval record for this claim revision."""

    code = APPROVAL_REQUIRED

    def __init__(self, session_id: str, claim_id: str, claim_revision: Optional[str], reason: str):
        self.session_id, self.claim_id, self.claim_revision = session_id, claim_id, claim_revision
        self.command = approve_command(session_id, claim_id)
        super().__init__(reason)

    def to_dict(self) -> dict:
        return {
            "error": True,
            "code": APPROVAL_REQUIRED,
            "message": str(self),
            "session_id": self.session_id,
            "claim_id": self.claim_id,
            "approval_command": self.command,
            "recovery": (
                "Promotion needs a human approval record bound to this exact claim revision. "
                "Ask the researcher to run, in their own interactive terminal: "
                f"`{self.command}`. Then request promotion again. Editing the claim or its retained "
                "evidence afterwards invalidates the approval, and an approval authorises "
                "one promotion only. Tools cannot create approvals."
            ),
            "next_tools": [],
        }
