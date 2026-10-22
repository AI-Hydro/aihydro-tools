"""Science Wave 3, PR-4b: validators resolve their series by digest; precipitation consumers key on reason codes;
`separate_baseflow` teaching error; the evidence compaction keeps interval scope. Offline, synthetic series."""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import ai_hydro.mcp  # noqa: F401  (registers every tool)
from ai_hydro.mcp.tools_validators import (
    BENIGN_PRECIPITATION_GAP_REASONS, check_regulated_basin, check_record_length, check_stationarity,
    check_usgs_qualification_codes, check_water_balance_consistency, precipitation_usable,
    run_signature_validators,
)
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from ai_hydro.validators import series_binding as sb

SID = "w3-binding"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)


def _served(start="1989-10-01", end="2009-09-30", *, qual=None, appr=None, trend=0.0, seed=1):
    idx = pd.date_range(start, end, freq="D")
    rng = np.random.default_rng(seed)
    q = 5.0 + 3.0 * np.abs(np.sin(np.arange(len(idx)) / 40.0)) + rng.uniform(0, 1, len(idx)) + trend * np.arange(len(idx))
    return {"dates": [f"{d:%Y-%m-%d}" for d in idx], "q_cms": [float(v) for v in q], "units": "m3/s",
            "qualifier_codes": qual(len(idx)) if callable(qual) else qual,
            "approval_codes": appr(len(idx)) if callable(appr) else appr}


def _signatures_session(served, *, gauge="01013500", record_digest=True, **data_over):
    """A session whose signatures slot retained ``served`` exactly as the tool does."""
    s = HydroSession(SID)
    s.site_id = gauge
    ret = sb.retain_served_series(SID, served)
    data = {"q_mean": 1.0, "flow_quantile_convention": "camels_nonexceedance_v1",
            "streamflow_digest": sb.series_digest(served["dates"], served["q_cms"]),
            "_data_file": ret["path"], **data_over}
    s.set("signatures", {"data": data, "meta": {"computed_at": "2026-10-01T00:00:00+00:00",
                                                 "params": {"gauge_id": gauge},
                                                 "retained_series": {"path": ret["path"], "digest": ret["digest"]}}})
    s.save()
    return ret


# ------------------------------------------------------------------ digest scheme + retention

def test_series_digest_matches_watersheds_scheme():
    from aihydro_watershed.signatures.signatures import _series_digest
    served = _served("2000-01-01", "2000-06-30")
    ser = pd.Series(served["q_cms"], index=pd.to_datetime(served["dates"]))
    ser.iloc[5] = np.nan
    served["q_cms"][5] = None
    assert sb.series_digest(served["dates"], served["q_cms"]) == _series_digest(ser)["digest"]


def test_retained_file_is_content_addressed_and_never_overwritten():
    a, b = _served(seed=1), _served(seed=2)
    ra, rb = sb.retain_served_series(SID, a), sb.retain_served_series(SID, b)
    assert ra["path"] != rb["path"] and Path(ra["path"]).is_file() and Path(rb["path"]).is_file()
    assert sb.retain_served_series(SID, a) == ra                       # same content, same file


def test_slot_health_ok_for_a_signatures_slot_with_retained_series():
    _signatures_session(_served(), **{"signature_definitions": {"q5": {}}, "_precipitation": {"status": "used",
                                                                                              "completeness": 1.0}})
    h = HydroSession.load(SID).slot_health()
    assert h["signatures"]["health"] == "ok", h


# ------------------------------------------------------------------ validators bound to the signatures series

def test_validators_judge_the_signatures_series_not_the_streamflow_slot():
    _signatures_session(_served())
    r = check_record_length(SID, source="signatures")
    assert r["status"] == "pass" and r["metadata"]["n_days"] == 7305
    assert r["metadata"]["series"]["start"] == "1989-10-01" and r["metadata"]["series"]["source"] == "signatures"
    assert check_stationarity(SID, source="signatures")["metadata"]["n_water_years"] == 20


