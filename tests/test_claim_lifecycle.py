"""Claim lifecycle across study and registry (R-03 retraction, R-07 stale reinstatement; mechanism M2).

Synthetic fixtures only; all IO under tmp_path. The human approval boundary is exercised, not mocked
away: only ``promote_claim_to_registry`` (with a human approval record) ever makes a registry row
``promoted``.
"""
from __future__ import annotations

import pytest

from ai_hydro.mcp.tools_ledger import (
    add_claim, check_registry_staleness, promote_claim_to_registry, update_claim_status,
)
from ai_hydro.mcp.tools_session import start_session
from ai_hydro.mcp.tools_skeptic import run_skeptic
from ai_hydro.registry import store as registry
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from approval_helpers import approve, basin_ref, basin_ref_full, retained

SID = "life"
RAT = "Synthetic lifecycle fixture rationale."


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(registry, "REGISTRY_DIR", tmp_path / "registry")
    monkeypatch.setattr(registry, "CLAIMS_FILE", tmp_path / "registry" / "claims.jsonl")
    monkeypatch.setattr("ai_hydro.knowledge.embeddings.PASSAGE_INDEX_PATH", tmp_path / "passages.jsonl")


def run_row(run_id="r1", nse=0.8, ci_low=0.7):
    from ai_hydro.session.evidence import capture_result_evidence
    res = {"data": {"nse": nse, "series": [1.0, 2.0, 3.0],
                    "_uncertainty": {"nse": {"value": nse, "ci_low": ci_low, "ci_high": 0.9, "ci_level": 0.95,
                                              "n": 30, "method": "synthetic_fixture"}}},
           "quality_flags": [{"validator": "fixture_check", "status": "pass"}]}
    return {"run_id": run_id, "session_id": SID, "tool_name": "fixture",
            "key_outputs": {"nse": nse, "series_n": 3}, "evidence": capture_result_evidence(res)}


def claim(spans=("r1",), status="supported"):
    return {"id": "c1", "claim": "Synthetic evaluation NSE is 0.8",
            "claim_type": "empirical_result", "status": status,
            "confidence": "medium", "confidence_rationale": "Synthetic regression fixture only.",
            "scope": {"basins": ["synthetic"], "period": "2000-2001", "metric": "nse",
                      "basin_refs": [basin_ref("synthetic")]},
            "evidence_spans": [{"source_type": "run", "source_id": s, "metric_ref": "nse"} for s in spans],
            "limitations": ["Synthetic regression case, no real research conclusion."],
            "uncertainty_verified": True, "basin_ref_records": retained("synthetic")}


@pytest.fixture
def promoted():
    s = HydroSession(SID)
    s.claims["c1"] = claim()
    s.set("_run_log", {"r1": run_row("r1")})
    s.save()
    approve(SID, "c1")
    res = promote_claim_to_registry(SID, "c1", researcher_approved=True)
    assert res["status"] == "promoted", res
    return res["registry_id"]


def rows():
    return {r["registry_id"]: r["status"] for r in registry.all_entries()}


def change_run(run_id="r1", **kw):
    s = HydroSession.load(SID)
    log = dict(s.get("_run_log") or {})
    log[run_id] = run_row(run_id, **kw)
    s.set("_run_log", log)
    s.save()


def add_run(run_id):
    s = HydroSession.load(SID)
    log = dict(s.get("_run_log") or {})
    log[run_id] = run_row(run_id)
    s.set("_run_log", log)
    s.save()


def stale_it():
    change_run("r1", nse=0.5)
    out = check_registry_staleness(SID)
    assert out["n_stale"] == 1, out
    assert HydroSession.load(SID).claims["c1"]["status"] == "stale"
    return out


# ------------------------------------------------------------------ R-03 retraction propagates

def test_retracting_a_promoted_claim_retracts_the_registry_entry(promoted):
    res = update_claim_status(SID, "c1", "retracted", "low", "Withdrawn: the source series was wrong.")
    assert res["status"] == "updated" and res["registry_retracted"] == [promoted]
    assert rows() == {promoted: "retracted"}
    entry, = registry.all_entries()
    assert entry["retraction_reason"].startswith("Withdrawn") and entry["retracted_at"]
    assert HydroSession.load(SID).claims["c1"]["status"] == "retracted"


