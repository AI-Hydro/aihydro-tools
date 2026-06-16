"""
Unit tests for Phase 1.7 — Pre-registration.

Covers:
  - register_research_plan tool (lock, immutability, content_hash, teaching error)
  - add_claim prereg_id parameter (stored on claim dict)
  - Section 7 of build_defensibility_report (plan present / absent / partial)
  - Confirmatory vs exploratory labeling in Section 4 Claims Register
  - Summary dict new keys

All tests are fixture-only: no network, sessions redirected to tmp_path.
"""
from __future__ import annotations

import pytest

from ai_hydro.reports.defensibility import build_defensibility_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(
    session_id: str,
    monkeypatch,
    sessions_dir,
    *,
    claims: dict | None = None,
    run_log: dict | None = None,
    research_plan: dict | None = None,
):
    import ai_hydro.session.store as _store
    from ai_hydro.session.store import HydroSession

    monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

    session = HydroSession(session_id)
    if claims:
        session.claims = claims
    if run_log:
        session.set("_run_log", run_log)
    if research_plan:
        session.set("_research_plan", research_plan)
    session.save()
    return session


_LOCKED_PLAN = {
    "prereg_id": "prereg.testses1.20260610.ab12cd",
    "hypothesis": "Baseflow index at the test site exceeds 0.5.",
    "planned_analyses": ["extract_hydrological_signatures", "compute_baseflow"],
    "content_hash": "ab12cd34ef56gh78",
    "locked_at": "2026-06-10T00:00:00+00:00",
    "locked": True,
}

_CONFIRMATORY_CLAIM = {
    "id": "claim-001",
    "claim": "Baseflow index is 0.62, confirming the pre-registered hypothesis.",
    "claim_type": "empirical_result",
    "status": "supported",
    "confidence": "high",
    "confidence_rationale": "Bootstrap 90% CI [0.58, 0.67] does not overlap 0.5.",
    "scope": {"basins": ["01031500"], "period": "1990-2020"},
    "evidence_spans": [{"source_type": "run", "source_id": "sigs.run.001", "metric_ref": "baseflow_index"}],
    "limitations": ["Single gauge."],
    "prereg_id": "prereg.testses1.20260610.ab12cd",
}

_EXPLORATORY_CLAIM = {
    "id": "claim-002",
    "claim": "Flow variability is unusually high for a humid basin.",
    "claim_type": "empirical_result",
    "status": "proposed",
    "confidence": "low",
    "confidence_rationale": "Post-hoc observation, no pre-registered threshold.",
    "scope": {"basins": ["01031500"], "period": "1990-2020"},
    "evidence_spans": [],
    "limitations": ["Single gauge."],
}


# ---------------------------------------------------------------------------
# TestRegisterResearchPlan
# ---------------------------------------------------------------------------