def test_changed_retained_file_is_a_series_mismatch_not_a_validation():
    ret = _signatures_session(_served())
    p = Path(ret["path"])
    body = json.loads(p.read_text())
    body["q_cms"][10] = 999.0
    p.write_text(json.dumps(body))
    for fn in (check_record_length, check_usgs_qualification_codes, check_stationarity, check_regulated_basin):
        r = fn(SID, source="signatures")
        assert r["status"] == "insufficient_data", fn.__name__
        assert r["metadata"]["quality_flag"] == "series_mismatch" and r["metadata"]["series_mismatch"] is True


def test_series_that_is_not_the_signatures_digest_is_a_mismatch_even_with_an_intact_file():
    """The file digest is intact but the series is not the one the signatures recorded."""
    served = _served()
    ret = sb.retain_served_series(SID, served)
    s = HydroSession(SID)
    s.set("signatures", {"data": {"flow_quantile_convention": "x", "_data_file": ret["path"],
                                   "streamflow_digest": "sha256:" + "0" * 64},
                         "meta": {"params": {"gauge_id": "01013500"},
                                  "retained_series": {"path": ret["path"], "digest": ret["digest"]}}})
    s.save()
    r = check_record_length(SID, source="signatures")
    assert r["status"] == "insufficient_data" and r["metadata"]["quality_flag"] == "series_mismatch"
    assert "streamflow_digest" in r["message"]


def test_caller_digest_for_another_series_is_a_mismatch():
    _signatures_session(_served())
    other = sb.series_digest(*(lambda x: (x["dates"], x["q_cms"]))(_served(seed=9)))
    r = check_stationarity(SID, source="signatures", series_digest=other)
    assert r["status"] == "insufficient_data" and r["metadata"]["series_mismatch"] is True


def test_signatures_without_a_retained_series_say_not_retained_not_mismatch():
    s = HydroSession(SID)
    s.set("signatures", {"data": {"q_mean": 1.0}, "meta": {}})
    s.save()
    r = check_record_length(SID, source="signatures")
    assert r["status"] == "insufficient_data" and r["metadata"]["quality_flag"] == "series_not_retained"
    assert r["metadata"]["series_mismatch"] is False
    bad = check_record_length(SID, source="nonsense")
    assert bad["status"] == "insufficient_data" and bad["metadata"]["quality_flag"] == "unknown_source"


def test_ice_token_raises_the_other_qualifier_warning():
    served = _served(qual=lambda n: ["ICE" if i % 50 == 0 else ("e" if i % 7 == 0 else "") for i in range(n)],
                     appr=lambda n: ["A"] * n)
    _signatures_session(served)
    r = check_usgs_qualification_codes(SID, source="signatures")
    assert r["status"] == "warning" and r["metadata"]["quality_flag"] == "other_qualifiers"
    n_ice = sum(1 for i in range(len(served["dates"])) if i % 50 == 0)
    assert r["metadata"]["qualifiers_other"] == {"ICE": n_ice}


def test_provisional_and_estimated_and_clean_series_verdicts():
    n = 7305
    _signatures_session(_served(qual=[""] * n, appr=["A"] * n))
    assert check_usgs_qualification_codes(SID, source="signatures")["status"] == "pass"
    _signatures_session(_served(qual=["e"] * 10 + [""] * (n - 10), appr=["A"] * n))
    r = check_usgs_qualification_codes(SID, source="signatures")
    assert r["status"] == "warning" and r["metadata"]["quality_flag"] == "estimated_data"
    _signatures_session(_served(qual=[""] * n, appr=["A"] * (n - 30) + ["P"] * 30))
    r = check_usgs_qualification_codes(SID, source="signatures")
    assert r["metadata"]["quality_flag"] == "provisional_data" and r["metadata"]["approval_counts"]["P"] == 30


def test_series_without_codes_is_insufficient_data_not_pass():
    _signatures_session(_served())                                    # caller-supplied / modelled: no codes
    r = check_usgs_qualification_codes(SID, source="signatures")
    assert r["status"] == "insufficient_data" and r["metadata"]["quality_flag"] == "qualification_codes_absent"