def test_check_registry_staleness_reports_and_propagates_a_retraction_made_elsewhere(promoted):
    """A study retracted before this fix (or by hand) still leaves the registry `promoted`."""
    s = HydroSession.load(SID)
    s.claims["c1"]["status"] = "retracted"
    s.save()
    assert rows() == {promoted: "promoted"}
    out = check_registry_staleness(SID)
    assert out["retracted_claims"] == [{"claim_id": "c1", "registry_id": promoted}] and out["n_retracted"] == 1
    assert out["fresh_claims"] == [] and "All checked claims have current evidence" not in out["note"]
    assert rows() == {promoted: "retracted"}
    again = check_registry_staleness(SID)                       # idempotent; nothing left to propagate
    assert again["n_retracted"] == 0 and again["n_checked"] == 0


def test_add_claim_redefinition_keeps_the_registry_link_and_propagates_a_retraction(promoted):
    """R-03 second half: add_claim used to drop `promoted` and `registry_id`."""
    res = add_claim(SID, "c1", "Synthetic evaluation NSE is 0.8", "empirical_result", "retracted", "low",
                    "Withdrawn after review of the source.", ["synthetic"], "2000-2001", metric="nse",
                    evidence_spans=[{"source_type": "run", "source_id": "r1", "metric_ref": "nse"}])
    assert res["status"] == "recorded" and res["registry_retracted"] == [promoted]
    stored = HydroSession.load(SID).claims["c1"]
    assert stored["registry_id"] == promoted and stored["promoted"] is True and stored["status"] == "retracted"
    assert rows() == {promoted: "retracted"}


def test_add_claim_redefinition_without_a_retraction_keeps_the_link(promoted):
    res = add_claim(SID, "c1", "Synthetic evaluation NSE is 0.8 (reworded)", "empirical_result", "tested", "low",
                    "Reworded for clarity; evidence unchanged.", ["synthetic"], "2000-2001", metric="nse",
                    evidence_spans=[{"source_type": "run", "source_id": "r1", "metric_ref": "nse"}])
    assert res["status"] == "recorded"
    assert HydroSession.load(SID).claims["c1"]["registry_id"] == promoted
    assert rows() == {promoted: "promoted"}


def test_a_retracted_claim_is_not_set_back_to_supported(promoted):
    update_claim_status(SID, "c1", "retracted", "low", "Withdrawn: the source series was wrong.")
    res = update_claim_status(SID, "c1", "supported", "medium", "Reviewed again and it is fine after all.",
                              uncertainty_verified=True)
    assert res["error"] == "retracted_claim_gate"
    assert HydroSession.load(SID).claims["c1"]["status"] == "retracted" and rows() == {promoted: "retracted"}
    again = add_claim(SID, "c1", "Synthetic evaluation NSE is 0.8", "empirical_result", "weakly_supported", "low",
                      "Reinstated by redefinition.", ["synthetic"], "2000-2001",
                      evidence_spans=[{"source_type": "run", "source_id": "r1"}])
    assert again["error"] == "retracted_claim_gate"
    assert rows() == {promoted: "retracted"}


def test_a_retracted_study_claim_with_a_retracted_row_is_not_a_registry_conflict(promoted):
    update_claim_status(SID, "c1", "retracted", "low", "Withdrawn: the source series was wrong.")
    rep = run_skeptic(SID)
    assert not [i for i in rep.get("issues", []) if i["issue_type"] == "registry_conflict"], rep


def test_the_skeptic_flags_a_retraction_the_registry_has_not_seen(promoted):
    s = HydroSession.load(SID)
    s.claims["c1"]["status"] = "retracted"
    s.save()
    issues = [i for i in run_skeptic(SID)["issues"] if i["issue_type"] == "registry_conflict"]
    assert issues and "check_registry_staleness" in issues[0]["recommendation"]


# ------------------------------------------------------------------ R-07 stale is not reinstated by a sentence

def test_a_stale_claim_cannot_be_set_back_to_supported_by_a_sentence(promoted):
    stale_it()
    res = update_claim_status(SID, "c1", "supported", "medium", "Reviewed and it remains fully supported.",
                              uncertainty_verified=True)
    assert res["error"] == "stale_claim_gate" and res["still_cited"] == ["r1"] and res["stale_sources"] == ["r1"]
    assert "aihydro-approve" in res["teaching_error"]["how_to_fix"]
    assert HydroSession.load(SID).claims["c1"]["status"] == "stale"
    assert rows() == {promoted: "stale"}
    assert update_claim_status(SID, "c1", "weakly_supported", "low", "Still believed to hold.")["error"] == "stale_claim_gate"


def test_a_stale_claim_can_still_be_downgraded(promoted):
    stale_it()
    for st in ("tested", "inconclusive", "contradicted", "retracted"):
        assert update_claim_status(SID, "c1", st, "low", "Downgraded while the evidence is re-examined.")["status"] == "updated"


