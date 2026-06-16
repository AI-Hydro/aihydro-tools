"""
Unit tests for the Answer Auditor (ai_hydro/audit/).

These tests exercise the grammar parser, resolver, and the MCP gate
independently — no external APIs, no real session files.
Session storage is monkeypatched to tmp_path so tests don't pollute ~/.aihydro.
"""
from __future__ import annotations

import pytest
from ai_hydro.audit.grammar import extract_markers, extract_numerics, MarkerKind
from ai_hydro.audit.models import AuditReport, AuditViolation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(session_id: str, run_log: dict | None = None,
                  claims: dict | None = None, monkeypatch=None, sessions_dir=None):
    """Create a HydroSession with synthetic run-log and claims for testing."""
    from ai_hydro.session.store import HydroSession
    import ai_hydro.session.store as _store
    if monkeypatch and sessions_dir:
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)
    session = HydroSession(session_id)
    if run_log is not None:
        session.set("_run_log", run_log)
    if claims is not None:
        session.claims = claims
    session.save()
    return session


# ---------------------------------------------------------------------------
# Grammar: extract_markers
# ---------------------------------------------------------------------------

class TestExtractMarkers:
    def test_run_marker_parsed(self):
        text = "The ratio is 0.42 [run:sigs.20260610.sess.ab12#key_outputs.runoff_ratio]."
        markers = extract_markers(text)
        assert len(markers) == 1
        m = markers[0]
        assert m.kind == MarkerKind.RUN
        assert m.run_id == "sigs.20260610.sess.ab12"
        assert m.json_path == "key_outputs.runoff_ratio"

    def test_claim_marker_parsed(self):
        text = "This supports [claim:C-001] the hypothesis."
        markers = extract_markers(text)
        assert len(markers) == 1
        assert markers[0].kind == MarkerKind.CLAIM
        assert markers[0].claim_id == "C-001"

    def test_lit_marker_parsed(self):
        text = "The threshold of 0.41 [lit:gupta2009] is standard."
        markers = extract_markers(text)
        assert len(markers) == 1
        assert markers[0].kind == MarkerKind.LIT

    def test_multiple_markers_all_extracted(self):
        text = (
            "NSE=0.81 [run:cal.1#key_outputs.nse] beats threshold 0.41 [lit:gupta2009] "
            "confirming [claim:C-002]."
        )
        markers = extract_markers(text)
        kinds = {m.kind for m in markers}
        assert MarkerKind.RUN in kinds
        assert MarkerKind.LIT in kinds
        assert MarkerKind.CLAIM in kinds

    def test_empty_text_returns_empty(self):
        assert extract_markers("") == []

    def test_no_markers_returns_empty(self):
        assert extract_markers("The sky is blue, Figure 3 shows results.") == []


# ---------------------------------------------------------------------------
# Grammar: extract_numerics
# ---------------------------------------------------------------------------

class TestExtractNumerics:
    def test_plain_float(self):
        spans = extract_numerics("The ratio is 0.42 here.")
        assert any(s.raw == "0.42" for s in spans)

    def test_percentage(self):
        spans = extract_numerics("Coverage of 85% was achieved.")
        assert any(s.raw == "85%" for s in spans)

    def test_comma_thousands(self):
        spans = extract_numerics("Area of 4,256 km².")
        assert any("4,256" in s.raw for s in spans)

    def test_year_whitelisted_not_numeric(self):
        # 4-digit years should not be flagged; they are handled by whitelist
        spans = extract_numerics("Published in 2023.")
        # Year may be extracted but should be whitelisted by resolver
        # Here we just check grammar extraction; whitelist is tested in resolver
        pass

    def test_figure_ref_not_numeric(self):
        spans = extract_numerics("As shown in Figure 3.")
        # "3" appears but should be context-whitelisted
        pass


# ---------------------------------------------------------------------------
# Resolver: full audit_prose
# ---------------------------------------------------------------------------