def test_stationarity_uses_core_water_year_and_flags_a_trend():
    _signatures_session(_served(trend=0.002))
    r = check_stationarity(SID, source="signatures")
    assert r["status"] == "warning" and r["metadata"]["quality_flag"] == "non_stationary"
    assert (r["metadata"]["first_water_year"], r["metadata"]["last_water_year"]) == (1990, 2009)   # end-year labels


def test_stationarity_water_year_is_core_water_year_for_every_oct_boundary():
    from aihydro_core.science import water_year
    from ai_hydro.mcp.tools_validators import _stationarity_verdict
    dates = [f"{d:%Y-%m-%d}" for d in pd.date_range("2000-09-25", "2012-10-05", freq="D")]
    r = _stationarity_verdict(dates, [1.0 + (i % 5) for i in range(len(dates))], ["x"])
    wy = water_year(pd.DatetimeIndex(dates))
    complete = [y for y in sorted(set(wy)) if (wy == y).sum() >= 330]
    assert r["metadata"]["n_water_years"] == len(complete)


def test_regulated_basin_is_not_judged_for_another_basins_series():
    _signatures_session(_served(), gauge="01013500")
    s = HydroSession.load(SID)
    s.set("watershed", {"data": {"gauge_id": "01109000", "regulated": False}, "meta": {}})
    s.save()
    r = check_regulated_basin(SID, source="signatures")
    assert r["status"] == "insufficient_data" and r["metadata"]["series_mismatch"] is True
    s = HydroSession.load(SID)
    s.set("watershed", {"data": {"gauge_id": "01013500", "regulated": True}, "meta": {}})
    s.save()
    r = check_regulated_basin(SID, source="signatures")
    assert r["status"] == "warning" and r["metadata"]["quality_flag"] == "regulated_basin"


def test_run_signature_validators_reports_an_unresolved_series_in_each_validator():
    s = HydroSession(SID)
    s.save()
    b = sb.bound_series_problem(sb.SERIES_NOT_RETAINED, "nothing retained")
    out = run_signature_validators(SID, b, "01013500")
    assert [f["validator"] for f in out] == ["record_length", "usgs_qualification_codes", "stationarity",
                                              "regulated_basin"]
    assert all(f["status"] == "insufficient_data" and f["metadata"]["quality_flag"] == "series_not_retained"
               for f in out)


def test_default_streamflow_behaviour_is_unchanged():
    s = HydroSession(SID)
    s.set("streamflow", {"data": {"n_days": 8000, "q_cms": [1.0] * 10}, "meta": {}})
    s.save()
    assert check_record_length(SID)["status"] == "pass"
    assert check_record_length(SID)["metadata"] == {"n_days": 8000}


# ------------------------------------------------------------------ precipitation: reason codes, not status strings

@pytest.mark.parametrize("prec,usable", [
    ({"status": "used"}, True),
    ({"status": "used_with_gaps", "missing_ranges": [{"reason": "client_calendar_assumption", "days": 1}]}, True),
    ({"status": "used_with_gaps", "missing_ranges": [{"reason": "client_calendar_assumption"},
                                                      {"reason": "absent_in_response"}]}, False),
    ({"status": "used_with_gaps", "missing_ranges": []}, False),                    # unknown reason: not benign
    ({"status": "used_with_gaps", "missing_ranges": [{"reason": "client_calendar_assumption"}],
      "water_years_excluded": [{"water_year": 1990}]}, False),
    ({"status": "used_with_gaps", "missing_ranges": [{"reason": "client_calendar_assumption"}],
      "missing_ranges_truncated": True}, False),
    ({"status": "rejected", "reason": "no water year is complete"}, False),
    ({"status": "unavailable"}, False),
    (None, False),
])
def test_precipitation_usable_keys_on_reason_codes(prec, usable):
    assert precipitation_usable(prec)[0] is usable
    assert BENIGN_PRECIPITATION_GAP_REASONS == ("client_calendar_assumption",)


