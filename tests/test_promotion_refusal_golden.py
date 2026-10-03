"""Byte-identity of every production promotion refusal across the W1 policy refactor.

``fixtures/promotion_refusals_golden.json`` was captured by running these exact
scenarios against aihydro-tools main ``5bf155e`` (before ``promotion_violations``
existed). Each entry is the full ``promote_claim_to_registry`` response, minus the
diagnostic ``_traceback`` (it carries source line numbers). Error codes AND messages
must stay identical: the policy function may reorganise the checks, never reword or
reorder what an agent sees.

Deliberate change (promotion-errors): the checks that used to raise a bare ``ValueError``
(no evidence span, no limitation, status not eligible, uncertainty not verified, modelled
signature, claim not found) were pinned here as ``UNEXPECTED_ERROR``. That pin recorded a
defect, not a contract: they now carry their own stable codes, and every policy refusal
adds the full ``violations`` list. See CHANGELOG.

Regenerate only deliberately: ``GENERATE_PROMOTION_GOLDEN=1 pytest <this file>``.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from ai_hydro.mcp.tools_ledger import promote_claim_to_registry
from ai_hydro.registry import store as registry
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from approval_helpers import approve, basin_ref, retained

GOLDEN = Path(__file__).parent / "fixtures" / "promotion_refusals_golden.json"
SID, CID = "golden", "c1"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(registry, "REGISTRY_DIR", tmp_path / "registry")
    monkeypatch.setattr(registry, "CLAIMS_FILE", tmp_path / "registry" / "claims.jsonl")
    monkeypatch.setattr("ai_hydro.knowledge.embeddings.PASSAGE_INDEX_PATH", tmp_path / "passages.jsonl")


def base_result():
    return {"data": {"nse": 0.8, "series": [1.0, 2.0, 3.0],
                     "_uncertainty": {"nse": {"value": 0.8, "ci_low": 0.7, "ci_high": 0.9,
                                              "ci_level": 0.95, "n": 30, "method": "synthetic_fixture"}}},
            "quality_flags": [{"validator": "fixture_check", "status": "pass"}]}


def base_run():
    from ai_hydro.session.evidence import capture_result_evidence
    return {"run_id": "r1", "session_id": SID, "tool_name": "fixture",
            "key_outputs": {"nse": 0.8, "series_n": 3},
            "evidence": capture_result_evidence(base_result())}


def base_claim():
    return {"id": CID, "claim": "Synthetic evaluation NSE is 0.8",
            "claim_type": "empirical_result", "status": "supported",
            "confidence": "medium", "confidence_rationale": "Synthetic regression fixture only.",
            "scope": {"basins": ["synthetic"], "period": "2000-2001", "metric": "nse",
                      "basin_refs": [basin_ref("synthetic")]},
            "evidence_spans": [{"source_type": "run", "source_id": "r1", "metric_ref": "nse"}],
            "limitations": ["Synthetic regression case, no real research conclusion."],
            "uncertainty_verified": True, "basin_ref_records": retained("synthetic")}


def _claim(**kw):
    def f(c, run):
        c.update(kw)
    return f


def _scope(**kw):
    def f(c, run):
        c["scope"].update(kw)
    return f


def _run_evidence(path, value):
    def f(c, run):
        node = run["evidence"]
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value
    return f


def _del_run_evidence(*path):
    def f(c, run):
        node = run["evidence"]
        for part in path[:-1]:
            node = node[part]
        del node[path[-1]]
    return f


def _span(**kw):
    def f(c, run):
        c["evidence_spans"][0].update(kw)
    return f


# name -> (mutator(claim, run), approve_first, kwargs for promote)
SCENARIOS = {
    "ok_but_no_approval": (lambda c, r: None, False, {}),
    "not_requested": (lambda c, r: None, False, {"researcher_approved": False}),
    "basin_ref_required": (_scope(basin_refs=None), True, {}),
    "basin_ref_unknown": (lambda c, r: c.pop("basin_ref_records"), True, {}),
    "no_evidence_spans": (_claim(evidence_spans=[]), True, {}),
    "no_limitations": (_claim(limitations=[]), True, {}),
    "status_not_eligible": (_claim(status="proposed"), True, {}),
    "uncertainty_not_verified": (_claim(uncertainty_verified=False), True, {}),
    "modelled_signature_no_limitation": (_scope(metric="baseflow_index"), True, {}),
    "metric_ref_missing_on_metric_claim": (_span(metric_ref=None), True, {}),
    "metric_mismatch": (_scope(metric="kge"), True, {}),
    "evidence_unresolved": (_span(source_id="invented-run"), True, {}),
    "evidence_identity_mismatch": (lambda c, r: r.update(session_id="other-session"), True, {}),
    "evidence_check_failed": (_run_evidence(("quality_flags",), [{"validator": "v", "status": "fail"}]), True, {}),
    "evidence_metric_unavailable": (_run_evidence(("data", "nse"), float("nan")), True, {}),
    "evidence_uncertainty_unavailable": (_del_run_evidence("uncertainty"), True, {}),
    "evidence_uncertainty_invalid": (_run_evidence(("uncertainty", "nse", "ci_low"), 0.99), True, {}),
    "evidence_uncertainty_mismatch": (_run_evidence(("uncertainty", "nse", "value"), 0.7), True, {}),
}
# refusals decided before the claim is evaluated
PRE_CLAIM = {
    "invalid_session_id": ("bad id!", CID),
    "invalid_claim_id": (SID, "bad id!"),
    "claim_not_found": (SID, "missing"),
}


def _seed(mutator, approve_first):
    claim, run = copy.deepcopy(base_claim()), copy.deepcopy(base_run())
    mutator(claim, run)
    s = HydroSession(SID)
    s.claims[CID] = claim
    s.set("_run_log", {"r1": run})
    s.save()
    if approve_first:
        approve(SID, CID)


def _clean(response):
    response = {k: v for k, v in response.items() if k != "_traceback"}
    return json.loads(json.dumps(response, sort_keys=True, default=str))


def run_scenario(name):
    if name in PRE_CLAIM:
        _seed(lambda c, r: None, False)
        sid, cid = PRE_CLAIM[name]
        return _clean(promote_claim_to_registry(sid, cid, researcher_approved=True))
    mutator, approve_first, kwargs = SCENARIOS[name]
    _seed(mutator, approve_first)
    kwargs = {"researcher_approved": True, **kwargs}
    return _clean(promote_claim_to_registry(SID, CID, **kwargs))


ALL = list(SCENARIOS) + list(PRE_CLAIM)


def test_golden_generation_marker():
    if os.environ.get("GENERATE_PROMOTION_GOLDEN") != "1":
        pytest.skip("golden is only regenerated deliberately")


@pytest.mark.parametrize("name", ALL)
def test_refusal_is_byte_identical_to_main(name):
    actual = run_scenario(name)
    if os.environ.get("GENERATE_PROMOTION_GOLDEN") == "1":
        data = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        data[name] = actual
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        return
    golden = json.loads(GOLDEN.read_text())
    assert actual == golden[name]
    assert actual.get("error") is True, "every scenario here is a refusal"
