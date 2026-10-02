"""``promotion_violations`` (P1 / W1): one policy, advisory at drafting time, blocking at promotion.

Per family: a claim that triggers it shows the violation in ``add_claim``'s
``promotion_check`` AND the same violation is what ``promote_claim_to_registry``
refuses with (same code where production has one; the identical message where
production's envelope is a bare ``UNEXPECTED_ERROR``). Synthetic fixtures only.
"""
from __future__ import annotations

import copy

import pytest

from ai_hydro.claims.promotion_policy import promotion_violations
from ai_hydro.mcp.tools_ledger import add_claim, promote_claim_to_registry, update_claim_status
from ai_hydro.registry import store as registry
from ai_hydro.session import store
from ai_hydro.session.evidence import capture_result_evidence
from ai_hydro.session.store import HydroSession
from approval_helpers import basin_ref_full

SID = "pol"
RAT = "Synthetic fixture only, regression case."
LIM = "Synthetic regression case, no real research conclusion."


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(registry, "REGISTRY_DIR", tmp_path / "registry")
    monkeypatch.setattr(registry, "CLAIMS_FILE", tmp_path / "registry" / "claims.jsonl")
    monkeypatch.setattr("ai_hydro.knowledge.embeddings.PASSAGE_INDEX_PATH", tmp_path / "passages.jsonl")


def _result(flag_status="pass", metric="nse"):
    return {"data": {metric: 0.8},
            "uncertainty": {metric: {"value": 0.8, "ci_low": 0.7, "ci_high": 0.9, "ci_level": 0.95,
                                     "n": 30, "method": "synthetic_fixture"}},
            "quality_flags": [{"validator": "fixture_check", "status": flag_status}]}


def _run(run_id, **kw):
    ev = capture_result_evidence(_result(**kw))
    return {"run_id": run_id, "session_id": SID, "tool_name": "fixture",
            "key_outputs": {}, "evidence": ev}


@pytest.fixture
def session():
    s = HydroSession(SID)
    s.set("_run_log", {
        "r1": _run("r1"),
        "r_fail": _run("r_fail", flag_status="fail"),
        "r_warn": _run("r_warn", flag_status="warning"),
        "r_bfi": _run("r_bfi", metric="baseflow_index"),
    })
    s.save()
    return s


def draft(claim_id="c1", **over):
    args = dict(session_id=SID, claim_id=claim_id, statement="Synthetic NSE is 0.8",
                claim_type="empirical_result", status="supported", confidence="medium",
                confidence_rationale="Synthetic fixture only, regression case.", basins=["synthetic"], period="2000-2001",
                metric="nse", limitations=[LIM],
                evidence_spans=[{"source_type": "run", "source_id": "r1", "metric_ref": "nse"}],
                basin_refs=[basin_ref_full("synthetic")])
    args.update(over)
    return add_claim(**args)


def verify(claim_id="c1", **kw):
    return update_claim_status(SID, claim_id, kw.pop("status", "supported"), "medium",
                               "Synthetic fixture only, regression case.", uncertainty_verified=True)


def codes(check):
    return [v["code"] for v in check]


def first_blocking(check):
    return next(v for v in check if v["blocking"])


def assert_promotion_refuses_with(violation, claim_id="c1"):
    resp = promote_claim_to_registry(SID, claim_id, researcher_approved=True)
    assert resp["error"] is True, resp
    assert resp["message"] == violation["message"]
    if resp["code"] != "UNEXPECTED_ERROR":     # production's bare-ValueError refusals keep that code
        assert resp["code"] == violation["code"]


def _drop_basin_records(claim_id="c1"):
    s = HydroSession.load(SID)
    s.claims[claim_id].pop("basin_ref_records")
    s.save()


