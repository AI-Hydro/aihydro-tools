"""Pre-registration integrity (R-04): "confirmatory" must be earned, not typed.

Probe from the audit: ``add_claim(prereg_id="prereg.never.existed")`` was stored and the report printed it as
confirmatory; a plan locked AFTER the run still counted its analysis as executed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai_hydro.mcp.tools_ledger import add_claim, register_research_plan
from ai_hydro.reports.defensibility import build_defensibility_report
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession

SID = "pre"
RAT = "Synthetic pre-registration fixture rationale."


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)


def ts(delta_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()


def seed(*runs):
    """runs: (run_id, tool_name, iso timestamp)"""
    s = HydroSession.load(SID) if HydroSession.exists(SID) else HydroSession(SID)
    s.set("_run_log", {rid: {"run_id": rid, "session_id": SID, "tool_name": tool, "timestamp": t,
                              "key_outputs": {"baseflow_index": 0.4}} for rid, tool, t in runs})
    s.save()


def claim(**over):
    args = dict(session_id=SID, claim_id="c_pre", statement="Baseflow index exceeds 0.4.",
                claim_type="empirical_result", status="proposed", confidence="low", confidence_rationale=RAT,
                basins=["01013500"], period="2000-2010")
    args.update(over)
    return add_claim(**args)


def report():
    md, summary = build_defensibility_report(HydroSession.load(SID), SID, "2026-10-30")
    return md, summary


def test_a_prereg_id_that_names_no_plan_is_refused():
    HydroSession(SID).save()
    res = claim(prereg_id="prereg.never.existed")
    assert res["error"] == "prereg_gate" and res["study_plan_prereg_id"] is None
    assert "register_research_plan" in res["teaching_error"]["how_to_fix"]
    assert "c_pre" not in HydroSession.load(SID).claims


def test_a_prereg_id_of_another_plan_is_refused_and_names_the_real_one():
    HydroSession(SID).save()
    plan = register_research_plan(SID, "BFI exceeds 0.4", ["extract_hydrological_signatures"])
    res = claim(prereg_id="prereg.typo.20261030.e8db5a")
    assert res["error"] == "prereg_gate" and res["study_plan_prereg_id"] == plan["prereg_id"]
    assert plan["prereg_id"] in res["teaching_error"]["how_to_fix"]


def test_the_studys_own_prereg_id_is_confirmatory_when_nothing_ran_first():
    HydroSession(SID).save()
    plan = register_research_plan(SID, "BFI exceeds 0.4", ["extract_hydrological_signatures"])
    assert plan["post_hoc"] is False and plan["planned_ran_before_lock"] == []
    res = claim(prereg_id=plan["prereg_id"])
    assert res["status"] == "recorded" and res["prereg"] == "confirmatory"
    md, summary = report()
    assert "✅ confirmatory" in md and summary["n_confirmatory_claims"] == 1


def test_a_claim_without_a_prereg_id_is_unchanged():
    HydroSession(SID).save()
    assert claim()["status"] == "recorded"
    assert report()[1]["n_confirmatory_claims"] == 0


def test_a_plan_locked_after_its_analysis_ran_is_post_hoc_and_does_not_count_as_planned():
    HydroSession(SID).save()
    seed(("r1", "extract_hydrological_signatures", ts(-60)))          # the run predates the lock
    plan = register_research_plan(SID, "BFI exceeds 0.4", ["extract_hydrological_signatures", "compute_flood_frequency"])
    assert plan["post_hoc"] is True and plan["planned_ran_before_lock"] == ["extract_hydrological_signatures"]
    assert "AFTER" in plan["note"]
    res = claim(prereg_id=plan["prereg_id"])
    assert res["status"] == "recorded" and res["prereg"] == "post_hoc_plan"
    assert res["warnings"][0]["code"] == "PREREG_POST_HOC"
    md, summary = report()
    assert "Post-hoc plan" in md and "ran before the lock (not counted)" in md
    assert summary["n_executed_planned"] == 0 and summary["n_confirmatory_claims"] == 0
    assert "✅ confirmatory" not in md and "plan locked after its analyses ran" in md


def test_unrelated_setup_runs_before_the_lock_do_not_make_a_plan_post_hoc():
    HydroSession(SID).save()
    seed(("r0", "delineate_watershed", ts(-120)), ("r1", "fetch_streamflow_data", ts(-90)))
    plan = register_research_plan(SID, "BFI exceeds 0.4", ["extract_hydrological_signatures"])
    assert plan["post_hoc"] is False


def test_only_runs_after_the_lock_count_as_executed_and_names_match_exactly():
    HydroSession(SID).save()
    plan = register_research_plan(SID, "BFI exceeds 0.4",
                                  ["extract_hydrological_signatures", "flood", "Compute Flood Frequency"])
    seed(("r1", "extract_hydrological_signatures", ts(+5)),
         ("r2", "compute_flood_frequency_analysis_v2", ts(+6)))      # contains "flood" as a substring only
    md, summary = report()
    # exact match only: the signature tool ran after the lock; "flood" matches nothing; the description does not
    # match the longer tool name either
    assert summary["n_planned_analyses"] == 3 and summary["n_executed_planned"] == 1, md
    seed(("r1", "extract_hydrological_signatures", ts(+5)), ("r3", "compute_flood_frequency", ts(+7)))
    assert report()[1]["n_executed_planned"] == 2                    # "Compute Flood Frequency" -> exact tool name


def test_an_unverified_prereg_id_already_on_disk_is_shown_as_exploratory():
    """Sessions saved before the check keep working; the report no longer prints their label as confirmatory."""
    s = HydroSession(SID)
    s.claims["old"] = {"id": "old", "claim": "x", "claim_type": "empirical_result", "status": "proposed",
                       "confidence": "low", "confidence_rationale": RAT, "scope": {"basins": [], "period": "p"},
                       "evidence_spans": [], "limitations": [], "prereg_id": "prereg.never.existed"}
    s.save()
    md, summary = report()
    assert summary["n_confirmatory_claims"] == 0 and "prereg_id names no locked plan" in md


def test_a_plan_on_disk_without_the_stamp_is_judged_from_its_timestamps():
    s = HydroSession(SID)
    s.set("_research_plan", {"prereg_id": "prereg.pre.20261030.abcdef", "hypothesis": "h",
                              "planned_analyses": ["extract_hydrological_signatures"], "content_hash": "x",
                              "locked_at": ts(0), "locked": True})
    s.save()
    seed(("r1", "extract_hydrological_signatures", ts(-30)))
    md, summary = report()
    assert "Post-hoc plan" in md and summary["n_executed_planned"] == 0


# ---- a free-text plan cannot hide that the analysis already ran (review B1)

SPAN_R1 = [{"source_type": "run", "source_id": "r1", "metric_ref": "baseflow_index"}]


def test_a_free_text_plan_locked_after_the_run_is_not_confirmatory_for_a_claim_citing_that_run():
    HydroSession(SID).save()
    seed(("r1", "separate_baseflow", ts(-60)))                       # ran before the plan
    plan = register_research_plan(SID, "BFI exceeds 0.4", ["baseflow analysis"])
    assert plan["post_hoc"] is False                                 # name matching cannot see it ...
    assert plan["planned_unmatched"] == ["baseflow analysis"]        # ... and the plan says so
    assert "Unmatched" in plan["note"]
    res = claim(prereg_id=plan["prereg_id"], evidence_spans=SPAN_R1)
    assert res["status"] == "recorded" and res["prereg"] == "post_hoc_plan"
    w = [x for x in res["warnings"] if x["code"] == "PREREG_POST_HOC"]
    assert w and "r1" in w[0]["message"]
    md, summary = report()
    assert summary["n_confirmatory_claims"] == 0 and "✅ confirmatory" not in md
    assert "unmatched" in md and "not yet run" not in md


def test_the_same_plan_is_confirmatory_for_a_claim_citing_a_run_made_after_the_lock():
    HydroSession(SID).save()
    seed(("r1", "separate_baseflow", ts(-60)))
    plan = register_research_plan(SID, "BFI exceeds 0.4", ["baseflow analysis"])
    seed(("r1", "separate_baseflow", ts(-60)), ("r2", "separate_baseflow", ts(+5)))
    res = claim(prereg_id=plan["prereg_id"],
                evidence_spans=[{"source_type": "run", "source_id": "r2", "metric_ref": "baseflow_index"}])
    assert res["prereg"] == "confirmatory" and "warnings" not in res
    assert report()[1]["n_confirmatory_claims"] == 1
    # citing both the old and the new run is not confirmatory
    both = claim(claim_id="c2", prereg_id=plan["prereg_id"], evidence_spans=SPAN_R1 + [
        {"source_type": "run", "source_id": "r2", "metric_ref": "baseflow_index"}])
    assert both["prereg"] == "post_hoc_plan"
