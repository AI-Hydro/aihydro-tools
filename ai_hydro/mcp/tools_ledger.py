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
) -> dict:
    """
    Add a scoped scientific claim to the session ledger.

    evidence_spans: preferred format — list of dicts matching EvidenceSpan schema:
        [{"source_type": "run", "source_id": "<run_id>", "metric_ref": "kge"}]
    evidence: legacy format — list of untyped dicts (auto-migrated to evidence_spans).
        Pass either evidence or evidence_spans, not both.
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
        
        session.claims[claim_id] = claim.model_dump()
        session.save()
        return {"id": claim_id, "status": "recorded"}
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def update_claim_status(
    session_id: str,
    claim_id: str,
    status: str,
    confidence: str,
    rationale: str
) -> dict:
    """
    Update the status and confidence of an existing claim.
    """
    try:
        session = HydroSession.load(session_id)
        if claim_id not in session.claims:
            raise ValueError(f"Claim '{claim_id}' not found.")
            
        claim_dict = session.claims[claim_id]
        claim_dict["status"] = status
        claim_dict["confidence"] = confidence
        claim_dict["confidence_rationale"] = rationale
        claim_dict["updated_at"] = datetime.now(timezone.utc).isoformat()
        
        session.save()
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
    researcher_approved: bool = False
) -> dict:
    """
    Promote a session claim to the global knowledge registry.
    Requires researcher approval and passes through a strict validation gate.
    """
    try:
        if not researcher_approved:
            raise ValueError("Researcher approval is required to promote a claim to global knowledge.")
            
        session = HydroSession.load(session_id)
        claim_dict = session.claims.get(claim_id)
        if not claim_dict:
            raise ValueError(f"Claim '{claim_id}' not found.")
            
        claim = ScientificClaim(**claim_dict)
        
        # Promotion Gate Checks
        if not claim.evidence_spans:
            raise ValueError(
                "Promotion requires at least one evidence_span. "
                "Add a typed EvidenceSpan (run, paper, or dataset) via add_claim or update_claim_status."
            )
        if not claim.limitations:
            raise ValueError("Promotion requires at least one limitation to be listed.")
        if claim.status not in ["supported", "weakly_supported"]:
            raise ValueError(f"Claim status '{claim.status}' is not eligible for promotion.")
        
        # In a real system, this would write to a shared knowledge database or PR.
        # For this prototype, we record the promotion in the session metadata.
        claim_dict["promoted"] = True
        claim_dict["promoted_at"] = datetime.now(timezone.utc).isoformat()
        session.save()
        
        return {
            "id": claim_id,
            "status": "promoted",
            "note": "Claim successfully passed promotion gate and is marked as global knowledge candidate."
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