class TestRegisterResearchPlan:
    def test_locks_plan_and_returns_prereg_id(self, monkeypatch, tmp_path):
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession
        from ai_hydro.mcp.tools_ledger import register_research_plan

        monkeypatch.setattr(_store, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", tmp_path)
        session = HydroSession("test-prereg-001")
        session.save()

        result = register_research_plan(
            session_id="test-prereg-001",
            hypothesis="Baseflow index exceeds 0.5.",
            planned_analyses=["extract_hydrological_signatures"],
        )

        assert "error" not in result
        assert result["prereg_id"].startswith("prereg.")
        assert result["content_hash"]
        assert result["locked_at"]
        assert result["n_planned"] == 1

    def test_plan_stored_in_session(self, monkeypatch, tmp_path):
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession
        from ai_hydro.mcp.tools_ledger import register_research_plan

        monkeypatch.setattr(_store, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", tmp_path)
        HydroSession("test-prereg-002").save()

        register_research_plan(
            session_id="test-prereg-002",
            hypothesis="KGE > 0.6 for calibrated model.",
            planned_analyses=["calibrate_model", "extract_hydrological_signatures"],
        )

        session = HydroSession.load("test-prereg-002")
        plan = session.get("_research_plan")
        assert plan is not None
        assert plan["locked"] is True
        assert plan["hypothesis"] == "KGE > 0.6 for calibrated model."
        assert len(plan["planned_analyses"]) == 2
        assert plan["content_hash"]

    def test_content_hash_deterministic(self, monkeypatch, tmp_path):
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession
        from ai_hydro.mcp.tools_ledger import register_research_plan
        from aihydro_core.primitives.hashing import content_hash

        monkeypatch.setattr(_store, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", tmp_path)
        HydroSession("test-prereg-003").save()

        result = register_research_plan(
            session_id="test-prereg-003",
            hypothesis="Test hypothesis.",
            planned_analyses=["analysis_a", "analysis_b"],
        )

        expected_hash = content_hash({
            "hypothesis": "Test hypothesis.",
            "planned_analyses": ["analysis_a", "analysis_b"],
        })
        assert result["content_hash"] == expected_hash

    def test_second_call_returns_teaching_error(self, monkeypatch, tmp_path):
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession
        from ai_hydro.mcp.tools_ledger import register_research_plan

        monkeypatch.setattr(_store, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", tmp_path)
        HydroSession("test-prereg-004").save()

        register_research_plan(
            session_id="test-prereg-004",
            hypothesis="Original hypothesis.",
            planned_analyses=["analysis_a"],
        )
        result2 = register_research_plan(
            session_id="test-prereg-004",
            hypothesis="Replacement hypothesis — should be blocked.",
            planned_analyses=["analysis_b"],
        )

        assert result2["error"] == "plan_already_locked"
        assert "teaching_error" in result2
        assert result2["teaching_error"]["rule"] == "research_plan_immutable_after_lock"

    def test_second_call_preserves_original_plan(self, monkeypatch, tmp_path):
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession
        from ai_hydro.mcp.tools_ledger import register_research_plan

        monkeypatch.setattr(_store, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", tmp_path)
        HydroSession("test-prereg-005").save()

        r1 = register_research_plan(
            session_id="test-prereg-005",
            hypothesis="Original hypothesis.",
            planned_analyses=["analysis_a"],
        )
        register_research_plan(
            session_id="test-prereg-005",
            hypothesis="Replacement — must not overwrite.",
            planned_analyses=["analysis_b"],
        )

        session = HydroSession.load("test-prereg-005")
        plan = session.get("_research_plan")
        assert plan["hypothesis"] == "Original hypothesis."
        assert plan["prereg_id"] == r1["prereg_id"]

    def test_empty_hypothesis_error(self, monkeypatch, tmp_path):
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession
        from ai_hydro.mcp.tools_ledger import register_research_plan

        monkeypatch.setattr(_store, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", tmp_path)
        HydroSession("test-prereg-006").save()

        result = register_research_plan(
            session_id="test-prereg-006",
            hypothesis="   ",
            planned_analyses=["analysis_a"],
        )
        assert "error" in result

    def test_empty_planned_analyses_error(self, monkeypatch, tmp_path):
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession
        from ai_hydro.mcp.tools_ledger import register_research_plan

        monkeypatch.setattr(_store, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", tmp_path)
        HydroSession("test-prereg-007").save()

        result = register_research_plan(
            session_id="test-prereg-007",
            hypothesis="Valid hypothesis.",
            planned_analyses=[],
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# TestAddClaimPreregId
# ---------------------------------------------------------------------------

class TestAddClaimPreregId:
    def test_prereg_id_stored_on_claim(self, monkeypatch, tmp_path):
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession
        from ai_hydro.mcp.tools_ledger import add_claim

        monkeypatch.setattr(_store, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", tmp_path)
        HydroSession("test-claim-prereg-001").save()

        result = add_claim(
            session_id="test-claim-prereg-001",
            claim_id="claim-001",
            statement="Baseflow index is 0.62.",
            claim_type="empirical_result",
            status="proposed",
            confidence="medium",
            confidence_rationale="Based on bootstrap CI from signature analysis.",
            basins=["01031500"],
            period="1990-2020",
            prereg_id="prereg.testtest.20260610.ab12",
        )
        assert result["status"] == "recorded"

        session = HydroSession.load("test-claim-prereg-001")
        assert session.claims["claim-001"]["prereg_id"] == "prereg.testtest.20260610.ab12"

    def test_no_prereg_id_omitted(self, monkeypatch, tmp_path):
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession
        from ai_hydro.mcp.tools_ledger import add_claim

        monkeypatch.setattr(_store, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", tmp_path)
        HydroSession("test-claim-prereg-002").save()

        add_claim(
            session_id="test-claim-prereg-002",
            claim_id="claim-001",
            statement="Exploratory observation.",
            claim_type="empirical_result",
            status="proposed",
            confidence="low",
            confidence_rationale="Post-hoc, unplanned observation from the data.",
            basins=["01031500"],
            period="1990-2020",
        )

        session = HydroSession.load("test-claim-prereg-002")
        assert "prereg_id" not in session.claims["claim-001"]


# ---------------------------------------------------------------------------
# TestPreregSection (Section 7)
# ---------------------------------------------------------------------------

class TestPreregSection:
    def test_section_7_header_present(self, monkeypatch, tmp_path):
        session = _make_session("test-rep-prereg-001", monkeypatch, tmp_path)
        md, _ = build_defensibility_report(session, "test-rep-prereg-001", "2026-06-10")
        assert "## 7. Pre-registration Plan" in md

    def test_no_plan_shows_exploratory_note(self, monkeypatch, tmp_path):
        session = _make_session("test-rep-prereg-002", monkeypatch, tmp_path)
        md, summary = build_defensibility_report(session, "test-rep-prereg-002", "2026-06-10")
        assert "No pre-registration found" in md
        assert "exploratory" in md
        assert summary["prereg_present"] is False

    def test_plan_present_shows_hypothesis(self, monkeypatch, tmp_path):
        session = _make_session(
            "test-rep-prereg-003", monkeypatch, tmp_path,
            research_plan=_LOCKED_PLAN,
        )
        md, summary = build_defensibility_report(session, "test-rep-prereg-003", "2026-06-10")
        assert "Baseflow index at the test site exceeds 0.5." in md
        assert summary["prereg_present"] is True
        assert summary["n_planned_analyses"] == 2

    def test_plan_shows_content_hash(self, monkeypatch, tmp_path):
        session = _make_session(
            "test-rep-prereg-004", monkeypatch, tmp_path,
            research_plan=_LOCKED_PLAN,
        )
        md, _ = build_defensibility_report(session, "test-rep-prereg-004", "2026-06-10")
        assert _LOCKED_PLAN["content_hash"] in md

    def test_plan_shows_locked_at(self, monkeypatch, tmp_path):
        session = _make_session(
            "test-rep-prereg-005", monkeypatch, tmp_path,
            research_plan=_LOCKED_PLAN,
        )
        md, _ = build_defensibility_report(session, "test-rep-prereg-005", "2026-06-10")
        assert "2026-06-10" in md

    def test_planned_analyses_listed(self, monkeypatch, tmp_path):
        session = _make_session(
            "test-rep-prereg-006", monkeypatch, tmp_path,
            research_plan=_LOCKED_PLAN,
        )
        md, _ = build_defensibility_report(session, "test-rep-prereg-006", "2026-06-10")
        assert "extract_hydrological_signatures" in md
        assert "compute_baseflow" in md


# ---------------------------------------------------------------------------
# TestConfirmatoryVsExploratory
# ---------------------------------------------------------------------------

class TestConfirmatoryVsExploratory:
    def test_confirmatory_claim_labeled(self, monkeypatch, tmp_path):
        session = _make_session(
            "test-rep-conf-001", monkeypatch, tmp_path,
            claims={"claim-001": _CONFIRMATORY_CLAIM},
            research_plan=_LOCKED_PLAN,
        )
        md, summary = build_defensibility_report(session, "test-rep-conf-001", "2026-06-10")
        assert "confirmatory" in md
        assert summary["n_confirmatory_claims"] == 1

    def test_exploratory_claim_labeled(self, monkeypatch, tmp_path):
        session = _make_session(
            "test-rep-conf-002", monkeypatch, tmp_path,
            claims={"claim-002": _EXPLORATORY_CLAIM},
        )
        md, summary = build_defensibility_report(session, "test-rep-conf-002", "2026-06-10")
        assert "exploratory" in md
        assert summary["n_confirmatory_claims"] == 0

    def test_mixed_claims_correct_counts(self, monkeypatch, tmp_path):
        session = _make_session(
            "test-rep-conf-003", monkeypatch, tmp_path,
            claims={
                "claim-001": _CONFIRMATORY_CLAIM,
                "claim-002": _EXPLORATORY_CLAIM,
            },
            research_plan=_LOCKED_PLAN,
        )
        md, summary = build_defensibility_report(session, "test-rep-conf-003", "2026-06-10")
        assert summary["n_confirmatory_claims"] == 1
        assert summary["n_claims"] == 2
        assert "1 confirmatory" in md
        assert "1 exploratory" in md

    def test_no_confirmatory_shows_guidance(self, monkeypatch, tmp_path):
        session = _make_session(
            "test-rep-conf-004", monkeypatch, tmp_path,
            research_plan=_LOCKED_PLAN,
        )
        md, _ = build_defensibility_report(session, "test-rep-conf-004", "2026-06-10")
        assert "prereg_id=" in md


# ---------------------------------------------------------------------------
# TestSummaryDictNewKeys
# ---------------------------------------------------------------------------

class TestSummaryDictNewKeys:
    def test_all_new_keys_present_without_plan(self, monkeypatch, tmp_path):
        session = _make_session("test-summ-001", monkeypatch, tmp_path)
        _, summary = build_defensibility_report(session, "test-summ-001", "2026-06-10")
        assert "prereg_present" in summary
        assert "n_confirmatory_claims" in summary
        assert "n_planned_analyses" in summary
        assert "n_executed_planned" in summary

    def test_defaults_without_plan(self, monkeypatch, tmp_path):
        session = _make_session("test-summ-002", monkeypatch, tmp_path)
        _, summary = build_defensibility_report(session, "test-summ-002", "2026-06-10")
        assert summary["prereg_present"] is False
        assert summary["n_confirmatory_claims"] == 0
        assert summary["n_planned_analyses"] == 0
        assert summary["n_executed_planned"] == 0

    def test_executed_planned_counted_from_run_log(self, monkeypatch, tmp_path):
        run_log = {
            "sigs.20260610.bench.ab12": {
                "tool_name": "extract_hydrological_signatures",
                "timestamp": "2026-06-10T00:00:00+00:00",
                "key_outputs": {"q_mean": 1.25},
            }
        }
        session = _make_session(
            "test-summ-003", monkeypatch, tmp_path,
            run_log=run_log,
            research_plan=_LOCKED_PLAN,
        )
        _, summary = build_defensibility_report(session, "test-summ-003", "2026-06-10")
        assert summary["n_executed_planned"] >= 1