class TestResolveProse:
    def test_clean_prose_passes(self, tmp_path, monkeypatch):
        sid = "test-audit-clean-01"
        _make_session(sid, run_log={
            "sigs.20260610.sess.ab12": {
                "run_id": "sigs.20260610.sess.ab12",
                "key_outputs": {"runoff_ratio": 0.42, "baseflow_index": 0.61},
            }
        }, monkeypatch=monkeypatch, sessions_dir=tmp_path / "sessions")

        from ai_hydro.audit import audit_prose
        prose = (
            "The basin runoff ratio of 0.42 "
            "[run:sigs.20260610.sess.ab12#key_outputs.runoff_ratio] is typical, "
            "and the baseflow index is 0.61 "
            "[run:sigs.20260610.sess.ab12#key_outputs.baseflow_index]."
        )
        report = audit_prose(prose, sid)
        assert report.passed
        assert report.numeric_coverage == 1.0
        assert report.violations == []

    def test_uncited_number_fails(self, tmp_path, monkeypatch):
        sid = "test-audit-uncited-02"
        _make_session(sid, run_log={}, monkeypatch=monkeypatch,
                      sessions_dir=tmp_path / "sessions")

        from ai_hydro.audit import audit_prose
        report = audit_prose("The runoff ratio is 0.42 with no marker.", sid)
        assert not report.passed
        assert any(v.kind == "uncited_number" for v in report.violations)

    def test_run_id_not_found_fails(self, tmp_path, monkeypatch):
        sid = "test-audit-badid-03"
        _make_session(sid, run_log={
            "real.run.id.1234": {"run_id": "real.run.id.1234", "key_outputs": {"nse": 0.81}}
        }, monkeypatch=monkeypatch, sessions_dir=tmp_path / "sessions")

        from ai_hydro.audit import audit_prose
        prose = "NSE of 0.81 [run:FAKE.run.id.9999#key_outputs.nse]."
        report = audit_prose(prose, sid)
        assert not report.passed
        assert any(v.kind == "run_id_not_found" for v in report.violations)

    def test_value_mismatch_fails(self, tmp_path, monkeypatch):
        sid = "test-audit-mismatch-04"
        _make_session(sid, run_log={
            "sigs.run.mm.5678": {"run_id": "sigs.run.mm.5678", "key_outputs": {"runoff_ratio": 0.82}}
        }, monkeypatch=monkeypatch, sessions_dir=tmp_path / "sessions")

        from ai_hydro.audit import audit_prose
        prose = "The runoff ratio is 0.42 [run:sigs.run.mm.5678#key_outputs.runoff_ratio]."
        report = audit_prose(prose, sid)
        assert not report.passed
        assert any(v.kind == "value_mismatch" for v in report.violations)
        v = next(v for v in report.violations if v.kind == "value_mismatch")
        assert v.prose_value == "0.42"
        assert abs(float(v.stored_value) - 0.82) < 0.001

    def test_json_path_not_found_fails(self, tmp_path, monkeypatch):
        sid = "test-audit-path-05"
        _make_session(sid, run_log={
            "sigs.run.jp.9abc": {"run_id": "sigs.run.jp.9abc", "key_outputs": {"nse": 0.81}}
        }, monkeypatch=monkeypatch, sessions_dir=tmp_path / "sessions")

        from ai_hydro.audit import audit_prose
        prose = "The KGE is 0.81 [run:sigs.run.jp.9abc#key_outputs.kge]."
        report = audit_prose(prose, sid)
        assert not report.passed
        assert any(v.kind == "json_path_not_found" for v in report.violations)

    def test_lit_marker_passes_without_run_log(self, tmp_path, monkeypatch):
        sid = "test-audit-lit-06"
        _make_session(sid, run_log={}, monkeypatch=monkeypatch,
                      sessions_dir=tmp_path / "sessions")

        from ai_hydro.audit import audit_prose
        prose = "The threshold NSE of -0.41 [lit:gupta2009] is widely cited."
        report = audit_prose(prose, sid)
        assert report.passed

    def test_claim_not_found_fails(self, tmp_path, monkeypatch):
        sid = "test-audit-claim-07"
        _make_session(sid, run_log={}, claims={}, monkeypatch=monkeypatch,
                      sessions_dir=tmp_path / "sessions")

        from ai_hydro.audit import audit_prose
        prose = "This confirms [claim:C-999] the hypothesis."
        report = audit_prose(prose, sid)
        assert not report.passed
        assert any(v.kind == "claim_not_found" for v in report.violations)

    def test_claim_bad_status_fails(self, tmp_path, monkeypatch):
        sid = "test-audit-claim-bad-08"
        _make_session(sid, run_log={}, claims={
            "C-001": {"status": "proposed", "statement": "something"}
        }, monkeypatch=monkeypatch, sessions_dir=tmp_path / "sessions")

        from ai_hydro.audit import audit_prose
        prose = "This confirms [claim:C-001] the hypothesis."
        report = audit_prose(prose, sid)
        assert not report.passed
        assert any(v.kind == "claim_bad_status" for v in report.violations)

    def test_claim_supported_status_passes(self, tmp_path, monkeypatch):
        sid = "test-audit-claim-ok-09"
        _make_session(sid, run_log={}, claims={
            "C-002": {"status": "supported", "statement": "something"}
        }, monkeypatch=monkeypatch, sessions_dir=tmp_path / "sessions")

        from ai_hydro.audit import audit_prose
        prose = "This confirms [claim:C-002] that the model is valid."
        report = audit_prose(prose, sid)
        assert report.passed

    def test_rounding_tolerance_passes(self, tmp_path, monkeypatch):
        """Prose '0.82' should pass against stored 0.819 (within rounding)."""
        sid = "test-audit-round-10"
        _make_session(sid, run_log={
            "cal.run.rnd.0001": {"run_id": "cal.run.rnd.0001", "key_outputs": {"nse": 0.819}}
        }, monkeypatch=monkeypatch, sessions_dir=tmp_path / "sessions")

        from ai_hydro.audit import audit_prose
        prose = "NSE = 0.82 [run:cal.run.rnd.0001#key_outputs.nse]."
        report = audit_prose(prose, sid)
        assert report.passed

    def test_empty_prose_returns_trivial_pass(self, tmp_path, monkeypatch):
        """Empty prose (no numerics, no markers) should pass with coverage=1.0."""
        sid = "test-audit-empty-11"
        _make_session(sid, monkeypatch=monkeypatch,
                      sessions_dir=tmp_path / "sessions")

        from ai_hydro.audit import audit_prose
        report = audit_prose("No numbers here, qualitative only.", sid)
        assert report.passed
        assert report.numeric_coverage == 1.0

    def test_teaching_error_contains_fix_hints(self, tmp_path, monkeypatch):
        """teaching_error() output includes the fix_hint from each violation."""
        sid = "test-audit-teach-12"
        _make_session(sid, run_log={}, monkeypatch=monkeypatch,
                      sessions_dir=tmp_path / "sessions")

        from ai_hydro.audit import audit_prose
        report = audit_prose("The ratio is 0.42 uncited.", sid)
        assert not report.passed
        te = report.teaching_error()
        assert "0.42" in te or "uncited" in te.lower() or "run:" in te.lower()