def _sig_with_precip(prec, rr=0.6):
    s = HydroSession(SID)
    s.set("signatures", {"data": {"runoff_ratio": rr, "_precipitation": prec, "precipitation_status": prec["status"]},
                         "meta": {}})
    s.save()


def test_water_balance_accepts_used_with_gaps_when_only_the_calendar_assumption_gap_is_present():
    _sig_with_precip({"status": "used_with_gaps", "reason": "1 day",
                      "missing_ranges": [{"start": "2000-12-31", "end": "2000-12-31", "days": 1,
                                          "reason": "client_calendar_assumption"}]})
    assert check_water_balance_consistency(SID)["status"] == "pass"


def test_water_balance_does_not_pass_on_used_with_gaps_for_other_reasons():
    _sig_with_precip({"status": "used_with_gaps", "reason": "gaps",
                      "missing_ranges": [{"start": "2000-05-01", "end": "2000-05-09", "days": 9,
                                          "reason": "absent_in_response"}]})
    r = check_water_balance_consistency(SID)
    assert r["status"] == "insufficient_data" and "absent_in_response" in r["message"]


# ------------------------------------------------------------------ evidence keeps interval scope

def test_run_row_evidence_keeps_the_scope_lists_of_an_interval_but_not_arrays():
    from ai_hydro.session.evidence import capture_result_evidence
    ci = {"value": 1.0, "ci_low": 0.9, "ci_high": 1.1, "scope": {"varies": ["water_year_sample"],
          "assumes": ["exchangeable_years"], "excludes": ["forcing_product", "rating_measurement"]},
          "samples": [0.1] * 50, "note": ["a", "b"]}
    ev = capture_result_evidence({"data": {"q_mean": 1.0, "_uncertainty": {"q_mean": ci}, "series": [1, 2, 3]}})
    got = ev["uncertainty"]["q_mean"]
    assert got["scope"]["excludes"] == ["forcing_product", "rating_measurement"]
    assert got["scope"]["varies"] == ["water_year_sample"]
    assert "samples" not in got                       # numeric arrays are still dropped
    assert "series" not in ev["data"]
    long = capture_result_evidence({"data": {"_uncertainty": {"m": {"scope": {"excludes": [f"t{i}" for i in range(40)]}}}}})
    assert long["uncertainty"]["m"]["scope"] == {}    # a long list is not a scope; dropped


# ------------------------------------------------------------------ separate_baseflow

def _baseflow_session(n, seed=3):
    rng = np.random.default_rng(seed)
    q = [float(v) for v in 3.0 + rng.uniform(0, 1, n)]
    s = HydroSession(SID)
    path = store.write_session_data_file(SID, "streamflow_baseflow_test.json", {"q_cms": q})   # lean session JSON
    s.set("streamflow", {"data": {"n_days": n, "_data_file": path}, "meta": {}})
    s.save()
    return q


def test_separate_baseflow_refuses_30_days_or_fewer_with_a_teaching_error():
    from ai_hydro.mcp.tools_analysis import separate_baseflow
    _baseflow_session(30)
    r = separate_baseflow(SID)
    assert r["error"] is True and r["code"] == "SERIES_TOO_SHORT_FOR_BASEFLOW" and r["n_days"] == 30
    t = r["teaching_error"]
    assert t["rule"] and t["explanation"] and t["how_to_fix"] and "_traceback" not in r
    assert HydroSession.load(SID).get("baseflow") is None                     # nothing stored
    _baseflow_session(31)
    assert not separate_baseflow(SID).get("error")


