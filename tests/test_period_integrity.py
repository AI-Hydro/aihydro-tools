"""T1-P (tool audit D07-D12): period and statistics correctness at the tool layer.

No network, no real session store. Numbers are synthetic and hand-checkable.
The D08/D09/D26 kernels live in aihydro-watershed (``signatures.periods``); the tool-level
tests that need its new fields skip when an older aihydro-watershed is installed.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import ai_hydro.mcp  # noqa: F401  (registers every tool)
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession

try:
    import aihydro_watershed.signatures.periods  # noqa: F401
    HAS_PERIODS = True
except ImportError:                                     # pragma: no cover - older aihydro-watershed
    HAS_PERIODS = False
needs_periods = pytest.mark.skipif(not HAS_PERIODS, reason="needs aihydro-watershed with signatures.periods")

SID = "t1p-period"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)


def _iso(d):
    return [f"{x:%Y-%m-%d}" for x in d]


# ---------------------------------------------------------------- D07: spectral-index trend

def test_failed_periods_are_recorded_and_the_tool_reports_requested_versus_served():
    """12 monthly periods asked, 3 fail: the result says so and the slope is per YEAR."""
    import asyncio
    from datetime import date
    from ai_hydro.mcp.tools_indices import _monthly_periods, compute_spectral_index
    import xarray as xr

    periods = _monthly_periods("2021-01-01", "2021-12-31")
    failed = {2, 3, 4}
    stats = []
    for k, (a, b) in enumerate(periods):
        if k in failed:
            continue
        mid = (date.fromisoformat(a).toordinal() + date.fromisoformat(b).toordinal()) / 2.0
        stats.append({"period": a, "period_end": b, "mean": 0.12 * mid / 365.2425, "n_pixels": 10})
    fake = {"index_da": xr.DataArray(np.full((4, 4), 0.4, dtype="float32"), dims=("y", "x")),
            "time_axis": [r["period"] for r in stats], "period_stats": stats,
            "period_failures": [{"period": periods[k][0], "period_end": periods[k][1], "reason": "no scenes"}
                                for k in sorted(failed)],
            "n_periods_requested": 12}
    sess = MagicMock()
    sess.get.return_value = None
    sess.watershed = {"geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}}
    fake_registry = {"NDVI": {"fn": None, "bands": ["red", "nir"], "colormap": "RdYlGn", "range": (-1, 1),
                              "citation": "x", "use_case": "x", "threshold_hint": 0.4}}
    import types, sys
    mod = types.ModuleType("aihydro_data.transforms.indices")
    mod.INDEX_REGISTRY = fake_registry
    mod.SENSOR_BAND_MAPS = {}
    with patch.dict(sys.modules, {"aihydro_data.transforms.indices": mod}), \
         patch("ai_hydro.mcp.helpers._resolve_session", return_value="s"), \
         patch("ai_hydro.mcp.helpers._maybe_set_workspace"), \
         patch("ai_hydro.session.HydroSession.load", return_value=sess), \
         patch("ai_hydro.mcp.tools_analysis._resolve_session_geometry", return_value=sess.watershed["geometry"]), \
         patch("ai_hydro.mcp.tools_indices._fetch_and_compute_index", return_value=fake), \
         patch("ai_hydro.mcp.helpers._canonical_prefix", return_value="s_index_ndvi"), \
         patch("ai_hydro.mcp.helpers._session_store"):
        r = asyncio.run(compute_spectral_index(index_name="NDVI", session_id="s", start="2021-01-01",
                                               end="2021-12-31", frequency="monthly", create_map=False))
    assert "error" not in r, r
    assert r["n_periods_requested"] == 12 and r["n_periods_served"] == 9
    assert [f["period"] for f in r["periods_failed"]] == ["2021-03-01", "2021-04-01", "2021-05-01"]
    assert len(r["time_axis"]) == 9
    assert r["trend_slope_per_year"] == pytest.approx(0.12, rel=1e-9)        # was 0.01-ish "per step"
    assert "Theil-Sen" in r["trend"]["estimator"] and r["trend"]["unit"] == "index units per year"


def test_fetch_loop_records_each_failed_period(monkeypatch):
    import types, sys
    import xarray as xr
    from ai_hydro.mcp import tools_indices as ti

    calls = []

    def fetch(**kw):
        calls.append(kw["start"])
        if kw["start"] in ("2021-02-01", "2021-03-01"):
            raise RuntimeError("no scenes")
        da = xr.DataArray(np.full((3, 3), 0.5), dims=("y", "x"))
        return types.SimpleNamespace(data=da)

    fake_data = types.ModuleType("aihydro_data")
    fake_data.fetch = fetch
    tr = types.ModuleType("aihydro_data.transforms.indices")
    tr.compute_index = lambda *a, **k: None
    tr.INDEX_REGISTRY = {}
    from shapely.geometry import box
    with patch.dict(sys.modules, {"aihydro_data": fake_data, "aihydro_data.transforms.indices": tr}):
        out = ti._fetch_and_compute_index(idx_key="NDVI", sensor="sentinel2", required_bands=[], watershed=box(0, 0, 1, 1),
                                          start="2021-01-01", end="2021-04-30", frequency="monthly",
                                          apply_cloud_mask=False, reg_entry={})
    assert out["n_periods_requested"] == 4
    assert [r["period"] for r in out["period_stats"]] == ["2021-01-01", "2021-04-01"]
    assert [f["period"] for f in out["period_failures"]] == ["2021-02-01", "2021-03-01"]


# ---------------------------------------------------------------- D08 / D09 / D26 through the tools

def _forcing_session(precip, dates, **extra):
    s = HydroSession(SID)
    path = store.write_session_data_file(SID, "forcing_t1p.json", {
        "dates": _iso(dates), "prcp_mm": [None if np.isnan(v) else float(v) for v in precip], "n_days": len(dates)})
    s.set("forcing", {"data": {"n_days": len(dates), "_data_file": path}, "meta": {}})
    s.save()


@needs_periods
def test_drought_tool_excludes_the_partial_final_month_and_reports_it():
    from ai_hydro.mcp.tools_analysis import compute_drought_index
    d = pd.date_range("1990-01-01", "2019-12-03", freq="D")
    p = np.random.default_rng(1).gamma(0.5, 6, d.size)
    _forcing_session(p, d)
    r = compute_drought_index(SID, index="spi", scale_months=1)
    assert not r.get("error"), r
    assert [e["period"] for e in r["months_excluded"]] == ["2019-12"]
    assert r["latest"]["month"] == "2019-11"                        # was 2019-12 with -3.09
    assert r["min_month_fraction"] == 1.0 and r["n_months_complete"] == r["n_months"] - 1


@needs_periods
def test_flood_tool_uses_complete_water_years_and_states_the_convention():
    from ai_hydro.mcp.tools_analysis import compute_flood_frequency
    d = pd.date_range("2000-07-15", "2015-01-03", freq="D")
    q = np.full(d.size, 5.0)
    for k, y in enumerate(range(2001, 2015)):
        q[np.where(d == f"{y}-03-10")[0][0]] = 100.0 + 10 * k
    s = HydroSession(SID)
    path = store.write_session_data_file(SID, "sf_t1p.json", {
        "dates": _iso(d), "q_cms": q.tolist(), "interval": "daily",
        "start_date": "2000-07-15", "end_date": "2015-01-03"})
    s.set("streamflow", {"data": {"interval": "daily", "_data_file": path}, "meta": {}})
    s.save()
    r = compute_flood_frequency(SID, distribution="gumbel")
    assert not r.get("error"), r
    assert r["n_years"] == 14 and r["years"] == list(range(2001, 2015))
    assert [e["period"] for e in r["excluded_years"]] == [2000, 2015]
    assert r["year_type"] == "water_year" and "daily mean" in r["statistic"]


# ---------------------------------------------------------------- D10 (tool-side GEE copy)

def test_tools_gee_timeseries_passes_an_exclusive_end(monkeypatch):
    import ai_hydro.gee.timeseries as gt
    log: dict = {}

    class Coll:
        def __init__(self, d): pass
        def filterDate(self, a, b): log["fd"] = (a, b); return self
        def select(self, b): return self
        def map(self, f): return self

    class FC:
        def __init__(self, c): pass
        def getInfo(self):
            a, b = log["fd"]
            return {"features": [{"properties": {"date": f"{x:%Y-%m-%d}", "value": 2.0}}
                                 for x in pd.date_range(a, b, inclusive="left")]}

    ee = type("ee", (), {"Initialize": staticmethod(lambda **k: None), "Geometry": staticmethod(lambda g: g),
                         "ImageCollection": Coll, "FeatureCollection": FC,
                         "Reducer": type("R", (), {"mean": staticmethod(lambda: 1)})})
    monkeypatch.setattr(gt, "_import_ee", lambda: (True, ee, None))
    roi = {"type": "Point", "coordinates": [0, 0]}
    r = gt.extract_timeseries(dataset_id="d", band="b", start_date="2020-01-01", end_date="2020-01-31", roi_geojson=roi)
    assert log["fd"] == ("2020-01-01", "2020-02-01") and len(r["rows"]) == 31 and r["rows"][-1]["date"] == "2020-01-31"
    r = gt.extract_timeseries(dataset_id="d", band="b", start_date="2020-01-15", end_date="2020-01-15", roi_geojson=roi)
    assert [x["date"] for x in r["rows"]] == ["2020-01-15"]


def test_tools_copy_of_filter_end_matches_the_data_package():
    from ai_hydro.gee.dates import filter_end
    for end in ("2020-01-31", "2020-02-28", "2019-12-31", "2020-01-15T00:00:00", "2020-01-15T12:30:00"):
        assert filter_end(end) == {"2020-01-31": "2020-02-01", "2020-02-28": "2020-02-29",
                                   "2019-12-31": "2020-01-01", "2020-01-15T00:00:00": "2020-01-16",
                                   "2020-01-15T12:30:00": "2020-01-15T12:30:00"}[end]
    try:
        from aihydro_data.sources._gee_vendored.dates import filter_end as theirs
    except ImportError:
        return
    assert all(filter_end(e) == theirs(e) for e in ("2020-01-31", "2024-02-28", "2019-12-31"))


# ---------------------------------------------------------------- D11: forcing

def test_strip_keeps_warnings_variables_and_failed_records_but_not_arrays():
    from ai_hydro.mcp.helpers import _strip_forcing_arrays
    data = {"dates": ["2020-01-01"] * 4, "prcp_mm": [1.0, None, 3.0, None], "n_days": 4,
            "variables": ["prcp_mm", "pet_mm"], "_warnings": ["pet: ALL_BACKENDS_FAILED", "tmax: quota"],
            "variables_failed": [{"variable": "pet", "reason": "ALL_BACKENDS_FAILED"}], "product": "X"}
    c = _strip_forcing_arrays(data)
    assert c["variables"] == ["prcp_mm", "pet_mm"]
    assert c["_warnings"] == ["pet: ALL_BACKENDS_FAILED", "tmax: quota"]
    assert c["variables_failed"] == [{"variable": "pet", "reason": "ALL_BACKENDS_FAILED"}]
    assert "prcp_mm" not in c and "dates" not in c
    assert c["prcp_mm_mean"] == 2.0                                       # mean of the two finite days
    assert c["variable_coverage"]["prcp_mm"] == {"n_days": 4, "n_finite": 2, "n_missing": 2}
    assert "variable_coverage" in c and "variables_failed" not in c["variable_coverage"]


def test_forcing_failed_variables_leave_a_record_and_nan_days_are_not_zero(monkeypatch):
    from ai_hydro.data import forcing as fo
    d = pd.date_range("2020-01-01", "2020-12-31", freq="D")                 # 366 days
    pr = np.full(d.size, 3.0); pr[:183] = np.nan                            # half the year missing
    frames = {"precipitation": pd.DataFrame({"date": d, "precipitation": pr})}

    def fetch(var, geom, start, end, **kw):
        import types
        if var in frames:
            return types.SimpleNamespace(data=frames[var].copy(), product="CHIRPS", source="gee", units="mm/day",
                                         coverage_complete=False, days_missing_head=0, days_missing_tail=0,
                                         days_missing_internal=183, missing_ranges=[{"start": "2020-01-01"}],
                                         notes=["COVERAGE INCOMPLETE"])
        raise RuntimeError("ALL_BACKENDS_FAILED")

    monkeypatch.setattr(fo, "_adata_fetch", fetch, raising=False)
    import aihydro_data
    monkeypatch.setattr(aihydro_data, "fetch", fetch)
    geom = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    res = fo.fetch_forcing_data_routed(geom, "2020-01-01", "2020-12-31", variables=["pr", "pet"])
    d_ = res.data
    assert d_["mean_annual_precip_mm"] == pytest.approx(3.0 * 365.25, abs=0.01)   # was ~half (543-style bias)
    assert d_["precip_n_missing_days"] == 183 and d_["precip_n_finite_days"] == 183
    failed = {f["variable"] for f in d_["variables_failed"]}
    assert failed == {"pet"} and all("ALL_BACKENDS_FAILED" in f["reason"] for f in d_["variables_failed"])
    assert d_["forcing_complete"] is False
    prov = d_["variable_provenance"]["prcp_mm"]
    assert prov["product"] == "CHIRPS" and prov["coverage_complete"] is False and prov["units"] == "mm/day"
    assert d_["_warnings"]


# ---------------------------------------------------------------- D12: sub-daily guard

def _sub_daily_slot(interval="hourly", n=35136):
    t = pd.date_range("2020-01-01", periods=n, freq="15min")
    return {"dates": [x.isoformat() for x in t], "q_cms": [1.0] * n, "interval": interval, "n_days": n}


@pytest.mark.parametrize("tool", ["separate_baseflow", "compute_flow_duration_curve", "compute_flood_frequency"])
def test_daily_tools_refuse_a_sub_daily_slot(tool):
    import ai_hydro.mcp.tools_analysis as ta
    s = HydroSession(SID)
    path = store.write_session_data_file(SID, "sf_sub_t1p.json", _sub_daily_slot())
    s.set("streamflow", {"data": {"_data_file": path}, "meta": {}})
    s.save()
    r = getattr(ta, tool)(SID)
    assert r["error"] is True and r["code"] == "STREAMFLOW_NOT_DAILY"
    assert r["interval"] == "hourly" and r["median_step_s"] == 900.0
    assert HydroSession.load(SID).get("baseflow") is None


def test_guard_catches_sub_daily_data_whatever_the_label_says():
    from ai_hydro.mcp.tools_analysis import _daily_streamflow_problem
    mislabelled = _sub_daily_slot(interval="monthly", n=500)               # interval=monthly -> get_continuous
    assert _daily_streamflow_problem(mislabelled, {}, "x")["code"] == "STREAMFLOW_NOT_DAILY"
    unlabelled = {k: v for k, v in _sub_daily_slot(n=500).items() if k != "interval"}
    assert _daily_streamflow_problem(unlabelled, {}, "x")["code"] == "STREAMFLOW_NOT_DAILY"


def test_guard_passes_daily_and_unlabelled_legacy_slots():
    from ai_hydro.mcp.tools_analysis import _daily_streamflow_problem
    t = pd.date_range("2020-01-01", periods=400, freq="D")
    daily = {"dates": _iso(t), "q_cms": [1.0] * 400, "interval": "daily"}
    assert _daily_streamflow_problem(daily, {}, "x") is None
    assert _daily_streamflow_problem({"q_cms": [1.0] * 400}, {}, "x") is None       # nothing to check
    assert _daily_streamflow_problem({"dates": _iso(t)}, {"data": {"interval": "daily"}}, "x") is None


def test_fetch_streamflow_refuses_an_unknown_interval():
    from ai_hydro.core import ToolError
    from ai_hydro.data.streamflow import fetch_streamflow_data
    with pytest.raises(ToolError) as e:
        fetch_streamflow_data("01013500", "2020-01-01", "2020-01-02", interval="monthly")
    assert e.value.code == "INVALID_INTERVAL"
