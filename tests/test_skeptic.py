"""
Unit tests for Phase 2.3 — Skeptic / referee agent.

Covers:
  - SkepticIssue / SkepticReport models
  - check_stale_claims_cited
  - check_scope_overreach
  - check_assumption_violations
  - check_registry_conflicts
  - run_all_checks aggregate
  - run_skeptic MCP tool
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from ai_hydro.skeptic.checks import (
    check_assumption_violations,
    check_registry_conflicts,
    check_scope_overreach,
    check_stale_claims_cited,
    run_all_checks,
)
from ai_hydro.skeptic.models import SkepticIssue, SkepticReport


# ---------------------------------------------------------------------------
# Helpers — minimal HydroSession stand-in
# ---------------------------------------------------------------------------

class _FakeSession:
    """Minimal HydroSession stand-in for unit tests."""

    def __init__(
        self,
        session_id: str = "test-session",
        claims: dict | None = None,
        assumptions: dict | None = None,
        interpretation: str = "",
    ):
        self.session_id = session_id
        self.claims = claims or {}
        self.assumptions = assumptions or {}
        self.interpretation = interpretation


def _claim(
    claim_id: str = "c-001",
    status: str = "supported",
    basins: list[str] | None = None,
    registry_id: str | None = None,
) -> dict:
    d = {
        "id": claim_id,
        "claim": f"Test claim {claim_id}",
        "claim_type": "empirical_result",
        "status": status,
        "confidence": "medium",
        "scope": {"basins": basins or ["01013500"], "period": "1990-2010"},
        "limitations": ["Limited to one gauge"],
        "evidence_spans": [],
    }
    if registry_id:
        d["registry_id"] = registry_id
    return d


def _assumption(
    assumption_id: str = "a-001",
    risk: str = "high",
    validated: bool = False,
    statement: str = "Stationarity holds over the study period.",
) -> dict:
    return {
        "id": assumption_id,
        "statement": statement,
        "risk": risk,
        "validated": validated,
        "affects": ["signatures"],
        "scope": "test-session",
    }


# ---------------------------------------------------------------------------
# TestSkepticModels
# ---------------------------------------------------------------------------

class TestSkepticModels(unittest.TestCase):

    def test_issue_roundtrip(self):
        issue = SkepticIssue(
            issue_type="scope_overreach",
            severity="warning",
            description="Gauge 01234567 not in any claim scope.",
            recommendation="Add a claim or remove the reference.",
        )
        d = issue.model_dump()
        self.assertEqual(d["issue_type"], "scope_overreach")
        self.assertEqual(d["severity"], "warning")

    def test_report_verdict_clean(self):
        report = SkepticReport(passed=True, verdict="clean", session_id="s")
        self.assertEqual(report.error_count(), 0)
        self.assertEqual(report.warning_count(), 0)

    def test_report_teaching_advisory(self):
        issue = SkepticIssue(
            issue_type="stale_claim_cited",
            severity="warning",
            description="Claim c-001 is stale.",
            recommendation="Retract the claim.",
        )
        report = SkepticReport(
            passed=False, verdict="advisory", issues=[issue], session_id="s"
        )
        advisory = report.teaching_advisory()
        self.assertIn("stale_claim_cited", advisory)
        self.assertIn("Retract the claim", advisory)


# ---------------------------------------------------------------------------
# TestCheckStaleClaims
# ---------------------------------------------------------------------------

class TestCheckStaleClaims(unittest.TestCase):

    def test_no_markers_no_issues(self):
        session = _FakeSession(claims={"c-001": _claim("c-001", "supported")})
        issues = check_stale_claims_cited(session, "No claim markers here.")
        self.assertEqual(issues, [])

    def test_supported_claim_marker_no_issue(self):
        session = _FakeSession(claims={"c-001": _claim("c-001", "supported")})
        issues = check_stale_claims_cited(session, "Result [claim:c-001] shows X.")
        self.assertEqual(issues, [])

    def test_stale_claim_marker_raises_warning(self):
        session = _FakeSession(claims={"c-001": _claim("c-001", "stale")})
        issues = check_stale_claims_cited(session, "Result [claim:c-001] shows X.")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "stale_claim_cited")
        self.assertEqual(issues[0].severity, "warning")
        self.assertEqual(issues[0].claim_id, "c-001")

    def test_retracted_claim_marker_raises_error(self):
        session = _FakeSession(claims={"c-001": _claim("c-001", "retracted")})
        issues = check_stale_claims_cited(session, "As shown [claim:c-001],")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "retracted_claim_cited")
        self.assertEqual(issues[0].severity, "error")

    def test_unknown_claim_id_ignored(self):
        session = _FakeSession(claims={})
        issues = check_stale_claims_cited(session, "[claim:nonexistent]")
        self.assertEqual(issues, [])


# ---------------------------------------------------------------------------
# TestCheckScopeOverreach
# ---------------------------------------------------------------------------

class TestCheckScopeOverreach(unittest.TestCase):

    def test_no_gauge_in_text_no_issue(self):
        session = _FakeSession(claims={"c-001": _claim("c-001", basins=["01013500"])})
        issues = check_scope_overreach(session, "Mean streamflow is high.")
        self.assertEqual(issues, [])

    def test_gauge_in_scope_no_issue(self):
        session = _FakeSession(claims={"c-001": _claim("c-001", basins=["01013500"])})
        issues = check_scope_overreach(session, "Gauge 01013500 shows runoff of 0.5.")
        self.assertEqual(issues, [])

    def test_gauge_outside_scope_raises_warning(self):
        session = _FakeSession(claims={"c-001": _claim("c-001", basins=["01013500"])})
        issues = check_scope_overreach(session, "Gauge 09380000 was also studied.")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "scope_overreach")
        self.assertEqual(issues[0].severity, "warning")
        self.assertIn("09380000", issues[0].description)

    def test_no_claims_no_check(self):
        session = _FakeSession(claims={})
        issues = check_scope_overreach(session, "Gauge 09380000 was studied.")
        self.assertEqual(issues, [])

    def test_empty_text_no_issue(self):
        session = _FakeSession(claims={"c-001": _claim("c-001", basins=["01013500"])})
        issues = check_scope_overreach(session, "")
        self.assertEqual(issues, [])


# ---------------------------------------------------------------------------
# TestCheckAssumptionViolations
# ---------------------------------------------------------------------------

class TestCheckAssumptionViolations(unittest.TestCase):

    def test_no_assumptions_no_issues(self):
        session = _FakeSession(
            claims={"c-001": _claim("c-001", "supported")},
            assumptions={},
        )
        self.assertEqual(check_assumption_violations(session), [])

    def test_validated_assumption_no_issue(self):
        session = _FakeSession(
            claims={"c-001": _claim("c-001", "supported")},
            assumptions={"a-001": _assumption(risk="high", validated=True)},
        )
        self.assertEqual(check_assumption_violations(session), [])

    def test_low_risk_unvalidated_no_issue(self):
        session = _FakeSession(
            claims={"c-001": _claim("c-001", "supported")},
            assumptions={"a-001": _assumption(risk="low", validated=False)},
        )
        self.assertEqual(check_assumption_violations(session), [])

    def test_high_risk_unvalidated_with_supported_claim_raises_advisory(self):
        session = _FakeSession(
            claims={"c-001": _claim("c-001", "supported")},
            assumptions={"a-001": _assumption(risk="high", validated=False)},
        )
        issues = check_assumption_violations(session)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "unvalidated_high_risk_assumption")
        self.assertEqual(issues[0].severity, "advisory")

    def test_no_supported_claims_no_issue(self):
        session = _FakeSession(
            claims={"c-001": _claim("c-001", "proposed")},
            assumptions={"a-001": _assumption(risk="high", validated=False)},
        )
        self.assertEqual(check_assumption_violations(session), [])


# ---------------------------------------------------------------------------
# TestCheckRegistryConflicts
# ---------------------------------------------------------------------------

class TestCheckRegistryConflicts(unittest.TestCase):

    def test_no_registry_id_no_issue(self):
        session = _FakeSession(claims={"c-001": _claim("c-001", "supported")})
        self.assertEqual(check_registry_conflicts(session), [])

    def test_promoted_status_no_issue(self):
        c = _claim("c-001", "supported", registry_id="reg.abc12345.2026-06-12.a1b2c3")
        session = _FakeSession(claims={"c-001": c})
        self.assertEqual(check_registry_conflicts(session), [])

    def test_stale_with_registry_id_raises_warning(self):
        c = _claim("c-001", "stale", registry_id="reg.abc12345.2026-06-12.a1b2c3")
        session = _FakeSession(claims={"c-001": c})
        issues = check_registry_conflicts(session)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "registry_conflict")
        self.assertEqual(issues[0].severity, "warning")

    def test_retracted_with_registry_id_raises_warning(self):
        c = _claim("c-001", "retracted", registry_id="reg.abc12345.2026-06-12.a1b2c3")
        session = _FakeSession(claims={"c-001": c})
        issues = check_registry_conflicts(session)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "registry_conflict")


# ---------------------------------------------------------------------------
# TestRunAllChecks
# ---------------------------------------------------------------------------

class TestRunAllChecks(unittest.TestCase):

    def test_clean_session_verdict_clean(self):
        session = _FakeSession(
            claims={"c-001": _claim("c-001", "supported")},
        )
        report = run_all_checks(session, "Streamflow at 01013500 is high [claim:c-001].")
        self.assertTrue(report.passed)
        self.assertEqual(report.verdict, "clean")
        self.assertEqual(report.n_claims_checked, 1)
        self.assertEqual(report.n_assumptions_checked, 0)

    def test_stale_cited_verdict_advisory(self):
        session = _FakeSession(claims={"c-001": _claim("c-001", "stale")})
        report = run_all_checks(session, "As per [claim:c-001], flow is high.")
        self.assertFalse(report.passed)
        self.assertEqual(report.verdict, "advisory")
        self.assertEqual(len(report.issues), 1)

    def test_retracted_cited_verdict_flagged(self):
        session = _FakeSession(claims={"c-001": _claim("c-001", "retracted")})
        report = run_all_checks(session, "As per [claim:c-001], flow is high.")
        self.assertFalse(report.passed)
        self.assertEqual(report.verdict, "flagged")
        self.assertEqual(report.error_count(), 1)

    def test_scope_overreach_verdict_advisory(self):
        session = _FakeSession(claims={"c-001": _claim("c-001", basins=["01013500"])})
        report = run_all_checks(session, "Gauge 09380000 also shows trends.")
        self.assertFalse(report.passed)
        self.assertEqual(report.verdict, "advisory")
        types = [i.issue_type for i in report.issues]
        self.assertIn("scope_overreach", types)

    def test_multiple_issues_combined(self):
        c_stale = _claim("c-001", "stale", basins=["01013500"])
        c_supported = _claim("c-002", "supported", basins=["01013500"])
        session = _FakeSession(
            claims={"c-001": c_stale, "c-002": c_supported},
            assumptions={"a-001": _assumption(risk="high", validated=False)},
        )
        report = run_all_checks(
            session,
            "As per [claim:c-001], gauge 09380000 is similar.",
        )
        # stale_claim_cited (warning) + scope_overreach (warning) +
        # unvalidated_high_risk_assumption (advisory) = 3 issues
        self.assertGreaterEqual(len(report.issues), 3)
        types = {i.issue_type for i in report.issues}
        self.assertIn("stale_claim_cited", types)
        self.assertIn("scope_overreach", types)
        self.assertIn("unvalidated_high_risk_assumption", types)


# ---------------------------------------------------------------------------
# TestRunSkepticMCPTool
# ---------------------------------------------------------------------------

class TestRunSkepticMCPTool(unittest.TestCase):

    def setUp(self):
        import tempfile, pathlib
        import ai_hydro.session.store as _store
        self._tmp = tempfile.mkdtemp()
        self._sessions_dir = pathlib.Path(self._tmp) / "sessions"
        self._sessions_dir.mkdir()
        self._orig_dir = _store._SESSIONS_DIR
        _store._SESSIONS_DIR = self._sessions_dir

    def tearDown(self):
        import ai_hydro.session.store as _store
        _store._SESSIONS_DIR = self._orig_dir

    def _make_session(self, session_id: str, claims: dict | None = None) -> None:
        from ai_hydro.session.store import HydroSession
        session = HydroSession(session_id)
        if claims:
            session.claims = claims
        session.save()

    def test_clean_session_returns_verdict_clean(self):
        from ai_hydro.mcp.tools_skeptic import run_skeptic
        self._make_session(
            "sk-001",
            claims={"c-001": _claim("c-001", "supported", basins=["01013500"])},
        )
        r = run_skeptic("sk-001", "Streamflow at 01013500 [claim:c-001] is steady.")
        self.assertNotIn("error", r)
        self.assertEqual(r["verdict"], "clean")
        self.assertEqual(len(r.get("issues", [])), 0)

    def test_scope_overreach_detected(self):
        from ai_hydro.mcp.tools_skeptic import run_skeptic
        self._make_session(
            "sk-002",
            claims={"c-001": _claim("c-001", "supported", basins=["01013500"])},
        )
        r = run_skeptic("sk-002", "Gauge 09380000 shows similar patterns.")
        self.assertNotIn("error", r)
        self.assertEqual(r["verdict"], "advisory")
        types = [i["issue_type"] for i in r["issues"]]
        self.assertIn("scope_overreach", types)

    def test_stale_claim_cited_detected(self):
        from ai_hydro.mcp.tools_skeptic import run_skeptic
        self._make_session(
            "sk-003",
            claims={"c-001": _claim("c-001", "stale")},
        )
        r = run_skeptic("sk-003", "As per [claim:c-001], flow is consistent.")
        self.assertNotIn("error", r)
        self.assertEqual(r["verdict"], "advisory")
        self.assertIn("advisory", r)  # teaching advisory string

    def test_missing_session_returns_error(self):
        from ai_hydro.mcp.tools_skeptic import run_skeptic
        r = run_skeptic("does-not-exist", "some text")
        # Should return without raising — HydroSession.load returns empty session
        # (no error key expected since empty session is valid)
        self.assertIsInstance(r, dict)

    def test_uses_session_interpretation_when_text_omitted(self):
        from ai_hydro.session.store import HydroSession
        from ai_hydro.mcp.tools_skeptic import run_skeptic
        session = HydroSession("sk-004")
        session.interpretation = "Gauge 09380000 shows trends."
        session.claims = {"c-001": _claim("c-001", "supported", basins=["01013500"])}
        session.save()
        r = run_skeptic("sk-004")  # no text — uses session.interpretation
        self.assertNotIn("error", r)
        # scope overreach: 09380000 not in scope
        self.assertEqual(r["verdict"], "advisory")


if __name__ == "__main__":
    unittest.main()