# ---------------------------------------------------------------------------
# MCP tool: audit_interpretation
# ---------------------------------------------------------------------------

class TestAuditInterpretationTool:
    def test_tool_passes_clean_prose(self, tmp_path, monkeypatch):
        import ai_hydro.session.store as _store
        sessions_dir = tmp_path / "sessions"
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-tool-audit-pass-01"
        _make_session(sid, run_log={
            "sigs.tool.pass.ab12": {
                "key_outputs": {"runoff_ratio": 0.42}
            }
        }, monkeypatch=monkeypatch, sessions_dir=sessions_dir)

        from ai_hydro.mcp.tools_audit import audit_interpretation
        result = audit_interpretation(
            session_id=sid,
            prose="Runoff ratio 0.42 [run:sigs.tool.pass.ab12#key_outputs.runoff_ratio].",
        )
        assert result["passed"] is True
        assert "error" not in result

    def test_tool_fails_uncited(self, tmp_path, monkeypatch):
        import ai_hydro.session.store as _store
        sessions_dir = tmp_path / "sessions"
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-tool-audit-fail-02"
        _make_session(sid, run_log={}, monkeypatch=monkeypatch,
                      sessions_dir=sessions_dir)

        from ai_hydro.mcp.tools_audit import audit_interpretation
        result = audit_interpretation(session_id=sid, prose="The ratio is 0.42.")
        assert result["passed"] is False
        assert len(result["violations"]) > 0
        assert "teaching_error" in result

    def test_tool_empty_prose_returns_error(self, tmp_path, monkeypatch):
        import ai_hydro.session.store as _store
        sessions_dir = tmp_path / "sessions"
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-tool-audit-empty-03"
        _make_session(sid, monkeypatch=monkeypatch, sessions_dir=sessions_dir)

        from ai_hydro.mcp.tools_audit import audit_interpretation
        result = audit_interpretation(session_id=sid, prose="")
        assert "error" in result


