"""Promotion refusals carry their own stable code (never UNEXPECTED_ERROR / a traceback), and the
uncertainty gate on claim status does not depend on which tool sets the status."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import Client

import ai_hydro.mcp  # noqa: F401  (registers every tool)
from ai_hydro.mcp import app
from ai_hydro.mcp import eval_condition as ec
from ai_hydro.mcp.tools_ledger import add_claim, promote_claim_to_registry, update_claim_status
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from test_promotion_refusal_golden import (  # noqa: F401  (isolated: autouse fixture)
    CID, SCENARIOS, SID, _claim, _seed, isolated,
)

EXPECTED = {
    "no_evidence_spans": "EVIDENCE_REQUIRED",
    "no_limitations": "LIMITATIONS_REQUIRED",
    "status_not_eligible": "STATUS_NOT_ELIGIBLE",
    "uncertainty_not_verified": "UNCERTAINTY_NOT_VERIFIED",
    "modelled_signature_no_limitation": "MODELLED_LIMITATION_REQUIRED",
}


@pytest.mark.parametrize("name,code", sorted(EXPECTED.items()))
def test_policy_refusals_have_their_own_code_and_no_traceback(name, code):
    mutator, approve_first, _ = SCENARIOS[name]
    _seed(mutator, approve_first)
    res = promote_claim_to_registry(SID, CID, researcher_approved=True)
    assert res["error"] is True and res["code"] == code
    assert "_traceback" not in res and res["recovery"] and "unexpected internal" not in res["recovery"]
    assert code in [v["code"] for v in res["violations"]]
    assert res["violations"][0]["blocking"] is True


def test_every_violation_is_listed_and_first_blocking_is_the_code():
    _seed(lambda c, r: c.update(status="proposed", limitations=[]), True)
    res = promote_claim_to_registry(SID, CID, researcher_approved=True)
    codes = [v["code"] for v in res["violations"]]
    assert res["code"] == "LIMITATIONS_REQUIRED"          # first in policy order
    assert "STATUS_NOT_ELIGIBLE" in codes and codes.index("LIMITATIONS_REQUIRED") < codes.index("STATUS_NOT_ELIGIBLE")


def test_claim_not_found_is_structured():
    _seed(lambda c, r: None, False)
    res = promote_claim_to_registry(SID, "missing", researcher_approved=True)
    assert res["code"] == "CLAIM_NOT_FOUND" and "_traceback" not in res


# ---------------------------------------------------------------- D2: one gate, every status path

def _new(status, **kw):
    args = dict(session_id=SID, claim_id="m1", statement="NSE is 0.8", claim_type="empirical_result",
                status=status, confidence="medium", confidence_rationale="Synthetic rationale long enough to pass.", basins=[], period="2000",
                metric="nse")
    args.update(kw)
    return add_claim(**args)


def test_add_claim_supported_metric_claim_hits_the_uncertainty_gate():
    HydroSession(SID).save()
    res = _new("supported")
    assert res["error"] == "uncertainty_gate" and res["requested_status"] == "supported"
    assert "m1" not in HydroSession.load(SID).claims            # nothing stored
    assert _new("proposed")["status"] == "recorded"
    ok = update_claim_status(SID, "m1", "supported", "medium", "bootstrap confidence interval checked", uncertainty_verified=True)
    assert ok["status"] == "updated"


def test_add_claim_and_update_claim_status_apply_the_same_rule():
    HydroSession(SID).save()
    _new("proposed")
    assert update_claim_status(SID, "m1", "supported", "medium", "r sufficiently long rationale text")["error"] == "uncertainty_gate"
    a = _new("supported", claim_id="m2")
    b = update_claim_status(SID, "m1", "supported", "medium", "r sufficiently long rationale text")
    assert {k: v for k, v in a.items() if k != "claim_id"} == {k: v for k, v in b.items() if k != "claim_id"}


def test_redefining_an_existing_id_as_supported_is_gated_and_leaves_it_unchanged():
    HydroSession(SID).save()
    _new("proposed")
    update_claim_status(SID, "m1", "weakly_supported", "low", "r sufficiently long rationale text")
    res = _new("supported")
    assert res["error"] == "uncertainty_gate"
    assert HydroSession.load(SID).claims["m1"]["status"] == "weakly_supported"


def test_non_metric_or_non_supported_claims_are_unaffected():
    HydroSession(SID).save()
    assert _new("supported", claim_id="plain", metric=None)["status"] == "recorded"
    assert _new("weakly_supported", claim_id="weak")["status"] == "recorded"
    assert _new("proposed", claim_id="prop")["status"] == "recorded"


def test_existing_supported_claim_without_uncertainty_is_not_rewritten_but_flagged_and_refused():
    mutator, _, _ = SCENARIOS["uncertainty_not_verified"]
    _seed(mutator, True)                                        # stored supported, flag unset
    stored = HydroSession.load(SID).claims[CID]
    assert stored["status"] == "supported" and not stored.get("uncertainty_verified")
    from ai_hydro.claims.promotion_policy import promotion_check
    flagged = promotion_check(HydroSession.load(SID), stored, claim_id=CID)
    assert "UNCERTAINTY_NOT_VERIFIED" in [v["code"] for v in flagged]
    assert promote_claim_to_registry(SID, CID, researcher_approved=True)["code"] == "UNCERTAINTY_NOT_VERIFIED"
    assert HydroSession.load(SID).claims[CID]["status"] == "supported"   # nothing rewritten


@pytest.mark.parametrize("condition", ["C1", "C2", "C3"])
def test_gate_is_arm_invariant(tmp_path, monkeypatch, condition):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AIHYDRO_HOME", str(home))
    nonce = "n-gate"
    (home / ec.MARKER_FILE).write_text(json.dumps(
        {"schema": ec.MARKER_SCHEMA, "condition": condition, "nonce": nonce}))
    monkeypatch.setenv(ec.NONCE_ENV, nonce)
    HydroSession(SID).save()
    meta = {"aihydro/context": {"client": ec.expected_client_label(condition, nonce), "study_id": SID}}
    args = dict(session_id=SID, claim_id="m1", statement="s", claim_type="empirical_result",
                status="supported", confidence="medium", confidence_rationale="Synthetic rationale long enough to pass.", basins=[],
                period="2000", metric="nse")

    async def run():
        async with Client(app.mcp) as c:
            return await c.call_tool("add_claim", args, meta=meta, raise_on_error=False)
    res = asyncio.run(run())
    assert not res.is_error and res.structured_content["error"] == "uncertainty_gate"
