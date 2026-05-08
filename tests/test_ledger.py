import pytest
from ai_hydro.session.store import HydroSession
from ai_hydro.session.models import EvidenceSpan, ScientificClaim
from ai_hydro.mcp.tools_ledger import (
    add_claim,
    add_assumption,
    promote_claim_to_registry
)

def test_claims_ledger():
    session_id = "test-ledger-claims"
    session = HydroSession(session_id)
    
    res = add_claim(
        session_id=session_id,
        claim_id="claim.kge_high",
        statement="KGE is high for this basin.",
        claim_type="empirical_result",
        status="tested",
        confidence="medium",
        confidence_rationale="Tested with 10 years of data.",
        basins=["01031500"],
        period="2000-2010"
    )
    assert res["status"] == "recorded"
    
    session2 = HydroSession.load(session_id)
    assert "claim.kge_high" in session2.claims
    assert session2.claims["claim.kge_high"]["confidence"] == "medium"

def test_promotion_gate():
    session_id = "test-promotion"
    session = HydroSession(session_id)
    
    add_claim(
        session_id=session_id,
        claim_id="c1",
        statement="test",
        claim_type="empirical_result",
        status="tested",
        confidence="low",
        confidence_rationale="Preliminary result with limited data, low confidence.",
        basins=["b1"],
        period="p1"
    )

    # Fail: not approved
    res = promote_claim_to_registry(session_id, "c1", researcher_approved=False)
    assert "error" in res
    assert "approval is required" in res["message"]

    # Fail: missing evidence_spans and limitations
    res = promote_claim_to_registry(session_id, "c1", researcher_approved=True)
    assert "error" in res
    assert "evidence" in res["message"]

    # Success: with typed evidence_spans and limitations
    add_claim(
        session_id=session_id,
        claim_id="c2",
        statement="Supported claim",
        claim_type="empirical_result",
        status="supported",
        confidence="high",
        confidence_rationale="Validated across ten years of daily streamflow data with KGE > 0.7.",
        basins=["b1"],
        period="p1",
        limitations=["Only tested on one basin"],
        evidence_spans=[{"source_type": "run", "source_id": "r1", "metric_ref": "kge"}],
    )
    res = promote_claim_to_registry(session_id, "c2", researcher_approved=True)
    assert res["status"] == "promoted"

def test_evidence_span_schema():
    """EvidenceSpan validates source_type and rejects unknown types."""
    span = EvidenceSpan(source_type="run", source_id="run-abc123", metric_ref="kge")
    assert span.source_type == "run"
    assert span.metric_ref == "kge"

    paper_span = EvidenceSpan(
        source_type="paper",
        source_id="addor2017camels",
        page=5,
        passage_hash="abc123def456",
    )
    assert paper_span.page == 5

    with pytest.raises(Exception):
        EvidenceSpan(source_type="invalid_type", source_id="x")


def test_legacy_evidence_migration():
    """Old evidence: list[dict] is coerced to evidence_spans on load."""
    old_dict = {
        "id": "migrated-claim",
        "claim": "Test migration",
        "claim_type": "empirical_result",
        "status": "proposed",
        "confidence": "low",
        "confidence_rationale": "Legacy claim migrated from pre-EvidenceSpan sessions.",
        "scope": {"basins": ["01031500"], "period": "2000-2010"},
        "evidence": [{"run_id": "r42", "kge": 0.71}],  # old format
    }
    claim = ScientificClaim(**old_dict)
    assert len(claim.evidence_spans) == 1
    span = claim.evidence_spans[0]
    assert span.source_type == "run"
    assert span.source_id == "r42"
    assert span.metric_ref == "0.71"


def test_confidence_rationale_min_length():
    """confidence_rationale shorter than 20 chars raises ValueError."""
    with pytest.raises(Exception, match="at least 20 characters"):
        ScientificClaim(
            id="x",
            claim="test",
            claim_type="empirical_result",
            status="proposed",
            confidence="low",
            confidence_rationale="too short",  # 9 chars
            scope={"basins": ["01031500"], "period": "2000"},
        )


def test_assumptions_ledger():
    session_id = "test-ledger-assumptions"
    session = HydroSession(session_id)
    
    res = add_assumption(
        session_id=session_id,
        assumption_id="a1",
        statement="Assumed no pumping.",
        risk="high",
        risk_rationale="Pumping is common in this area.",
        affects=["water_balance"]
    )
    assert res["status"] == "recorded"
    
    session2 = HydroSession.load(session_id)
    assert "a1" in session2.assumptions
    assert session2.assumptions["a1"]["risk"] == "high"
