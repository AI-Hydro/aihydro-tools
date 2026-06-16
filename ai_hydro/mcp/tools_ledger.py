"""
Scientific Claims and Assumptions Ledger tools.

Allows for formalizing beliefs (claims) and caveats (assumptions)
within a research session.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from ai_hydro.mcp.app import mcp
from ai_hydro.session import HydroSession
from ai_hydro.session.models import ScientificClaim, Assumption, ClaimScope, EvidenceSpan
from ai_hydro.mcp.helpers import _tool_error_to_dict
from ai_hydro.mcp.ledger_commands import push_claim_event
from aihydro_core.primitives.hashing import content_hash

log = logging.getLogger("ai_hydro.mcp")


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
        # Prefer evidence_spans; fall back to evidence (triggers migration validator)
        raw_evidence = evidence_spans or evidence or []
        claim = ScientificClaim(
            id=claim_id,
            claim=statement,
            claim_type=claim_type,
            status=status,
            confidence=confidence,
            confidence_rationale=confidence_rationale,
            scope=scope,
            limitations=limitations or [],
            evidence_spans=raw_evidence if evidence_spans else [],
            evidence=raw_evidence if not evidence_spans else [],
        )

        claim_dict = claim.model_dump()
        if prereg_id:
            claim_dict["prereg_id"] = prereg_id
        session.claims[claim_id] = claim_dict
        session.save()
        push_claim_event(
            change_type="added",
            session_id=session_id,
            claim_id=claim_id,
            statement=statement,
            status=status,
            claim_type=claim_type,
            confidence=confidence,
            evidence_spans=evidence_spans or [],
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

    uncertainty_verified : set True to confirm that all numeric values in
        this claim have associated uncertainty estimates (CI bounds). Required
        when status='supported' and claim_type='quantitative'; the tool
        returns a teaching error otherwise.
    """
    try:
        session = HydroSession.load(session_id)
        if claim_id not in session.claims:
            raise ValueError(f"Claim '{claim_id}' not found.")

        claim_dict = session.claims[claim_id]
        claim_type = claim_dict.get("claim_type", "")

        # Promotion gate: quantitative claims need verified uncertainty to
        # reach 'supported'. Prevents bare scalar values from being promoted
        # without CI bounds — enforces Phase 1.3 design contract.
        if status == "supported" and claim_type == "quantitative" and not uncertainty_verified:
            return {
                "error": "uncertainty_gate",
                "claim_id": claim_id,
                "claim_type": claim_type,
                "requested_status": status,
                "teaching_error": {
                    "rule": "quantitative_claims_require_uncertainty",
                    "explanation": (
                        "A quantitative claim cannot reach 'supported' status "
                        "without verified uncertainty estimates (confidence intervals). "
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

        claim_dict["status"] = status
        claim_dict["confidence"] = confidence
        claim_dict["confidence_rationale"] = rationale
        claim_dict["updated_at"] = datetime.now(timezone.utc).isoformat()
        if uncertainty_verified:
            claim_dict["uncertainty_verified"] = True

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
    status ∈ {supported, weakly_supported}, uncertainty_verified for
    quantitative claims) then writes a real entry to
    ~/.aihydro/registry/claims.jsonl with evidence version hashes captured
    at this moment.  The registry_id is returned for future staleness checks.

    Requires researcher_approved=True to prevent accidental promotion.
    """
    try:
        if not researcher_approved:
            raise ValueError("Researcher approval is required to promote a claim to global knowledge.")

        session = HydroSession.load(session_id)
        claim_dict = session.claims.get(claim_id)
        if not claim_dict:
            raise ValueError(f"Claim '{claim_id}' not found.")

        claim = ScientificClaim(**claim_dict)

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
        if claim_dict.get("claim_type") == "quantitative" and not claim_dict.get("uncertainty_verified"):
            raise ValueError(
                "Quantitative claim cannot be promoted without uncertainty_verified=True. "
                "Call update_claim_status(uncertainty_verified=True) after confirming "
                "that uncertainty bounds (CIs) are available for all numeric values."
            )

        # ── Snapshot evidence versions ────────────────────────────────────────
        from ai_hydro.registry.store import (
            append as _reg_append,
            build_registry_id,
            snapshot_evidence_versions,
        )

        spans = [s if isinstance(s, dict) else s.model_dump() for s in claim.evidence_spans]
        evidence_versions = snapshot_evidence_versions(session, spans)

        promoted_at = datetime.now(timezone.utc).isoformat()
        registry_id = build_registry_id(session_id, claim_id)

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
            "staleness": None,
        }
        _reg_append(registry_entry)

        # ── Update session claim to reflect promotion ─────────────────────────
        claim_dict["promoted"] = True
        claim_dict["promoted_at"] = promoted_at
        claim_dict["registry_id"] = registry_id
        session.save()

        return {
            "id": claim_id,
            "registry_id": registry_id,
            "status": "promoted",
            "n_evidence_versions": len(evidence_versions),
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
    dataset-type evidence and compares against the hashes captured at
    promotion time.  If a hash differs, the claim is marked stale in the
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
                _reg_mark_stale(rid, stale_sources, reason="evidence_changed")
                # Update session claim status
                claim_dict = session.claims.get(cid)
                if claim_dict:
                    claim_dict["status"] = "stale"
                    claim_dict["staleness_detected_at"] = datetime.now(timezone.utc).isoformat()
                stale_results.append({
                    "claim_id": cid,
                    "registry_id": rid,
                    "stale_sources": stale_sources,
                })
            else:
                fresh_results.append(cid)

        if stale_results:
            session.save()

        return {
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
    """
    try:
        from ai_hydro.registry.store import all_entries, find_by_session

        entries = find_by_session(session_id) if session_id else all_entries()
        if status:
            entries = [e for e in entries if e.get("status") == status]

        n_stale = sum(1 for e in entries if e.get("status") == "stale")

        return {
            "entries": entries,
            "n_entries": len(entries),
            "n_stale": n_stale,
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
