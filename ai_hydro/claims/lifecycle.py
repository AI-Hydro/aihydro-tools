"""Claim lifecycle across the session and the global registry (R-03, R-07; mechanism M2).

A session claim and its registry row are one object seen in two places. The registry is the
external anchor other studies rely on, so the session may never assert more than the row does, and
a withdrawal must reach it. Every path that sets a claim's status (``add_claim``,
``update_claim_status``, ``check_registry_staleness``) goes through the functions here:

* **Retraction propagates** (``propagate_retraction``). A session claim set to ``retracted`` retracts its
  registry row(s). Narrowing what the registry asserts is never a privilege: it needs no approval.
* **Retracted stays retracted** (``reinstatement_gate``). A claim whose registry row was retracted cannot
  be set back to ``supported``/``weakly_supported``; state the finding as a new claim.
* **Stale is not reinstated by a sentence** (``reinstatement_gate``). A claim whose evidence changed
  (session status ``stale``, or a ``stale`` registry row) can take a supporting status again only when
  every evidence span it now cites is NOT one of the sources that changed, i.e. the evidence was re-bound.
  Even then the registry row stays ``stale`` until a human re-approves: only ``aihydro-approve`` plus
  ``promote_claim_to_registry`` can make it ``promoted`` again. Nothing here promotes.
"""
from __future__ import annotations

import logging
from typing import Any

from ai_hydro.claims.status_gate import EVIDENCE_STATUSES, _refusal

log = logging.getLogger("ai_hydro.claims.lifecycle")

#: Claim-dict keys that link a study claim to its registry row and staleness record. They sit outside the
#: revision digest and the approval record, so keeping them across a redefinition grants nothing.
LINK_FIELDS = ("promoted", "promoted_at", "registry_id", "staleness_detected_at", "stale_sources")

#: Registry row states that bar a supporting status on the session claim.
_BARRING = ("stale", "retracted")


def registry_rows(session_id: str, claim_id: str, claim_dict: dict | None = None) -> list[dict]:
    """Registry rows of this session's claim: its linked ``registry_id`` plus any other row for the pair.

    Raises when the registry cannot be read: "no rows" and "unreadable" must never look the same.
    """
    from ai_hydro.registry.store import all_entries, find_by_session
    rows = [r for r in find_by_session(session_id) if r.get("claim_id") == claim_id]
    rid = (claim_dict or {}).get("registry_id")
    if rid and not any(r.get("registry_id") == rid for r in rows):
        rows += [r for r in all_entries() if r.get("registry_id") == rid]
    return rows


def _stale_sources(previous: dict, rows: list[dict]) -> set[str]:
    out = {str(s) for s in previous.get("stale_sources") or []}
    for r in rows:
        if r.get("status") == "stale":
            out |= {str(s) for s in (r.get("staleness") or {}).get("stale_sources") or []}
    return out


def reinstatement_gate(session_id: str, claim_id: str, previous: dict | None, candidate: dict,
                       status: str) -> dict | None:
    """Refusal when ``status`` would reinstate a claim the registry or the staleness check withdrew."""
    if status not in EVIDENCE_STATUSES or not previous:
        return None
    try:
        rows = registry_rows(session_id, claim_id, previous)
    except Exception as exc:       # fail closed: an unreadable registry cannot clear a reinstatement
        return _refusal(
            "REGISTRY_UNREADABLE", claim_id, candidate, status, "registry_must_be_readable_to_assert",
            f"The global registry could not be read ({type(exc).__name__}), so it cannot be checked whether "
            f"claim '{claim_id}' was retracted or went stale. Nothing was changed.",
            "Fix or restore the registry file, then repeat the call. Recording the claim as 'tested' or "
            "'inconclusive' does not need the registry.")
    if any(r.get("status") == "promoted" for r in rows):
        return None            # re-promoted under a fresh human approval: the registry row is live again
    retracted = [r for r in rows if r.get("status") == "retracted"]
    if retracted:
        ids = ", ".join(sorted(str(r.get("registry_id")) for r in retracted))
        return _refusal(
            "retracted_claim_gate", claim_id, candidate, status, "retracted_claim_stays_retracted",
            f"Claim '{claim_id}' was retracted from the global registry ({ids}). A withdrawn finding is not "
            f"set back to '{status}'.",
            "1. Leave it retracted or superseded.\n"
            "2. If new evidence supports the finding, record it as a NEW claim id citing that evidence; "
            "promotion needs a fresh human approval (aihydro-approve).",
            registry_ids=[r.get("registry_id") for r in retracted])
    stale_rows = [r for r in rows if r.get("status") == "stale"]
    if previous.get("status") != "stale" and not stale_rows:
        return None
    changed = _stale_sources(previous, rows)
    if not changed:            # the detection did not record which sources moved: the old basis is the old spans
        changed = {str(s.get("source_id")) for s in (previous.get("evidence_spans") or []) if isinstance(s, dict)}
    cited = {str(s.get("source_id")) for s in (candidate.get("evidence_spans") or []) if isinstance(s, dict)}
    unchanged_basis = sorted(cited & changed)
    if not unchanged_basis and cited:
        return None            # re-bound: nothing it cites is a source that changed
    return _refusal(
        "stale_claim_gate", claim_id, candidate, status, "stale_evidence_needs_rebinding",
        f"Claim '{claim_id}' is stale: the evidence it was promoted on has changed or can no longer be verified"
        + (f" ({', '.join(sorted(changed))})" if changed else "")
        + f". It cannot be set back to '{status}' while it still rests on that evidence"
        + (f" ({', '.join(unchanged_basis)})" if unchanged_basis else "") + ".",
        "1. Keep the claim 'tested' or 'inconclusive' meanwhile.\n"
        "2. Re-run the originating tool and re-state the claim with add_claim, citing the NEW run in "
        "evidence_spans and dropping the stale source.\n"
        "3. A registry entry that is stale returns to 'promoted' only through a fresh human approval "
        "(aihydro-approve, then promote_claim_to_registry).",
        stale_sources=sorted(changed), still_cited=unchanged_basis)


def propagate_retraction(session_id: str, claim_id: str, claim_dict: dict, reason: str = "") -> list[str]:
    """Retract every live registry row of this claim; return the registry ids changed."""
    from ai_hydro.registry.store import mark_retracted
    changed: list[str] = []
    for row in registry_rows(session_id, claim_id, claim_dict):
        if row.get("status") in ("promoted", "stale") and row.get("registry_id"):
            if mark_retracted(row["registry_id"], reason=(reason or "")[:500], retracted_by="agent"):
                changed.append(row["registry_id"])
    return changed


def registry_conflicts(session_id: str, claims: dict[str, Any]) -> list[dict]:
    """Session claims that assert more than their registry row: supporting status, row stale or retracted."""
    out: list[dict] = []
    for cid, claim in (claims or {}).items():
        if not isinstance(claim, dict) or claim.get("status") not in EVIDENCE_STATUSES:
            continue
        if not claim.get("registry_id") and not claim.get("promoted"):
            continue
        rows = registry_rows(session_id, cid, claim)
        barring = [r for r in rows if r.get("status") in _BARRING]
        if barring and not any(r.get("status") == "promoted" for r in rows):
            out.append({"claim_id": cid, "status": claim.get("status"),
                        "registry_status": barring[-1].get("status"),
                        "registry_id": barring[-1].get("registry_id")})
    return out
