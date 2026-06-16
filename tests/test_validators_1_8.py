"""
Unit tests for Phase 1.8 validator expansion.

Covers:
  - check_record_length (pass / warning / fail / insufficient_data)
  - check_usgs_qualification_codes (pass / warning provisional / warning estimated / insufficient_data)
  - check_regulated_basin (pass via metadata flag / warning via metadata flag / insufficient_data)
  - check_stationarity (pass / warning non-stationary / insufficient_data)

All tests are fixture-only: no network, sessions redirected to tmp_path.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(
    session_id: str,
    monkeypatch,
    sessions_dir,
    *,
    streamflow: dict | None = None,
    delineation: dict | None = None,
    signatures: dict | None = None,
):
    import ai_hydro.session.store as _store
    from ai_hydro.session.store import HydroSession

    monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

    session = HydroSession(session_id)
    if streamflow:
        session.set("streamflow", streamflow)
    if delineation:
        session.set("delineation", delineation)
    if signatures:
        session.set("signatures", signatures)
    session.save()
    return session


def _streamflow_slot(n_days: int, qual_codes=None, provisional=None) -> dict:
    """Build a minimal streamflow slot with n_days of synthetic data."""
    import datetime
    start = datetime.date(1980, 10, 1)
    dates = [(start + datetime.timedelta(days=i)).isoformat() for i in range(n_days)]
    q_vals = [1.0 + 0.01 * i for i in range(n_days)]
    d = {
        "data": {
            "gauge_id": "01031500",
            "dates": dates,
            "q_cms": q_vals,
            "units": "m3/s",
            "n_days": n_days,
        },
        "meta": {
            "tool": "fetch_streamflow_data",
            "source": "usgs nwis",
            "params": {},
        },
    }
    if qual_codes is not None:
        d["meta"]["qualification_codes"] = qual_codes
    if provisional is not None:
        d["meta"]["provisional"] = provisional
    return d


def _stationary_streamflow_with_file(n_years: int, tmp_path) -> dict:
    """n_years of daily data with NO trend — arrays in _data_file to survive strip."""
    import datetime, json
    n_days = n_years * 365
    start = datetime.date(1980, 10, 1)
    dates = [(start + datetime.timedelta(days=i)).isoformat() for i in range(n_days)]
    q_vals = [10.0] * n_days
    p = tmp_path / "streamflow_stat.json"
    p.write_text(json.dumps({"dates": dates, "q_cms": q_vals}))
    return {
        "data": {
            "gauge_id": "X", "n_days": n_days,
            "_data_file": str(p),
        },
        "meta": {"tool": "fetch_streamflow_data", "params": {}},
    }


def _trending_streamflow_with_file(n_years: int, tmp_path) -> dict:
    """n_years of daily data with a STRONG increasing trend — arrays in _data_file."""
    import datetime, json
    n_days = n_years * 365
    start = datetime.date(1980, 10, 1)
    dates = [(start + datetime.timedelta(days=i)).isoformat() for i in range(n_days)]
    q_vals = [float(i) for i in range(n_days)]
    p = tmp_path / "streamflow_trend.json"
    p.write_text(json.dumps({"dates": dates, "q_cms": q_vals}))
    return {
        "data": {
            "gauge_id": "X", "n_days": n_days,
            "_data_file": str(p),
        },
        "meta": {"tool": "fetch_streamflow_data", "params": {}},
    }


# ---------------------------------------------------------------------------
# TestCheckRecordLength
# ---------------------------------------------------------------------------

class TestCheckRecordLength:
    def test_no_streamflow_insufficient_data(self, monkeypatch, tmp_path):
        _make_session("test-rl-001", monkeypatch, tmp_path)
        from ai_hydro.mcp.tools_validators import check_record_length
        result = check_record_length("test-rl-001")
        assert result["status"] == "insufficient_data"

    def test_short_record_fail(self, monkeypatch, tmp_path):
        _make_session("test-rl-002", monkeypatch, tmp_path,
                      streamflow=_streamflow_slot(n_days=365 * 3))
        from ai_hydro.mcp.tools_validators import check_record_length
        result = check_record_length("test-rl-002")
        assert result["status"] == "fail"
        assert result["severity"] == "high"
        assert result["metadata"]["quality_flag"] == "record_too_short"

    def test_medium_record_warning(self, monkeypatch, tmp_path):
        _make_session("test-rl-003", monkeypatch, tmp_path,
                      streamflow=_streamflow_slot(n_days=365 * 15))
        from ai_hydro.mcp.tools_validators import check_record_length
        result = check_record_length("test-rl-003")
        assert result["status"] == "warning"
        assert result["metadata"]["quality_flag"] == "record_length_low"

    def test_adequate_record_pass(self, monkeypatch, tmp_path):
        _make_session("test-rl-004", monkeypatch, tmp_path,
                      streamflow=_streamflow_slot(n_days=365 * 25))
        from ai_hydro.mcp.tools_validators import check_record_length
        result = check_record_length("test-rl-004")
        assert result["status"] == "pass"
        assert result["metadata"]["n_days"] == 365 * 25

    def test_n_days_fallback_from_q_cms_n(self, monkeypatch, tmp_path):
        """Falls back to q_cms_n (stripped count) when n_days key is missing."""
        sf = _streamflow_slot(n_days=365 * 25)
        del sf["data"]["n_days"]
        # Simulate what session store does: strips array, stores count
        sf["data"].pop("q_cms", None)
        sf["data"].pop("dates", None)
        sf["data"]["q_cms_n"] = 365 * 25
        _make_session("test-rl-005", monkeypatch, tmp_path, streamflow=sf)
        from ai_hydro.mcp.tools_validators import check_record_length
        result = check_record_length("test-rl-005")
        assert result["status"] == "pass"

    def test_result_has_validator_key(self, monkeypatch, tmp_path):
        _make_session("test-rl-006", monkeypatch, tmp_path,
                      streamflow=_streamflow_slot(n_days=365 * 25))
        from ai_hydro.mcp.tools_validators import check_record_length
        result = check_record_length("test-rl-006")
        assert result["validator"] == "record_length"


# ---------------------------------------------------------------------------
# TestCheckUsgsQualificationCodes
# ---------------------------------------------------------------------------

class TestCheckUsgsQualificationCodes:
    def test_no_streamflow_insufficient_data(self, monkeypatch, tmp_path):
        _make_session("test-qc-001", monkeypatch, tmp_path)
        from ai_hydro.mcp.tools_validators import check_usgs_qualification_codes
        result = check_usgs_qualification_codes("test-qc-001")
        assert result["status"] == "insufficient_data"

    def test_no_codes_in_metadata_insufficient_data(self, monkeypatch, tmp_path):
        _make_session("test-qc-002", monkeypatch, tmp_path,
                      streamflow=_streamflow_slot(n_days=365 * 20))
        from ai_hydro.mcp.tools_validators import check_usgs_qualification_codes
        result = check_usgs_qualification_codes("test-qc-002")
        assert result["status"] == "insufficient_data"
        assert result["metadata"]["quality_flag"] == "qualification_codes_absent"

    def test_provisional_flag_warning(self, monkeypatch, tmp_path):
        _make_session("test-qc-003", monkeypatch, tmp_path,
                      streamflow=_streamflow_slot(n_days=365 * 20, provisional=True))
        from ai_hydro.mcp.tools_validators import check_usgs_qualification_codes
        result = check_usgs_qualification_codes("test-qc-003")
        assert result["status"] == "warning"
        assert result["metadata"]["quality_flag"] == "provisional_data"

    def test_provisional_code_P_warning(self, monkeypatch, tmp_path):
        _make_session("test-qc-004", monkeypatch, tmp_path,
                      streamflow=_streamflow_slot(n_days=365 * 20, qual_codes=["A", "P"]))
        from ai_hydro.mcp.tools_validators import check_usgs_qualification_codes
        result = check_usgs_qualification_codes("test-qc-004")
        assert result["status"] == "warning"
        assert result["metadata"]["quality_flag"] == "provisional_data"

    def test_estimated_code_e_warning(self, monkeypatch, tmp_path):
        _make_session("test-qc-005", monkeypatch, tmp_path,
                      streamflow=_streamflow_slot(n_days=365 * 20, qual_codes=["A", "e"]))
        from ai_hydro.mcp.tools_validators import check_usgs_qualification_codes
        result = check_usgs_qualification_codes("test-qc-005")
        assert result["status"] == "warning"
        assert result["metadata"]["quality_flag"] == "estimated_data"

    def test_approved_data_pass(self, monkeypatch, tmp_path):
        _make_session("test-qc-006", monkeypatch, tmp_path,
                      streamflow=_streamflow_slot(n_days=365 * 20, qual_codes=["A"]))
        from ai_hydro.mcp.tools_validators import check_usgs_qualification_codes
        result = check_usgs_qualification_codes("test-qc-006")
        assert result["status"] == "pass"


# ---------------------------------------------------------------------------
# TestCheckRegulatedBasin
# ---------------------------------------------------------------------------

class TestCheckRegulatedBasin:
    def test_no_delineation_insufficient_data(self, monkeypatch, tmp_path):
        _make_session("test-rb-001", monkeypatch, tmp_path)
        from ai_hydro.mcp.tools_validators import check_regulated_basin
        result = check_regulated_basin("test-rb-001")
        assert result["status"] == "insufficient_data"
        assert result["metadata"]["quality_flag"] == "regulation_unknown"

    def test_regulated_true_flag_warning(self, monkeypatch, tmp_path):
        delin = {"data": {"regulated": True}, "meta": {}}
        _make_session("test-rb-002", monkeypatch, tmp_path, delineation=delin)
        from ai_hydro.mcp.tools_validators import check_regulated_basin
        result = check_regulated_basin("test-rb-002")
        assert result["status"] == "warning"
        assert result["severity"] == "high"
        assert result["metadata"]["quality_flag"] == "regulated_basin"

    def test_regulated_false_flag_pass(self, monkeypatch, tmp_path):
        delin = {"data": {"regulated": False}, "meta": {}}
        _make_session("test-rb-003", monkeypatch, tmp_path, delineation=delin)
        from ai_hydro.mcp.tools_validators import check_regulated_basin
        result = check_regulated_basin("test-rb-003")
        assert result["status"] == "pass"

    def test_validator_key_correct(self, monkeypatch, tmp_path):
        _make_session("test-rb-004", monkeypatch, tmp_path)
        from ai_hydro.mcp.tools_validators import check_regulated_basin
        result = check_regulated_basin("test-rb-004")
        assert result["validator"] == "regulated_basin"

    def test_signatures_slot_also_checked(self, monkeypatch, tmp_path):
        sigs = {"data": {"regulated": True}, "meta": {}}
        _make_session("test-rb-005", monkeypatch, tmp_path, signatures=sigs)
        from ai_hydro.mcp.tools_validators import check_regulated_basin
        result = check_regulated_basin("test-rb-005")
        assert result["status"] == "warning"


# ---------------------------------------------------------------------------
# TestCheckStationarity
# ---------------------------------------------------------------------------

class TestCheckStationarity:
    def test_no_streamflow_insufficient_data(self, monkeypatch, tmp_path):
        _make_session("test-stat-001", monkeypatch, tmp_path)
        from ai_hydro.mcp.tools_validators import check_stationarity
        result = check_stationarity("test-stat-001")
        assert result["status"] == "insufficient_data"

    def test_no_arrays_insufficient_data(self, monkeypatch, tmp_path):
        sf = {"data": {"n_days": 100}, "meta": {}}  # no dates/q_cms
        _make_session("test-stat-002", monkeypatch, tmp_path, streamflow=sf)
        from ai_hydro.mcp.tools_validators import check_stationarity
        result = check_stationarity("test-stat-002")
        assert result["status"] == "insufficient_data"

    def test_too_few_years_insufficient_data(self, monkeypatch, tmp_path):
        _make_session("test-stat-003", monkeypatch, tmp_path,
                      streamflow=_streamflow_slot(n_days=365 * 3))
        from ai_hydro.mcp.tools_validators import check_stationarity
        result = check_stationarity("test-stat-003")
        assert result["status"] == "insufficient_data"

    def test_stationary_series_pass(self, monkeypatch, tmp_path):
        pytest.importorskip("scipy")
        _make_session("test-stat-004", monkeypatch, tmp_path,
                      streamflow=_stationary_streamflow_with_file(25, tmp_path))
        from ai_hydro.mcp.tools_validators import check_stationarity
        result = check_stationarity("test-stat-004")
        assert result["status"] == "pass"
        assert "mann_kendall_tau" in result["metadata"]

    def test_trending_series_warning(self, monkeypatch, tmp_path):
        pytest.importorskip("scipy")
        _make_session("test-stat-005", monkeypatch, tmp_path,
                      streamflow=_trending_streamflow_with_file(25, tmp_path))
        from ai_hydro.mcp.tools_validators import check_stationarity
        result = check_stationarity("test-stat-005")
        assert result["status"] == "warning"
        assert result["severity"] == "high"
        assert result["metadata"]["quality_flag"] == "non_stationary"
        assert result["metadata"]["p_value"] < 0.05

    def test_validator_key_correct(self, monkeypatch, tmp_path):
        _make_session("test-stat-006", monkeypatch, tmp_path)
        from ai_hydro.mcp.tools_validators import check_stationarity
        result = check_stationarity("test-stat-006")
        assert result["validator"] == "stationarity"