def test_separate_baseflow_records_warmup_bias_for_short_series_and_not_for_a_year():
    from ai_hydro.mcp.tools_analysis import separate_baseflow
    from aihydro_watershed.signatures.baseflow import warmup_bias_disclosure
    _baseflow_session(200)
    with warnings.catch_warnings():
        warnings.simplefilter("error")                       # the kernel's warning is disclosed, not leaked
        r = separate_baseflow(SID)
    assert r["warmup_bias"] == warmup_bias_disclosure(200) and r["warmup_bias"]["warmup_bias"] is True
    assert r["kernel"] == "lyne_hollick_ladson2013"
    assert HydroSession.load(SID).get("baseflow")["meta"]["warmup_bias"] == r["warmup_bias"]
    _baseflow_session(400)
    r = separate_baseflow(SID)
    assert "warmup_bias" not in r and "warmup_bias" not in HydroSession.load(SID).get("baseflow")["meta"]


def test_separate_baseflow_bfi_is_the_ladson_value():
    """01031500-like behaviour: the tool's BFI is the public kernel's, not the old clamped filter's."""
    from ai_hydro.mcp.tools_analysis import separate_baseflow
    from aihydro_watershed.signatures.baseflow import lyne_hollick_ladson2013
    q = _baseflow_session(900, seed=5)
    arr = np.asarray(q)
    expected = float(lyne_hollick_ladson2013(arr).sum() / arr.sum())
    assert separate_baseflow(SID)["bfi"] == pytest.approx(expected, abs=5e-5)


# ------------------------------------------------------------------ a slot with dropped days keeps its dates

def test_signatures_consume_a_gappy_slot_on_its_real_dates_not_as_consecutive_days(monkeypatch):
    """The streamflow slot drops days with no value, so its q_cms is not consecutive from start_date. A bare list
    would be read as consecutive days and shift every value after the gap onto the wrong date."""
    import aihydro_watershed.signatures.signatures as impl
    from aihydro_core.records import digest_bytes
    from ai_hydro.mcp.tools_analysis import extract_hydrological_signatures

    monkeypatch.setattr(impl, "_gauge_area_check", lambda *a, **k: {"area_check": "unavailable", "warnings": []})
    idx = pd.date_range("1999-10-01", "2001-09-30", freq="D")
    keep = np.ones(len(idx), bool)
    keep[[100, 101, 400]] = False                       # three days with no value
    idx = idx[keep]
    rng = np.random.default_rng(8)
    q = [float(v) for v in 4.0 + rng.uniform(0, 2, len(idx))]
    body = {"dates": [f"{d:%Y-%m-%d}" for d in idx], "q_cms": q, "units": "m3/s", "n_days": len(q)}
    path = store.write_session_data_file(SID, "streamflow_gappy.json", body)
    s = HydroSession(SID)
    s.site_id, s.site_type = "01013500", "usgs_gauge"
    s.set("watershed", {"data": {"area_km2": 250.0, "gauge_id": "01013500", "geometry_geojson": {
        "type": "Polygon", "coordinates": [[[-77.5, 39.2], [-77.4, 39.2], [-77.4, 39.3], [-77.5, 39.3], [-77.5, 39.2]]]}},
        "meta": {}})
    s.set("streamflow", {"data": {"n_days": len(q), "_data_file": path},
                         "meta": {"params": {"gauge_id": "01013500", "start_date": "1999-10-01", "end_date": "2001-09-30"},
                                  "retained_series": {"path": path, "digest": digest_bytes(Path(path).read_bytes())}}})
    s.save()
    out = extract_hydrological_signatures(session_id=SID, start_date="1999-10-01", end_date="2001-09-30",
                                          precipitation="skip")
    assert not out.get("error"), out
    served = json.loads(Path(out["meta"]["retained_series"]["path"]).read_text())
    assert served["dates"] == body["dates"]             # the slot's real dates, gaps included
    assert served["q_cms"] == q
    # and the slot's USGS codes (none here) are not invented
    assert served["qualifier_codes"] is None and served["approval_codes"] is None
    flags = {f["validator"]: f for f in out["quality_flags"]}
    assert flags["record_length"]["metadata"]["series"]["n_days"] == len(q)
    assert flags["usgs_qualification_codes"]["status"] == "insufficient_data"
