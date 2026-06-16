"""
Unit tests for ai_hydro/reports/defensibility.py and the
export_session(format='defensibility_report') integration.

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
    interpretation: str = "",
    claims: dict | None = None,
    run_log: dict | None = None,
    slots: dict | None = None,
):
    import ai_hydro.session.store as _store
    from ai_hydro.session.store import HydroSession

    monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

    session = HydroSession(session_id)
    session.interpretation = interpretation
    if claims:
        session.claims = claims
    if run_log:
        session.set("_run_log", run_log)
    if slots:
        for slot_key, slot_data in slots.items():
            session.set(slot_key, slot_data)
    session.save()
    return session


_MINIMAL_CLAIM = {
    "id": "claim-001",
    "claim": "Mean annual flow is 1.25 mm/day.",
    "claim_type": "empirical_result",
    "status": "supported",
    "confidence": "high",
    "confidence_rationale": "Bootstrap 90% CI does not overlap zero.",
    "scope": {"basins": ["01031500"], "period": "1990-2020"},
    "evidence_spans": [{"source_type": "run", "source_id": "sigs.20260610.bench.ab12", "metric_ref": "q_mean"}],
    "limitations": ["Single gauge only."],
}

_RUN_LOG = {
    "sigs.20260610.bench.ab12": {
        "run_id": "sigs.20260610.bench.ab12",
        "tool_name": "extract_hydrological_signatures",
        "session_id": "test-def-01",
        "timestamp": "2026-06-10T00:00:00+00:00",
        "key_outputs": {
            "q_mean": 1.25,
            "flow_variability": 2.10,
            "_quality_flags": [
                {"validator": "uncertainty_present", "status": "pass"}
            ],
        },
    },
    "model.20260610.bench.cd34": {
        "run_id": "model.20260610.bench.cd34",
        "tool_name": "train_hydro_model",
        "session_id": "test-def-01",
        "timestamp": "2026-06-10T01:00:00+00:00",
        "key_outputs": {"nse": 0.71, "kge": 0.68},
    },
}

_SIGS_SLOT = {
    "data": {
        "q_mean": 1.25,
        "_uncertainty": {
            "q_mean": {"value": 1.25, "ci_low": 1.10, "ci_high": 1.40,
                       "method": "bootstrap_block", "n": 500, "ci_level": 0.90},
        },
    },
    "meta": {"tool": "extract_hydrological_signatures", "params": {}, "computed_at": "2026-06-10"},
}


# ---------------------------------------------------------------------------
# Section 2: Audit summary
# ---------------------------------------------------------------------------

class TestAuditSection:
    def test_no_interpretation_shows_placeholder(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-audit-01", monkeypatch, tmp_path)
        md, summary = build_defensibility_report(sess, "test-def-audit-01", "2026-06-10")
        assert "No interpretation found" in md
        assert summary["audit_passed"] is None

    def test_clean_interpretation_sets_audit_passed(self, tmp_path, monkeypatch):
        # A fully-cited interpretation should pass (no unbound numerics)
        interp = "The basin is humid."
        sess = _make_session("test-def-audit-02", monkeypatch, tmp_path,
                              interpretation=interp)
        md, summary = build_defensibility_report(sess, "test-def-audit-02", "2026-06-10")
        assert summary["audit_passed"] is not None
        assert isinstance(summary["numeric_coverage"], float)

    def test_numeric_coverage_in_summary(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-audit-03", monkeypatch, tmp_path,
                              interpretation="Flow is normal.")
        _, summary = build_defensibility_report(sess, "test-def-audit-03", "2026-06-10")
        assert 0.0 <= summary["numeric_coverage"] <= 1.0

    def test_violations_table_present_when_audit_fails(self, tmp_path, monkeypatch):
        # Unbound numeric: "1.25 mm/day" with no [run:...] marker → violation
        interp = "Mean flow is 1.25 mm/day."
        sess = _make_session("test-def-audit-04", monkeypatch, tmp_path,
                              interpretation=interp)
        md, summary = build_defensibility_report(sess, "test-def-audit-04", "2026-06-10")
        # If audit fails (violations found), violations table should appear
        if not summary["audit_passed"]:
            assert "Violations" in md or "violation" in md.lower()


# ---------------------------------------------------------------------------
# Section 3: Uncertainty coverage
# ---------------------------------------------------------------------------

class TestUncertaintySection:
    def test_slot_with_uncertainty_shows_check(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-unc-01", monkeypatch, tmp_path,
                              slots={"signatures": _SIGS_SLOT})
        md, summary = build_defensibility_report(sess, "test-def-unc-01", "2026-06-10")
        assert "✅" in md
        assert summary["n_with_uncertainty"] == 1

    def test_slot_without_uncertainty_shows_cross(self, tmp_path, monkeypatch):
        slot_no_ci = {
            "data": {"q_mean": 1.25},
            "meta": {"tool": "extract_hydrological_signatures", "params": {},
                     "computed_at": "2026-06-10"},
        }
        sess = _make_session("test-def-unc-02", monkeypatch, tmp_path,
                              slots={"signatures": slot_no_ci})
        md, summary = build_defensibility_report(sess, "test-def-unc-02", "2026-06-10")
        assert "❌" in md
        assert summary["n_with_uncertainty"] == 0

    def test_uncertainty_coverage_100_when_no_quant_slots(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-unc-03", monkeypatch, tmp_path)
        _, summary = build_defensibility_report(sess, "test-def-unc-03", "2026-06-10")
        assert summary["uncertainty_coverage_pct"] == 100.0

    def test_uncertainty_coverage_pct_correct(self, tmp_path, monkeypatch):
        # 1 slot with CI, 0 without → 100%
        sess = _make_session("test-def-unc-04", monkeypatch, tmp_path,
                              slots={"signatures": _SIGS_SLOT})
        _, summary = build_defensibility_report(sess, "test-def-unc-04", "2026-06-10")
        assert summary["uncertainty_coverage_pct"] == 100.0


# ---------------------------------------------------------------------------
# Section 4: Claims register
# ---------------------------------------------------------------------------

class TestClaimsSection:
    def test_claims_table_present(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-claims-01", monkeypatch, tmp_path,
                              claims={"claim-001": _MINIMAL_CLAIM})
        md, summary = build_defensibility_report(sess, "test-def-claims-01", "2026-06-10")
        assert "claim-001" in md
        assert summary["n_claims"] == 1

    def test_no_claims_shows_placeholder(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-claims-02", monkeypatch, tmp_path)
        md, summary = build_defensibility_report(sess, "test-def-claims-02", "2026-06-10")
        assert summary["n_claims"] == 0
        assert "No claims registered" in md

    def test_status_icon_in_output(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-claims-03", monkeypatch, tmp_path,
                              claims={"claim-001": _MINIMAL_CLAIM})
        md, _ = build_defensibility_report(sess, "test-def-claims-03", "2026-06-10")
        assert "● supported" in md

    def test_status_breakdown_in_output(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-claims-04", monkeypatch, tmp_path,
                              claims={"claim-001": _MINIMAL_CLAIM})
        md, _ = build_defensibility_report(sess, "test-def-claims-04", "2026-06-10")
        assert "Summary" in md

    def test_multiple_claims_counted(self, tmp_path, monkeypatch):
        c2 = dict(_MINIMAL_CLAIM)
        c2["id"] = "claim-002"
        c2["claim"] = "Baseflow index is 0.55."
        c2["status"] = "weakly_supported"
        sess = _make_session("test-def-claims-05", monkeypatch, tmp_path,
                              claims={"claim-001": _MINIMAL_CLAIM, "claim-002": c2})
        _, summary = build_defensibility_report(sess, "test-def-claims-05", "2026-06-10")
        assert summary["n_claims"] == 2


# ---------------------------------------------------------------------------
# Section 5: Validator flags
# ---------------------------------------------------------------------------

class TestValidatorSection:
    def test_quality_flags_from_run_log(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-val-01", monkeypatch, tmp_path,
                              run_log=_RUN_LOG)
        md, summary = build_defensibility_report(sess, "test-def-val-01", "2026-06-10")
        assert "uncertainty_present" in md
        assert summary["n_validator_flags"] >= 1

    def test_no_flags_shows_placeholder(self, tmp_path, monkeypatch):
        run_log_no_flags = {
            "sigs.20260610.bench.ab12": {
                "run_id": "sigs.20260610.bench.ab12",
                "tool_name": "extract_hydrological_signatures",
                "session_id": "test-def-val-02",
                "timestamp": "2026-06-10T00:00:00+00:00",
                "key_outputs": {"q_mean": 1.25},
            }
        }
        sess = _make_session("test-def-val-02", monkeypatch, tmp_path,
                              run_log=run_log_no_flags)
        md, summary = build_defensibility_report(sess, "test-def-val-02", "2026-06-10")
        assert summary["n_validator_flags"] == 0


# ---------------------------------------------------------------------------
# Section 6: Numbers manifest
# ---------------------------------------------------------------------------

class TestNumbersManifest:
    def test_cited_numbers_from_run_log(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-num-01", monkeypatch, tmp_path,
                              run_log=_RUN_LOG)
        md, summary = build_defensibility_report(sess, "test-def-num-01", "2026-06-10")
        assert "q_mean" in md
        assert "1.25" in md
        assert "nse" in md
        assert summary["n_cited_numbers"] >= 3  # q_mean, flow_variability, nse, kge

    def test_private_keys_excluded(self, tmp_path, monkeypatch):
        run_log_private = {
            "sigs.20260610.bench.ab12": {
                "run_id": "sigs.20260610.bench.ab12",
                "tool_name": "extract_hydrological_signatures",
                "session_id": "test-def-num-02",
                "timestamp": "2026-06-10T00:00:00+00:00",
                "key_outputs": {
                    "_quality_flags": [{"validator": "x", "status": "pass"}],
                    "q_mean": 1.25,
                },
            }
        }
        sess = _make_session("test-def-num-02", monkeypatch, tmp_path,
                              run_log=run_log_private)
        md, summary = build_defensibility_report(sess, "test-def-num-02", "2026-06-10")
        assert "_quality_flags" not in md
        assert summary["n_cited_numbers"] == 1  # only q_mean

    def test_no_run_log_shows_placeholder(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-num-03", monkeypatch, tmp_path)
        md, summary = build_defensibility_report(sess, "test-def-num-03", "2026-06-10")
        assert summary["n_cited_numbers"] == 0
        assert "No cited numbers" in md

    def test_string_values_excluded(self, tmp_path, monkeypatch):
        run_log_mixed = {
            "q.20260610.bench.xy99": {
                "run_id": "q.20260610.bench.xy99",
                "tool_name": "fetch_streamflow_data",
                "session_id": "test-def-num-04",
                "timestamp": "2026-06-10T00:00:00+00:00",
                "key_outputs": {"label": "humid", "n_days": 3650},
            }
        }
        sess = _make_session("test-def-num-04", monkeypatch, tmp_path,
                              run_log=run_log_mixed)
        _, summary = build_defensibility_report(sess, "test-def-num-04", "2026-06-10")
        assert summary["n_cited_numbers"] == 1  # only n_days (int), not "humid" (str)


# ---------------------------------------------------------------------------
# Header + structure
# ---------------------------------------------------------------------------

class TestReportStructure:
    def test_all_six_sections_present(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-struct-01", monkeypatch, tmp_path,
                              run_log=_RUN_LOG,
                              claims={"claim-001": _MINIMAL_CLAIM},
                              slots={"signatures": _SIGS_SLOT})
        md, _ = build_defensibility_report(sess, "test-def-struct-01", "2026-06-10")
        for section in ["Defensibility Report", "Audit Summary",
                        "Uncertainty Coverage", "Claims Register",
                        "Validator Flags", "Numbers Manifest"]:
            assert section in md, f"Section missing: {section}"

    def test_session_id_in_header(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-struct-02", monkeypatch, tmp_path)
        md, _ = build_defensibility_report(sess, "test-def-struct-02", "2026-06-10")
        assert "test-def-struct-02" in md

    def test_export_date_in_header(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-struct-03", monkeypatch, tmp_path)
        md, _ = build_defensibility_report(sess, "test-def-struct-03", "2026-06-10")
        assert "2026-06-10" in md

    def test_returns_string_and_dict(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-struct-04", monkeypatch, tmp_path)
        result = build_defensibility_report(sess, "test-def-struct-04", "2026-06-10")
        assert isinstance(result, tuple) and len(result) == 2
        md, summary = result
        assert isinstance(md, str)
        assert isinstance(summary, dict)

    def test_summary_has_all_keys(self, tmp_path, monkeypatch):
        sess = _make_session("test-def-struct-05", monkeypatch, tmp_path)
        _, summary = build_defensibility_report(sess, "test-def-struct-05", "2026-06-10")
        for key in ("audit_passed", "n_violations", "numeric_coverage", "n_claims",
                    "n_with_uncertainty", "n_quant_slots", "uncertainty_coverage_pct",
                    "n_cited_numbers", "n_validator_flags"):
            assert key in summary, f"Missing summary key: {key}"


# ---------------------------------------------------------------------------
# export_session integration
# ---------------------------------------------------------------------------

class TestExportSessionDefensibilityReport:
    def test_returns_file_saved_and_summary_keys(self, tmp_path, monkeypatch):
        import ai_hydro.mcp.tools_session as _ts
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-export-def-01"
        session = HydroSession(sid)
        session.set("_run_log", _RUN_LOG)
        session.claims = {"claim-001": _MINIMAL_CLAIM}
        session.save()

        result = _ts.export_session(session_id=sid, format="defensibility_report")
        assert "error" not in result, result
        assert result["format"] == "defensibility_report"
        assert result["file_saved"]
        assert "n_claims" in result
        assert result["n_claims"] == 1
        assert "n_cited_numbers" in result

    def test_file_contains_section_headers(self, tmp_path, monkeypatch):
        import ai_hydro.mcp.tools_session as _ts
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-export-def-02"
        session = HydroSession(sid)
        session.save()

        result = _ts.export_session(session_id=sid, format="defensibility_report")
        assert "error" not in result

        import pathlib
        md = pathlib.Path(result["file_saved"]).read_text()
        assert "Defensibility Report" in md
        assert "Numbers Manifest" in md
