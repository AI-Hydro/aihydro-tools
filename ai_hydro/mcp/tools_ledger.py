"""
Scientific Claims and Assumptions Ledger tools.

Allows for formalizing beliefs (claims) and caveats (assumptions)
within a research session.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from ai_hydro.mcp.app import mcp
from ai_hydro.session import HydroSession
from ai_hydro.session.models import ScientificClaim, Assumption, ClaimScope, EvidenceSpan
from ai_hydro.mcp.helpers import _tool_error_to_dict
from ai_hydro.mcp.ledger_commands import push_claim_event
from aihydro_core.primitives.hashing import content_hash

log = logging.getLogger("ai_hydro.mcp")

# Metric/keyword markers for hydrology signatures that MAY derive from
# modelled (not observed) streamflow — e.g. GEOGLOWS-routed BFI, flood
# frequency, or flow-duration-curve slope on a gauge-less basin. See
# _claim_touches_hydrology_signature_metric() docstring for why this is a
# name-based heuristic rather than a true evidence-provenance lookup.
_MODELLED_HYDROLOGY_METRIC_MARKERS = (
    "baseflow_index", "bfi", "flood_freq", "flood frequency", "fdc_slope",
    "flow duration", "q_mean", "q5", "q95", "high_flow", "low_flow",
    "runoff_ratio", "stream_elas", "hfd", "half_flow_date", "flow timing",
)
_MODELLED_LIMITATION_ACKNOWLEDGEMENT_MARKERS = (
    "model", "geoglows", "ungauged", "simulat", "unobserved", "no gauge",
)
# USGS gauge IDs are 8-15 digit numeric strings. A claim scoped entirely to
# basins that look like USGS gauge IDs is very likely CONUS observed
# streamflow — the common, legitimate case — and should not be flagged.
_USGS_GAUGE_ID_RE = re.compile(r"^\d{8,15}$")


def _claim_requires_uncertainty(claim_dict: dict) -> bool:
    """Return whether an empirical claim is explicitly tied to a metric.

    ``ScientificClaim.claim_type`` has no ``quantitative`` value. Quantitative
    intent is instead made explicit by ``scope.metric`` or an evidence span's
    ``metric_ref``. Keeping the rule tied to those structured fields avoids
    guessing from numbers in prose (which may be dates, gauge IDs, or sample
    counts) and makes the gate reachable through the public schema.
    """
    if claim_dict.get("claim_type") not in {"empirical_result", "negative_result"}:
        return False
    scope = claim_dict.get("scope") or {}
    if scope.get("metric"):
        return True
    return any(
        isinstance(span, dict) and bool(span.get("metric_ref"))
        for span in claim_dict.get("evidence_spans", [])
    )


def _claim_touches_hydrology_signature_metric(claim_dict: dict) -> bool:
    """
    Heuristic: does this claim's scope.metric or statement reference a
    hydrology-signature metric that may be computed from modelled (not
    observed) streamflow?

    This is a name-based heuristic, not a true evidence-provenance lookup.
    aihydro-lsh's AttributeResult.provenance[family].is_observed and
    value_provenance already record the real answer per attribute value —
    but the lsh_attributes MCP tool does not take a session_id or write to
    the session run log, so a promoted claim's EvidenceSpan cannot currently
    be resolved back to that provenance. Wiring the lsh MCP tool surface into
    the session/run-log system so promotion can do a real lookup is a
    separate, larger architectural task (tracked in audits/STATUS.md as N-12).
    Until then, this keyword gate is the safety net.

    Deliberately does NOT flag a claim whose scope.basins are all
    USGS-gauge-shaped IDs (8-15 digits) — that is the common, legitimate
    CONUS-observed case (e.g. "Q_mean for 01013500"), and flagging it would
    make the gate noise researchers learn to route around rather than a
    signal they act on.
    """
    scope = claim_dict.get("scope") or {}
    basins = scope.get("basins") or []
    if basins and all(_USGS_GAUGE_ID_RE.match(str(b)) for b in basins):
        return False
    haystack = " ".join([
        str(scope.get("metric") or ""),
        str(claim_dict.get("claim", claim_dict.get("statement", "")) or ""),
    ]).lower()
    return any(marker in haystack for marker in _MODELLED_HYDROLOGY_METRIC_MARKERS)


def _revision_fields(session, claim_id: str):
    """``claim_revision_fields`` of a loaded session claim, or None if it cannot be projected."""
    from ai_hydro.approval.records import session_claim_revision
    try:
        return session_claim_revision(session, claim_id)[2]
    except Exception:
        return None


def _record_revision(session, claim_id: str, *, tool: str, reason: str, before=None,
                     bookkeeping: bool = False, extra_cause: dict | None = None):
    """Write the sealed revision for the claim's current state (the only claim writer).

    Called after the in-memory claim is mutated and before ``session.save()``,
    so a claim change that cannot be recorded is never persisted.
    """
    from ai_hydro.approval.records import session_claim_revision
    from ai_hydro.session import claim_revisions
    after = session_claim_revision(session, claim_id)[2]
    return claim_revisions.record_change(
        session.session_id, claim_id, after, tool=tool, reason=reason, before=before,
        bookkeeping=bookkeeping, extra_cause=extra_cause)


def _normalize_evidence_spans(evidence_spans: list[dict] | None, evidence: list[dict] | None) -> list[dict]:
    """Return typed EvidenceSpan-compatible dicts from preferred or legacy evidence inputs."""
    raw = evidence_spans if evidence_spans is not None else evidence
    spans: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        if "source_id" in item:
            spans.append({
                "source_type": item.get("source_type", "run"),
                "source_id": item.get("source_id"),
                "metric_ref": item.get("metric_ref") or item.get("metric") or item.get("key") or item.get("json_path"),
                "page": item.get("page"),
                "passage_hash": item.get("passage_hash"),
            })
            continue
        run_id = item.get("run_id")
        if run_id:
            metric_ref = item.get("metric_ref") or item.get("metric") or item.get("key") or item.get("json_path")
            if metric_ref is None:
                metric_ref = next(
                    (str(k) for k in item.keys() if k not in {"run_id", "source_type", "source_id"}),
                    None,
                )
            spans.append({
                "source_type": item.get("source_type", "run"),
                "source_id": run_id,
                "metric_ref": str(metric_ref) if metric_ref is not None else None,
                "page": item.get("page"),
                "passage_hash": item.get("passage_hash"),
            })
    return [EvidenceSpan(**span).model_dump() for span in spans]


@mcp.tool()
def add_claim(
    session_id: str,
    claim_id: str,
    statement: str,
    claim_type: str,
    status: str,
    confidence: str,
    confidence_rationale: str,
    basins: list[str],
    period: str,
    metric: str | None = None,
    limitations: list[str] | None = None,
    evidence: list[dict] | None = None,
    evidence_spans: list[dict] | None = None,
    prereg_id: str | None = None,
) -> dict:
    """
    Add a scoped scientific claim to the session ledger.

    evidence_spans: preferred format — list of dicts matching EvidenceSpan schema:
        [{"source_type": "run", "source_id": "<run_id>", "metric_ref": "kge"}]
    evidence: legacy format — list of untyped dicts (auto-migrated to evidence_spans).
        Pass either evidence or evidence_spans, not both.
    prereg_id: if this claim was anticipated in a pre-registered research plan,
        pass the prereg_id returned by register_research_plan. Marks the claim as
        confirmatory (planned) vs exploratory (post-hoc) in the defensibility report.
    """
    try:
        session = HydroSession.load(session_id)

        scope = ClaimScope(basins=basins, period=period, metric=metric)
        normalized_spans = _normalize_evidence_spans(evidence_spans, evidence)
        claim = ScientificClaim(
            id=claim_id,
            claim=statement,
            claim_type=claim_type,
            status=status,
            confidence=confidence,
            confidence_rationale=confidence_rationale,
            scope=scope,
            limitations=limitations or [],
            evidence_spans=normalized_spans,
        )

        claim_dict = claim.model_dump()
        if prereg_id:
            claim_dict["prereg_id"] = prereg_id
        existed = claim_id in session.claims
        before = _revision_fields(session, claim_id) if existed else None
        session.claims[claim_id] = claim_dict
        _record_revision(session, claim_id, tool="add_claim", before=before,
                         reason="redefined" if existed else "created")
        session.save()
        push_claim_event(
            change_type="added",
            session_id=session_id,
            claim_id=claim_id,
            statement=statement,
            status=status,
            claim_type=claim_type,
            confidence=confidence,
            evidence_spans=normalized_spans,
            limitations=limitations or [],
        )
        return {"id": claim_id, "status": "recorded"}
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def update_claim_status(
    session_id: str,
    claim_id: str,
    status: str,
    confidence: str,
    rationale: str,
    uncertainty_verified: bool = False,
) -> dict:
    """
    Update the status and confidence of an existing claim.

    uncertainty_verified : set True to confirm that the metric estimate in an
        empirical claim has an associated uncertainty estimate. Required when
        status='supported' and the claim has scope.metric or an evidence-span
        metric_ref; the tool returns a teaching error otherwise.
    """
    try:
        session = HydroSession.load(session_id)
        if claim_id not in session.claims:
            raise ValueError(f"Claim '{claim_id}' not found.")

        claim_dict = session.claims[claim_id]
        claim_type = claim_dict.get("claim_type", "")

        # Metric-scoped empirical claims need verified uncertainty to reach
        # 'supported'. The structured metric fields make this rule reachable
        # without inferring quantitative intent from prose.
        if status == "supported" and _claim_requires_uncertainty(claim_dict) and not uncertainty_verified:
            return {
                "error": "uncertainty_gate",
                "claim_id": claim_id,
                "claim_type": claim_type,
                "requested_status": status,
                "teaching_error": {
                    "rule": "quantitative_claims_require_uncertainty",
                    "explanation": (
                        "A metric-scoped empirical claim cannot reach 'supported' status "
                        "without a verified uncertainty estimate. "
                        "Confirm that the underlying run results include bootstrap CIs "
                        "(check result._uncertainty or run the analysis with "
                        "uncertainty output enabled), then re-call with "
                        "uncertainty_verified=True."
                    ),
                    "how_to_fix": (
                        "1. Verify that extract_hydrological_signatures / the relevant "
                        "   analysis tool returned an '_uncertainty' key in its result.\n"
                        "2. Include uncertainty bounds in the claim statement or confidence_rationale.\n"
                        "3. Re-call update_claim_status with uncertainty_verified=True."
                    ),
                },
            }

        before = _revision_fields(session, claim_id)
        claim_dict["status"] = status
        claim_dict["confidence"] = confidence
        claim_dict["confidence_rationale"] = rationale
        claim_dict["updated_at"] = datetime.now(timezone.utc).isoformat()
        if uncertainty_verified:
            claim_dict["uncertainty_verified"] = True

        _record_revision(session, claim_id, tool="update_claim_status",
                         reason="status_update", before=before)
        session.save()
        push_claim_event(
            change_type="updated",
            session_id=session_id,
            claim_id=claim_id,
            statement=claim_dict.get("claim", ""),
            status=status,
            claim_type=claim_type,
            confidence=confidence,
        )
        return {"id": claim_id, "status": "updated"}
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def add_assumption(
    session_id: str,
    assumption_id: str,
    statement: str,
    risk: str,
    risk_rationale: str,
    affects: list[str],
    scope: str | None = None
) -> dict:
    """
    Record a scientific assumption or caveat in the session ledger.
    """
    try:
        session = HydroSession.load(session_id)
        
        assumption = Assumption(
            id=assumption_id,
            statement=statement,
            risk=risk,
            risk_rationale=risk_rationale,
            affects=affects,
            scope=scope or session_id
        )
        
        session.assumptions[assumption_id] = assumption.model_dump()
        session.save()
        return {"id": assumption_id, "status": "recorded"}
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def list_claims(session_id: str, status: str | None = None) -> list[dict]:
    """List all scientific claims in the session."""
    try:
        session = HydroSession.load(session_id)
        claims = list(session.claims.values())
        if status:
            claims = [c for c in claims if c["status"] == status]
        return claims
    except Exception:
        return []


@mcp.tool()
def list_assumptions(session_id: str, validated: bool | None = None) -> list[dict]:
    """List all assumptions in the session."""
    try:
        session = HydroSession.load(session_id)
        assumptions = list(session.assumptions.values())
        if validated is not None:
            assumptions = [a for a in assumptions if a["validated"] == validated]
        return assumptions
    except Exception:
        return []


@mcp.tool()
def promote_claim_to_registry(
    session_id: str,
    claim_id: str,
    researcher_approved: bool = False,
) -> dict:
    """
    Promote a session claim to the global knowledge registry.

    Passes through a strict validation gate (evidence_spans, limitations,
    status ∈ {supported, weakly_supported}, persisted metric/uncertainty checks and
    uncertainty_verified for
    metric-scoped empirical claims, a modelled-streamflow limitation when the claim
    touches a hydrology-signature metric) then writes a real entry to
    $AIHYDRO_HOME/registry/claims.jsonl (default ~/.aihydro/registry/claims.jsonl)
    with evidence version hashes captured at this moment.  The registry_id is returned for future staleness checks.

    Authority (ADR-002a): promotion also needs a human approval record bound to the
    claim's current revision (claim fields that land in the registry row, plus the
    fingerprints of the retained evidence it cites). The researcher creates it in
    their own terminal with `aihydro-approve <session_id> <claim_id>`; no tool can
    create it. Without a matching, unused record this refuses with code
    APPROVAL_REQUIRED and names that command. Editing the claim or mutating its
    retained evidence invalidates the approval, and one approval authorises one
    promotion. `researcher_approved=True` is only a request flag: required for
    compatibility but never sufficient. The registry row stamps the verifier-derived approval channel, trust root,
    principal, signer and policy (ADR-002b). Only a `system` trust root with an
    `sk` touch key is a boundary against a same-OS-user process.
    """
    try:
        from ai_hydro.approval.records import (
            ApprovalRequiredError,
            approval_stamp,
            claim_revision_digest,
            find_approval,
        )

        session = HydroSession.load(session_id)
        claim_dict = session.claims.get(claim_id)
        if not claim_dict:
            raise ValueError(f"Claim '{claim_id}' not found.")

        claim = ScientificClaim(**claim_dict)

        if not researcher_approved:
            raise ApprovalRequiredError(
                session_id, claim_id, None,
                "Researcher approval is required to promote a claim to global knowledge. "
                "researcher_approved=False: promotion was not requested, and a human approval "
                "record is also required (see approval_command).")

        # ── Promotion gate ────────────────────────────────────────────────────
        if not claim.evidence_spans:
            raise ValueError(
                "Promotion requires at least one evidence_span. "
                "Add a typed EvidenceSpan (run, paper, or dataset) via add_claim or update_claim_status."
            )
        if not claim.limitations:
            raise ValueError("Promotion requires at least one limitation to be listed.")
        if claim.status not in ["supported", "weakly_supported"]:
            raise ValueError(f"Claim status '{claim.status}' is not eligible for promotion.")
        if _claim_requires_uncertainty(claim_dict) and not claim_dict.get("uncertainty_verified"):
            raise ValueError(
                "Metric-scoped empirical claim cannot be promoted without uncertainty_verified=True. "
                "Call update_claim_status(uncertainty_verified=True) after confirming "
                "that an uncertainty estimate is available for the referenced metric."
            )
        if _claim_touches_hydrology_signature_metric(claim_dict):
            limitations_text = " ".join(claim.limitations).lower()
            if not any(w in limitations_text for w in _MODELLED_LIMITATION_ACKNOWLEDGEMENT_MARKERS):
                raise ValueError(
                    "Claim references a hydrology-signature metric (e.g. baseflow index, "
                    "flood frequency, flow-duration-curve slope) that may be computed from "
                    "modelled streamflow (e.g. GEOGLOWS routing on a gauge-less basin) rather "
                    "than gauge observations. Promotion requires a limitation that acknowledges "
                    "this — e.g. 'Signature computed from modelled GEOGLOWS discharge, not gauge "
                    "observations.' If the underlying data is confirmed gauge-observed, add a "
                    "limitation stating that explicitly instead."
                )

        # ── Snapshot evidence versions ────────────────────────────────────────
        from ai_hydro.registry.store import (
            ApprovalAlreadyConsumed,
            append as _reg_append,
            build_registry_id,
            find_by_session,
        )

        spans = [s if isinstance(s, dict) else s.model_dump() for s in claim.evidence_spans]
        from ai_hydro.registry.evidence import EvidenceError, verified_versions, fingerprint
        requires_uncertainty = _claim_requires_uncertainty(claim_dict)
        metric_spans = [span for span in spans if span.get("metric_ref")]
        if requires_uncertainty and not metric_spans:
            raise EvidenceError("EVIDENCE_METRIC_UNAVAILABLE", claim_id,
                                "Metric-scoped claims require an explicit metric_ref on their evidence.")
        if claim.scope.metric:
            def metric_name(name):
                for prefix in ("metric.", "key_outputs.", "data."):
                    if name.startswith(prefix):
                        return name[len(prefix):]
                return name
            if not any(metric_name(span["metric_ref"]) == metric_name(claim.scope.metric)
                       for span in metric_spans):
                raise EvidenceError("EVIDENCE_METRIC_MISMATCH", claim_id,
                                    "The scope metric does not match any referenced evidence metric.")
        evidence_versions = verified_versions(session, spans, require_uncertainty=requires_uncertainty)

        # ── Human approval (ADR-002a) ─────────────────────────────────────────
        # Checked last so the researcher is only asked to approve a claim that
        # already passes every other gate. The record must match the CURRENT
        # revision: claim fields that land in the registry row plus the
        # retained-evidence fingerprints just computed above. It must also be
        # unconsumed (single use). A tool argument can request but never confer
        # this.
        claim_rev = claim_revision_digest(claim_dict, evidence_versions)

        # ── Revision chain (slice 2) ──────────────────────────────────────────
        # Approvals bind to the claim's latest *stored* revision. A claim with no
        # history gets a legacy_unrecorded revision 0 now. If the claim as it is
        # now (including retained-evidence fingerprints) differs from the latest
        # stored revision, record why; the old approval cannot cover that state.
        from ai_hydro.approval.records import claim_revision_fields
        from ai_hydro.session import claim_revisions
        now_fields = claim_revision_fields(claim_dict, evidence_versions)
        stored = claim_revisions.ensure_baseline(
            session_id, claim_id, now_fields, tool="promote_claim_to_registry").to_dict()
        if stored["revision_digest"] != claim_rev:
            only_evidence = {k: v for k, v in stored["content"].items() if k != "evidence_versions"} \
                == {k: v for k, v in now_fields.items() if k != "evidence_versions"}
            stored = claim_revisions.record_change(
                session_id, claim_id, now_fields, tool="promote_claim_to_registry",
                reason="evidence_drift" if only_evidence else "out_of_band_edit").to_dict()
            # The old approval cannot cover the new revision. An approval that
            # already binds the new revision (the researcher approved the live
            # state) is honoured below; otherwise refuse.
            if find_approval(session_id, claim_id, claim_rev, unconsumed_only=True) is None:
                raise ApprovalRequiredError(
                    session_id, claim_id, claim_rev,
                    ("The retained evidence this claim cites changed since its last recorded "
                     "revision; a new revision was recorded with cause evidence_drift. Promotion "
                     "needs a fresh human approval for the new revision."
                     if only_evidence else
                     "The claim changed outside the recorded revision chain; a new revision was "
                     "recorded. Promotion needs a fresh human approval for the new revision."))
        approval = find_approval(session_id, claim_id, claim_rev, unconsumed_only=True)
        if approval is None:
            already = find_approval(session_id, claim_id, claim_rev) is not None
            raise ApprovalRequiredError(
                session_id, claim_id, claim_rev,
                ("The approval for this claim revision was already used for an earlier promotion; "
                 "an approval authorises one promotion, so a fresh approval is required."
                 if already else
                 "No human approval record exists for the current revision of this claim or its "
                 "retained evidence (never approved, or the claim or evidence changed after "
                 "approval). researcher_approved=True does not substitute for one."))

        if approval["claim_revision_digest"] != stored["revision_digest"]:   # defence in depth
            raise ApprovalRequiredError(
                session_id, claim_id, claim_rev,
                "The approval is not bound to the latest stored revision of this claim.")

        promoted_at = datetime.now(timezone.utc).isoformat()
        revision = fingerprint({"statement": claim.claim, "scope": claim.scope.model_dump(),
                                "claim_type": claim.claim_type, "confidence": claim.confidence,
                                "limitations": claim.limitations, "evidence_spans": spans,
                                "evidence_versions": evidence_versions})
        registry_id = build_registry_id(session_id, claim_id, revision)
        # A fresh approval must not silently reuse a stale/retracted entry.
        # Preserve that history even if restored evidence has identical bytes.
        prior = {e["registry_id"]: e.get("status") for e in find_by_session(session_id)}
        base_id, attempt = registry_id, 0
        while registry_id in prior and prior[registry_id] != "promoted":
            attempt += 1
            registry_id = f"{base_id}.r{attempt}"

        verification = {
            "level": "retained_record_integrity",
            "scope_alignment": "not_verified",
            "method_validity": "not_verified",
            "claim_text_alignment": "not_verified",
        }

        # Verifier-derived stamp (ADR-002b A2): channel names the trust root, plus
        # principal, signer and policy; never a constant.
        stamp = approval_stamp(approval)
        registry_entry = {
            "registry_id": registry_id,
            "claim_id": claim_id,
            "session_id": session_id,
            "statement": claim_dict.get("claim", claim_dict.get("statement", "")),
            "claim_type": claim_dict.get("claim_type", ""),
            "status": "promoted",
            "confidence": claim_dict.get("confidence", ""),
            "evidence_spans": spans,
            "limitations": list(claim.limitations),
            "prereg_id": claim_dict.get("prereg_id"),
            "promoted_at": promoted_at,
            "evidence_versions": evidence_versions,
            "evidence_schema_version": 2,
            "scope": claim.scope.model_dump(),
            "evidence_verification": verification,
            "approval": stamp,
            "claim_revision_digest": stored["revision_digest"],
            "claim_revision": stored["revision"],
            "staleness": None,
        }
        try:
            _reg_append(registry_entry)
        except ApprovalAlreadyConsumed as exc:   # lost a race for the same approval
            raise ApprovalRequiredError(session_id, claim_id, claim_rev, str(exc)) from exc

        # ── Update session claim to reflect promotion ─────────────────────────
        claim_dict["promoted"] = True
        claim_dict["promoted_at"] = promoted_at
        claim_dict["registry_id"] = registry_id
        # Promotion is outside the revision digest, so this row repeats the
        # approved revision_digest; it records the bookkeeping event itself.
        claim_revisions.record_change(
            session_id, claim_id, now_fields, tool="promote_claim_to_registry",
            reason="promotion", bookkeeping=True,
            extra_cause={"registry_id": registry_id,
                         "approval_record_digest": approval["record_digest"]})
        session.save()

        return {
            "id": claim_id,
            "registry_id": registry_id,
            "status": "promoted",
            "n_evidence_versions": len(evidence_versions),
            "evidence_verification": verification,
            "approval": {**stamp, "approver": approval["approver"]["id"]},
            "note": (
                f"Claim '{claim_id}' written to global registry as '{registry_id}'. "
                "Call check_registry_staleness to detect when underlying data changes."
            ),
        }
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def check_registry_staleness(session_id: str) -> dict:
    """
    Check all promoted claims from this session for staleness.

    For each claim with a registry entry, recomputes content hashes of
    retained evidence and compares against hashes captured at promotion.
    Missing or legacy unverifiable evidence also needs review. If evidence differs, the claim is marked stale in the
    registry and its status is updated to 'stale' in the session.

    Returns:
        n_checked     — number of promoted claims checked
        n_stale       — number of claims newly marked stale
        n_already_stale — claims already stale (not rechecked)
        stale_claims  — list of {claim_id, registry_id, stale_sources}
        fresh_claims  — list of claim_ids whose evidence is unchanged
    """
    try:
        from ai_hydro.registry.store import (
            find_by_session,
            mark_stale as _reg_mark_stale,
            check_evidence_staleness,
        )

        session = HydroSession.load(session_id)

        entries = find_by_session(session_id)
        promoted = [e for e in entries if e.get("status") == "promoted"]
        already_stale = [e for e in entries if e.get("status") == "stale"]

        stale_results = []
        fresh_results = []

        for entry in promoted:
            cid = entry["claim_id"]
            rid = entry["registry_id"]
            spans = entry.get("evidence_spans", [])
            ev_versions = entry.get("evidence_versions", {})

            stale_sources = check_evidence_staleness(session, ev_versions, spans)

            if stale_sources:
                legacy = entry.get("evidence_schema_version") != 2
                reason = "legacy_evidence_unverifiable" if legacy else "evidence_changed_missing_or_unverifiable"
                _reg_mark_stale(rid, stale_sources, reason=reason)
                # Update session claim status
                claim_dict = session.claims.get(cid)
                if claim_dict and claim_dict.get("registry_id", rid) == rid:
                    before = _revision_fields(session, cid)
                    claim_dict["status"] = "stale"
                    claim_dict["staleness_detected_at"] = datetime.now(timezone.utc).isoformat()
                    _record_revision(session, cid, tool="check_registry_staleness",
                                     reason="staleness", before=before,
                                     extra_cause={"registry_id": rid, "stale_sources": list(stale_sources)})
                stale_results.append({
                    "claim_id": cid,
                    "registry_id": rid,
                    "stale_sources": stale_sources,
                    "reason": reason,
                })
            else:
                fresh_results.append(cid)

        if stale_results:
            session.save()

        # Registry rows are the only external anchor of the revision chain:
        # compare the revision each row was promoted at with what the chain holds.
        from ai_hydro.session import claim_revisions
        mismatches = []
        for entry in entries:
            rev_no, rev_digest = entry.get("claim_revision"), entry.get("claim_revision_digest")
            if rev_no is None or not rev_digest:
                continue                      # row predates revision stamping
            reason = None
            try:
                row = claim_revisions.get_revision(session_id, entry["claim_id"], rev_no)
                if row is None:
                    reason = "missing_revision"
                elif row["revision_digest"] != rev_digest:
                    reason = "different_digest"
            except claim_revisions.ClaimRevisionError:
                reason = "chain_corrupt"
            if reason:
                mismatches.append({"claim_id": entry["claim_id"], "registry_id": entry.get("registry_id"),
                                   "claim_revision": rev_no, "reason": reason,
                                   "flag": "revision_chain_mismatch"})

        return {
            "revision_chain_mismatches": mismatches,
            "n_checked": len(promoted),
            "n_stale": len(stale_results),
            "n_already_stale": len(already_stale),
            "stale_claims": stale_results,
            "fresh_claims": fresh_results,
            "note": (
                "Stale claims have had their session status set to 'stale'. "
                "Re-run the originating tool with fresh data and call "
                "promote_claim_to_registry again to refresh."
            ) if stale_results else "All checked claims have current evidence.",
        }
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def list_registry_claims(
    session_id: str | None = None,
    status: str | None = None,
) -> dict:
    """
    List entries in the global claim registry.

    session_id : filter to claims from a specific session (optional).
    status     : filter by status — "promoted", "stale", "retracted" (optional).

    Returns:
        entries   — list of registry entry dicts
        n_entries — count
        n_stale   — stale count across the full filter result
        n_self_asserted — entries with no human approval record (legacy rows);
                          each such entry shows ``approval: "self_asserted"``
    """
    try:
        from ai_hydro.registry.store import all_entries, find_by_session

        from ai_hydro.registry.store import SELF_ASSERTED, with_approval_label

        entries = find_by_session(session_id) if session_id else all_entries()
        if status:
            entries = [e for e in entries if e.get("status") == status]

        n_stale = sum(1 for e in entries if e.get("status") == "stale")
        # Legacy rows have no approval stamp; label them at read time (the
        # stored rows are not rewritten).
        entries = [with_approval_label(e) for e in entries]

        return {
            "entries": entries,
            "n_entries": len(entries),
            "n_stale": n_stale,
            "n_self_asserted": sum(1 for e in entries if e["approval"] == SELF_ASSERTED),
        }
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def draft_claim_from_run(
    session_id: str,
    run_id: str,
    metric_ref: str,
    claim_id: str | None = None,
) -> dict:
    """
    Draft a claim pre-bound to evidence from a Tier 1 run. Reads
    session._run_log[run_id], returns a template with evidence_spans filled
    in. Agent authors only the 'statement' and 'confidence_rationale'.
    run_id: from a Tier 1 tool's _run_id field. metric_ref: e.g. kge,
    runoff_ratio, baseflow_index.
    """
    try:
        session = HydroSession.load(session_id)
        run_log: dict = session.get("_run_log") or {}
        run = run_log.get(run_id)

        if not run:
            available = list(run_log.keys())
            return _tool_error_to_dict(
                ValueError(
                    f"Run '{run_id}' not found in session '{session_id}'. "
                    f"Available run IDs: {available or ['none — no Tier 1 tools have run yet']}. "
                    "Run a Tier 1 tool first (e.g. extract_hydrological_signatures)."
                )
            )

        # Infer scope from session state
        basins = [session.site_id] if session.site_id else []
        key_outputs = run.get("key_outputs", {})

        # Suggested claim ID based on run_id and metric
        if not claim_id:
            safe_metric = metric_ref.replace("/", "_").replace(".", "_")
            claim_id = f"claim.{run_id}.{safe_metric}"

        template: dict = {
            "session_id":           session_id,
            "claim_id":             claim_id,
            "statement":            f"<AUTHOR: describe what {metric_ref}={key_outputs.get(metric_ref, '?')} means scientifically for this basin>",
            "claim_type":           "empirical_result",
            "status":               "proposed",
            "confidence":           "low",
            "confidence_rationale": "<AUTHOR: ≥20 chars — describe why this confidence level is appropriate>",
            "basins":               basins,
            "period":               run.get("period", "unknown"),
            "metric":               metric_ref,
            "evidence_spans": [
                {
                    "source_type": "run",
                    "source_id":   run_id,
                    "metric_ref":  metric_ref,
                }
            ],
            "limitations": [],
        }

        return {
            "status":         "drafted",
            "claim_template": template,
            "key_outputs":    key_outputs,
            "note": (
                "1. Replace <AUTHOR: ...> placeholders with your scientific interpretation. "
                "2. Set 'confidence' to low/medium/high and write a ≥20-char 'confidence_rationale'. "
                "3. Add at least one 'limitations' entry. "
                "4. Call add_claim(**claim_template) to record it."
            ),
        }
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def register_research_plan(
    session_id: str,
    hypothesis: str,
    planned_analyses: list[str],
) -> dict:
    """
    Pre-register a research plan for this session.

    Locks the hypothesis and planned analyses with a content hash and
    timestamp. Once locked, the plan is immutable — re-calling this tool
    on the same session returns a teaching error rather than overwriting.

    Claims subsequently filed with prereg_id set to the returned prereg_id
    are classified as **confirmatory** (pre-planned). All other claims are
    **exploratory** (post-hoc). The defensibility report renders this
    distinction in Section 7 (Pre-registration Plan).

    hypothesis: one-sentence scientific question or prediction for this
        session (e.g. "Baseflow index at site X exceeds 0.5").
    planned_analyses: list of analysis names/descriptions the researcher
        commits to running before seeing results (e.g. ["extract_hydrological_signatures",
        "compute_flood_frequency"]).

    Returns:
        prereg_id       — stable ID to pass to add_claim(prereg_id=...)
        content_hash    — SHA-256 fingerprint of {hypothesis, planned_analyses}
        locked_at       — ISO-8601 UTC timestamp of the lock
        hypothesis      — echo
        n_planned       — number of planned analyses registered
    """
    try:
        if not hypothesis or not hypothesis.strip():
            raise ValueError("hypothesis must be a non-empty string.")
        if not planned_analyses:
            raise ValueError("planned_analyses must contain at least one entry.")

        session = HydroSession.load(session_id)

        existing = session.get("_research_plan")
        if existing and existing.get("locked"):
            return {
                "error": "plan_already_locked",
                "prereg_id": existing.get("prereg_id"),
                "locked_at": existing.get("locked_at"),
                "teaching_error": {
                    "rule": "research_plan_immutable_after_lock",
                    "explanation": (
                        "A research plan has already been registered and locked for "
                        f"session '{session_id}'. Pre-registration is immutable — "
                        "the plan cannot be changed after locking, by design. "
                        "This preserves the confirmatory/exploratory distinction: "
                        "any claim filed after the plan was locked can only be "
                        "confirmatory if it was explicitly anticipated."
                    ),
                    "how_to_fix": (
                        "Use the existing prereg_id when calling add_claim to mark "
                        "claims as confirmatory. Start a new session if a different "
                        "hypothesis is needed."
                    ),
                },
            }

        locked_at = datetime.now(timezone.utc).isoformat()
        plan_payload = {
            "hypothesis": hypothesis.strip(),
            "planned_analyses": [a.strip() for a in planned_analyses if a.strip()],
        }
        chash = content_hash(plan_payload)

        # Build prereg_id: prereg.<session_frag>.<date>.<hash_frag>
        date_str = locked_at[:10].replace("-", "")
        session_frag = session_id[:8].replace(".", "")
        prereg_id = f"prereg.{session_frag}.{date_str}.{chash[:6]}"

        plan = {
            "prereg_id": prereg_id,
            "hypothesis": plan_payload["hypothesis"],
            "planned_analyses": plan_payload["planned_analyses"],
            "content_hash": chash,
            "locked_at": locked_at,
            "locked": True,
        }
        session.set("_research_plan", plan)
        session.save()

        return {
            "prereg_id": prereg_id,
            "content_hash": chash,
            "locked_at": locked_at,
            "hypothesis": plan["hypothesis"],
            "n_planned": len(plan["planned_analyses"]),
            "note": (
                f"Plan locked. Pass prereg_id='{prereg_id}' to add_claim for any "
                "claims that directly test this hypothesis. Claims without a "
                "prereg_id will be labelled exploratory in the defensibility report."
            ),
        }
    except Exception as exc:
        return _tool_error_to_dict(exc)