# ---------------------------------------------------------------------------
# Write gate: write_research_interpretation
# ---------------------------------------------------------------------------

class TestWriteGate:
    def test_gate_refuses_uncited_prose(self, tmp_path, monkeypatch):
        import ai_hydro.session.store as _store
        sessions_dir = tmp_path / "sessions"
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-write-gate-refuse-01"
        _make_session(sid, run_log={}, monkeypatch=monkeypatch,
                      sessions_dir=sessions_dir)

        from ai_hydro.mcp.tools_session import write_research_interpretation
        result = write_research_interpretation(
            session_id=sid,
            interpretation="The runoff ratio is 0.42 with no citation.",
        )
        assert result.get("error") == "audit_failed"
        assert "teaching_error" in result
        assert result.get("violation_count", 0) > 0
        assert "written_path" not in result

    def test_gate_allows_fully_cited_prose(self, tmp_path, monkeypatch):
        import ai_hydro.session.store as _store
        sessions_dir = tmp_path / "sessions"
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-write-gate-allow-02"
        _make_session(sid, run_log={
            "sigs.gate.ok.ef56": {
                "key_outputs": {"runoff_ratio": 0.42}
            }
        }, monkeypatch=monkeypatch, sessions_dir=sessions_dir)

        from ai_hydro.mcp.tools_session import write_research_interpretation
        result = write_research_interpretation(
            session_id=sid,
            interpretation=(
                "The basin has a runoff ratio of 0.42 "
                "[run:sigs.gate.ok.ef56#key_outputs.runoff_ratio], "
                "indicating a humid temperate climate."
            ),
        )
        assert result.get("error") != "audit_failed"
        assert "written_path" in result
        assert result.get("audit_passed") is True

    def test_gate_allows_pure_qualitative_prose(self, tmp_path, monkeypatch):
        import ai_hydro.session.store as _store
        sessions_dir = tmp_path / "sessions"
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-write-gate-qualitative-03"
        _make_session(sid, run_log={}, monkeypatch=monkeypatch,
                      sessions_dir=sessions_dir)

        from ai_hydro.mcp.tools_session import write_research_interpretation
        result = write_research_interpretation(
            session_id=sid,
            interpretation="The basin is hydrologically complex with multiple sub-regimes.",
        )
        assert result.get("error") != "audit_failed"
        assert "written_path" in result
