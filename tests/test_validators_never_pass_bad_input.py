"""General rule: a validator never passes on a missing or non-physical input.

Sweep of ai_hydro/mcp/tools_validators.py (D4 follow-up). Each case below was a
silent PASS before the sweep.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ai_hydro.mcp import tools_validators as tv
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession


@pytest.fixture
def sess(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)

    def make(sid="v-sweep", **slots):
        s = HydroSession(sid)
        for k, v in slots.items():
            s.set(k, v)
        s.save()
        return sid
    return make


# -- record_length: NaN n_days fell through every comparison to PASS ----------
@pytest.mark.parametrize("n", [float("nan"), float("inf"), -5, "9000", True])
def test_record_length_bad_n_days(sess, n):
    sid = sess(streamflow={"data": {"n_days": n}})
    assert tv.check_record_length(sid)["status"] == "insufficient_data"


def test_record_length_good_still_passes(sess):
    assert tv.check_record_length(sess(streamflow={"data": {"n_days": 8000}}))["status"] == "pass"


# -- temporal_alignment: equal garbage / inverted dates passed -----------------
@pytest.mark.parametrize("a,b", [
    (("garbage", "garbage"), ("garbage", "garbage")),
    (("2010-01-01", "2000-01-01"), ("2010-01-01", "2000-01-01")),
])
def test_temporal_alignment_bad_dates(sess, a, b):
    sid = sess(a={"meta": {"params": {"start_date": a[0], "end_date": a[1]}}},
               b={"meta": {"params": {"start_date": b[0], "end_date": b[1]}}})
    assert tv.check_temporal_alignment(sid, "a", "b")["status"] == "insufficient_data"


# -- usgs_qualification_codes: unreadable codes were "approved" ----------------
@pytest.mark.parametrize("codes", ["P", ["A", "Z"], [1, 2], {"x": 1}, ["  "]])
def test_qualification_codes_unreadable_never_pass(sess, codes):
    sid = sess(streamflow={"data": {"qualification_codes": codes}, "meta": {}})
    res = tv.check_usgs_qualification_codes(sid)
    assert res["status"] != "pass", (codes, res)


def test_qualification_codes_approved_passes_and_provisional_warns(sess):
    assert tv.check_usgs_qualification_codes(
        sess("q1", streamflow={"data": {"qualification_codes": ["A"]}, "meta": {}}))["status"] == "pass"
    assert tv.check_usgs_qualification_codes(
        sess("q2", streamflow={"data": {"qualification_codes": ["A", "P"]}, "meta": {}}))["status"] == "warning"


# -- stationarity: NaN discharge made p = nan, which PASSED --------------------
def _flow_slot(values, tmp_path, start="1990-10-01"):
    import json
    idx = pd.date_range(start, periods=len(values), freq="D")
    f = tmp_path / "q.json"            # large arrays are stripped from the slot on save
    f.write_text(json.dumps({"dates": [d.strftime("%Y-%m-%d") for d in idx],
                             "q_cms": [None if v is None else float(v) for v in values]}))
    return {"data": {"n_days": len(values), "_data_file": str(f)}, "meta": {}}


def test_stationarity_undefined_statistic_not_pass(sess, tmp_path, monkeypatch):
    import scipy.stats
    monkeypatch.setattr(scipy.stats, "kendalltau", lambda x, y: (float("nan"), float("nan")))
    sid = sess(streamflow=_flow_slot([5.0] * (365 * 12), tmp_path))
    res = tv.check_stationarity(sid)
    assert res["status"] == "insufficient_data" and "not defined" in res["message"]


def test_stationarity_nan_and_negative_discharge_treated_as_missing(sess, tmp_path):
    vals = list(np.random.default_rng(0).uniform(1, 9, 365 * 12))
    vals[10] = float("nan")
    vals[20] = -3.0
    res = tv.check_stationarity(sess(streamflow=_flow_slot(vals, tmp_path)))
    assert res["status"] in ("pass", "warning")
    assert "nan" not in res["message"].lower()


# -- uncertainty_present: an empty _uncertainty block passed -------------------
def test_uncertainty_empty_block_is_not_uncertainty(sess):
    sid = sess(signatures={"data": {"q_mean": 1.0, "_uncertainty": {}}})
    assert tv.check_uncertainty_present(sid)["status"] == "warning"
    sid = sess("u2", signatures={"data": {"q_mean": 1.0, "_uncertainty": {"q_mean": {"lo": 1, "hi": 2}}}})
    assert tv.check_uncertainty_present(sid)["status"] == "pass"


# -- water balance reads the flat sealed key too -------------------------------
def test_water_balance_flat_precipitation_status(sess):
    sid = sess(signatures={"data": {"runoff_ratio": 0.5, "precipitation_status": "rejected"}})
    assert tv.check_water_balance_consistency(sid)["status"] == "insufficient_data"
    sid = sess("wb2", signatures={"data": {"runoff_ratio": 0.5, "precipitation_status": "used"}})
    assert tv.check_water_balance_consistency(sid)["status"] == "pass"
