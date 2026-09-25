import pytest
from ai_hydro.session.store import HydroSession
from ai_hydro.session.models import EvidenceSpan, ScientificClaim
from ai_hydro.mcp.tools_ledger import (
    add_claim,
    add_assumption,
    promote_claim_to_registry,
    update_claim_status,
)


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    from ai_hydro.session import store
    from ai_hydro.registry import store as registry
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(registry, "REGISTRY_DIR", tmp_path / "registry")
    monkeypatch.setattr(registry, "CLAIMS_FILE", tmp_path / "registry" / "claims.jsonl")
    monkeypatch.setattr("ai_hydro.mcp.tools_ledger.push_claim_event", lambda **kwargs: None)

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

    # Success: typed references backed by an actual retained synthetic run.
    session.set("_run_log", {"r1": {"run_id": "r1", "session_id": session_id,
        "key_outputs": {"kge": 0.8, "_uncertainty": {"kge": {
            "value": 0.8, "ci_low": 0.7, "ci_high": 0.9, "ci_level": 0.95,
            "n": 30, "method": "synthetic_fixture"}}}}})
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
    update = update_claim_status(
        session_id=session_id,
        claim_id="c2",
        status="supported",
        confidence="high",
        rationale="Bootstrap uncertainty was verified for the recorded KGE estimate.",
        uncertainty_verified=True,
    )
    assert update["status"] == "updated"
    res = promote_claim_to_registry(session_id, "c2", researcher_approved=True)
    assert res["status"] == "promoted"


def test_metric_scoped_empirical_claim_requires_uncertainty():
    session_id = "test-metric-uncertainty-gate"
    HydroSession(session_id).save()
    add_claim(
        session_id=session_id,
        claim_id="c-metric",
        statement="The evaluated run has a recorded KGE estimate.",
        claim_type="empirical_result",
        status="tested",
        confidence="medium",
        confidence_rationale="The estimate comes from a stored evaluation run for one basin.",
        basins=["01031500"],
        period="2000-2010",
        metric="kge",
        evidence_spans=[{"source_type": "run", "source_id": "run-1", "metric_ref": "kge"}],
    )

    blocked = update_claim_status(
        session_id=session_id,
        claim_id="c-metric",
        status="supported",
        confidence="medium",
        rationale="The point estimate is present but uncertainty has not been verified.",
    )
    assert blocked["error"] == "uncertainty_gate"

    allowed = update_claim_status(
        session_id=session_id,
        claim_id="c-metric",
        status="supported",
        confidence="medium",
        rationale="Bootstrap uncertainty was verified for the recorded KGE estimate.",
        uncertainty_verified=True,
    )
    assert allowed["status"] == "updated"
    assert HydroSession.load(session_id).claims["c-metric"]["uncertainty_verified"] is True


def test_metric_scoped_empirical_claim_cannot_be_promoted_without_uncertainty():
    session_id = "test-metric-promotion-uncertainty"
    HydroSession(session_id).save()
    add_claim(
        session_id=session_id,
        claim_id="c-promote-metric",
        statement="The evaluation produced a metric-scoped empirical result.",
        claim_type="empirical_result",
        status="supported",
        confidence="medium",
        confidence_rationale="The point estimate is recorded but its uncertainty is not verified.",
        basins=["01031500"],
        period="2000-2010",
        metric="kge",
        limitations=["Synthetic regression case for one basin."],
        evidence_spans=[{"source_type": "run", "source_id": "run-2", "metric_ref": "kge"}],
    )

    result = promote_claim_to_registry(
        session_id=session_id,
        claim_id="c-promote-metric",
        researcher_approved=True,
    )
    assert result["error"] is True
    assert "cannot be promoted without uncertainty_verified=True" in result["message"]


def test_non_empirical_claim_is_not_misclassified_as_quantitative():
    session_id = "test-methodological-claim-uncertainty"
    HydroSession(session_id).save()
    add_claim(
        session_id=session_id,
        claim_id="c-method",
        statement="The workflow uses KGE as one evaluation criterion.",
        claim_type="methodological",
        status="tested",
        confidence="high",
        confidence_rationale="The criterion is declared directly in the stored workflow specification.",
        basins=["01031500"],
        period="2000-2010",
        metric="kge",
    )

    result = update_claim_status(
        session_id=session_id,
        claim_id="c-method",
        status="supported",
        confidence="high",
        rationale="The stored workflow specification directly records this design choice.",
    )
    assert result["status"] == "updated"

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


def test_add_claim_migrates_legacy_evidence_to_evidence_spans():
    session_id = "test-legacy-evidence-tool"
    HydroSession(session_id).save()

    res = add_claim(
        session_id=session_id,
        claim_id="c-legacy",
        statement="Legacy evidence should not be dropped.",
        claim_type="empirical_result",
        status="tested",
        confidence="medium",
        confidence_rationale="This claim checks that legacy evidence survives add_claim storage.",
        basins=["01031500"],
        period="2000-2020",
        evidence=[{"run_id": "r42", "metric": "baseflow_index"}],
    )
    assert res["status"] == "recorded"

    session = HydroSession.load(session_id)
    spans = session.claims["c-legacy"]["evidence_spans"]
    assert spans == [{"source_type": "run", "source_id": "r42", "metric_ref": "baseflow_index", "page": None, "passage_hash": None}]


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