# family, expected code, draft overrides, post-draft step (None | callable), via update_claim_status?
CASES = [
    ("basin_identity", "BASIN_REF_REQUIRED", dict(basins=["elsewhere"], basin_refs=None), verify),
    ("basin_identity", "BASIN_REF_UNKNOWN", {}, "drop_basin_records"),
    ("evidence", "EVIDENCE_REQUIRED", dict(evidence_spans=[]), verify),
    ("limitations", "LIMITATIONS_REQUIRED", dict(limitations=[]), verify),
    ("status", "STATUS_NOT_ELIGIBLE", dict(status="proposed"), None),
    ("uncertainty", "UNCERTAINTY_NOT_VERIFIED", {}, None),
    ("observed_modelled", "MODELLED_LIMITATION_REQUIRED",
     dict(metric="baseflow_index", basins=["ungauged-synthetic"],
          evidence_spans=[{"source_type": "run", "source_id": "r_bfi", "metric_ref": "baseflow_index"}]), verify),
    ("metric_binding", "EVIDENCE_METRIC_UNAVAILABLE",
     dict(evidence_spans=[{"source_type": "run", "source_id": "r1"}]), verify),
    ("metric_binding", "EVIDENCE_METRIC_MISMATCH", dict(metric="kge"), verify),
    ("evidence", "EVIDENCE_UNRESOLVED",
     dict(evidence_spans=[{"source_type": "run", "source_id": "ghost", "metric_ref": "nse"}]), verify),
    ("evidence", "EVIDENCE_CHECK_FAILED",
     dict(evidence_spans=[{"source_type": "run", "source_id": "r_fail", "metric_ref": "nse"}]), verify),
]


@pytest.mark.parametrize("family,code,over,step", CASES, ids=[f"{c[0]}:{c[1]}" for c in CASES])
def test_family_is_advisory_in_add_claim_and_blocks_promotion(session, family, code, over, step):
    resp = draft(**over)
    assert "promotion_check" in resp and resp.get("status") == "recorded", resp
    check = resp["promotion_check"]
    if step == "drop_basin_records":
        _drop_basin_records()
        check = update_claim_status(SID, "c1", "supported", "medium", RAT,
                                    uncertainty_verified=True)["promotion_check"]
    elif step is verify:
        # the later-ordered violation must still appear in add_claim's own advisory
        assert code in codes(check), check
        check = verify()["promotion_check"]
    assert code in codes(check), check
    hit = next(v for v in check if v["code"] == code)
    assert hit["family"] == family and hit["blocking"] is True
    assert first_blocking(check) == hit, "arrange the claim so the family under test blocks first"
    assert_promotion_refuses_with(hit)


def test_id_family_blocks_both_places(session):
    s = HydroSession.load(SID)
    s.claims["bad id!"] = {**draft_claim_dict(), "id": "bad id!"}
    s.save()
    check = verify("bad id!")["promotion_check"]
    hit = first_blocking(check)
    assert (hit["code"], hit["family"]) == ("INVALID_ID", "id")
    assert_promotion_refuses_with(hit, "bad id!")


def draft_claim_dict():
    draft("tmp")
    return copy.deepcopy(HydroSession.load(SID).claims["tmp"])


def test_clean_claim_has_empty_check_and_only_the_approval_step_remains(session):
    draft()
    assert verify()["promotion_check"] == []
    resp = promote_claim_to_registry(SID, "c1", researcher_approved=True)
    assert resp["code"] == "APPROVAL_REQUIRED"


def test_quality_warning_is_advisory_only(session):
    draft(evidence_spans=[{"source_type": "run", "source_id": "r_warn", "metric_ref": "nse"}])
    check = verify()["promotion_check"]
    assert codes(check) == ["EVIDENCE_QUALITY_WARNING"]
    assert check[0]["blocking"] is False and check[0]["family"] == "evidence"
    # a non-blocking violation never refuses: promotion proceeds to the human approval step
    assert promote_claim_to_registry(SID, "c1", researcher_approved=True)["code"] == "APPROVAL_REQUIRED"


def test_policy_function_is_pure_and_matches_response(session):
    draft()
    verify()
    s = HydroSession.load(SID)
    before = copy.deepcopy(s.claims)
    vs = promotion_violations(s, s.claims["c1"])
    assert [v.to_dict() for v in vs] == verify()["promotion_check"]
    assert s.claims == before   # the policy never mutates the session
    assert promotion_violations(s, HydroSession.load(SID).claims["c1"]) == vs
    assert registry.all_entries() == []


def test_update_claim_status_error_path_has_no_check(session):
    draft()
    resp = update_claim_status(SID, "c1", "supported", "medium", RAT)   # uncertainty gate error
    assert resp["error"] == "uncertainty_gate"