def test_redefining_a_stale_claim_on_the_same_evidence_is_refused(promoted):
    stale_it()
    res = add_claim(SID, "c1", "Synthetic evaluation NSE is 0.8", "empirical_result", "weakly_supported", "low",
                    "Redefined with the same run as before.", ["synthetic"], "2000-2001",
                    evidence_spans=[{"source_type": "run", "source_id": "r1", "metric_ref": "nse"}])
    assert res["error"] == "stale_claim_gate"
    assert HydroSession.load(SID).claims["c1"]["status"] == "stale"


def test_evidence_re_bound_to_a_new_run_can_be_asserted_and_then_needs_a_human_to_re_promote(promoted):
    stale_it()
    add_run("r2")
    res = add_claim(SID, "c1", "Synthetic evaluation NSE is 0.8", "empirical_result", "tested", "low",
                    "Re-run on fresh data; the earlier run changed.", ["synthetic"], "2000-2001", metric="nse",
                    evidence_spans=[{"source_type": "run", "source_id": "r2", "metric_ref": "nse"}],
                    limitations=["Synthetic regression case, no real research conclusion."],
                    basin_refs=[basin_ref_full("synthetic")])
    assert res["status"] == "recorded"
    up = update_claim_status(SID, "c1", "supported", "medium", "Supported by the re-run on fresh data.",
                             uncertainty_verified=True)
    assert up["status"] == "updated", up
    # the registry does not move without a human: still stale, and the conflict is visible
    assert rows() == {promoted: "stale"}
    out = check_registry_staleness(SID)
    assert [c["claim_id"] for c in out["status_conflicts"]] == ["c1"]
    assert out["status_conflicts"][0]["registry_status"] == "stale"
    flags = start_session(SID).get("claim_flags", [])
    assert any(f["code"] == "registry_conflict" and f["claim_id"] == "c1" for f in flags), flags
    # promotion needs the approval record; without it nothing changes
    refused = promote_claim_to_registry(SID, "c1", researcher_approved=True)
    assert refused.get("code") == "APPROVAL_REQUIRED"
    assert rows() == {promoted: "stale"}
    approve(SID, "c1")
    ok = promote_claim_to_registry(SID, "c1", researcher_approved=True)
    assert ok["status"] == "promoted"
    assert sorted(rows().values()) == ["promoted", "stale"]
    assert check_registry_staleness(SID)["status_conflicts"] == []
    assert update_claim_status(SID, "c1", "supported", "medium", "Re-confirmed after the re-promotion.",
                               uncertainty_verified=True)["status"] == "updated"


def test_citing_the_changed_run_next_to_a_new_one_is_still_refused(promoted):
    stale_it()
    add_run("r2")
    res = add_claim(SID, "c1", "Synthetic evaluation NSE is 0.8", "empirical_result", "weakly_supported", "low",
                    "Cites both the old and the new run.", ["synthetic"], "2000-2001",
                    evidence_spans=[{"source_type": "run", "source_id": "r1"}, {"source_type": "run", "source_id": "r2"}])
    assert res["error"] == "stale_claim_gate" and res["still_cited"] == ["r1"]


def test_skeptic_compares_the_study_status_with_the_registry_row(promoted):
    """R-07 probe: after reinstatement the skeptic said clean. A pre-fix reinstated state is still reported."""
    stale_it()
    s = HydroSession.load(SID)
    s.claims["c1"]["status"] = "supported"          # the state older builds allowed
    s.save()
    issues = [i for i in run_skeptic(SID)["issues"] if i["issue_type"] == "registry_conflict"]
    assert issues and issues[0]["claim_id"] == "c1"
    assert check_registry_staleness(SID)["status_conflicts"][0]["registry_status"] == "stale"
    assert any(f["code"] == "registry_conflict" for f in start_session(SID).get("claim_flags", []))


def test_a_claim_that_was_never_promoted_is_unaffected(promoted):
    s = HydroSession("plain")
    s.set("_run_log", {"r1": run_row("r1")})
    s.save()
    r = add_claim("plain", "p1", "Synthetic evaluation NSE is 0.8", "empirical_result", "weakly_supported", "low",
                  "No registry involved.", ["synthetic"], "2000-2001",
                  evidence_spans=[{"source_type": "run", "source_id": "r1"}])
    assert r["status"] == "recorded"
    assert update_claim_status("plain", "p1", "retracted", "low", "Withdrawn before ever being promoted.") \
        .get("registry_retracted") is None
