"""T1-P (tool audit D07-D12): period and statistics correctness at the tool layer.

No network, no real session store. Numbers are synthetic and hand-checkable.
The D08/D09/D26 kernels live in aihydro-watershed >= 0.3 (``signatures.periods``).
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

SID = "t1p-period"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)


def _iso(d):
    return [f"{x:%Y-%m-%d}" for x in d]


# ---------------------------------------------------------------- D07: spectral-index trend

def test_failed_periods_are_recorded_and_the_tool_reports_requested_versus_served():
    """36 monthly periods asked, 3 fail: the result says so and the slope is per YEAR."""
    import asyncio
    from datetime import date
    from ai_hydro.mcp.tools_indices import _monthly_periods, compute_spectral_index
    import xarray as xr

    periods = _monthly_periods("2021-01-01", "2023-12-31")
    failed = {2, 15, 28}            # Mar 2021, Apr 2022, May 2023: every calendar month keeps two or more values
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
            "n_periods_requested": 36}
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
                                               end="2023-12-31", frequency="monthly", create_map=False))
    assert "error" not in r, r
    assert r["n_periods_requested"] == 36 and r["n_periods_served"] == 33
    assert [f["period"] for f in r["periods_failed"]] == ["2021-03-01", "2022-04-01", "2023-05-01"]
    assert len(r["time_axis"]) == 33
    assert r["trend_slope_per_year"] == pytest.approx(0.12, rel=1e-9)        # was 0.01-ish "per step"
    assert "Sen" in r["trend"]["estimator"] and r["trend"]["unit"] == "index units per year"


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


def test_drought_tool_excludes_the_partial_final_month_and_reports_it():
    from ai_hydro.mcp.tools_analysis import compute_drought_index
    d = pd.date_range("1990-01-01", "2019-12-03", freq="D")
    p = np.random.default_rng(1).gamma(0.5, 6, d.size)
    _forcing_session(p, d)
    r = compute_drought_index(SID, index="spi", scale_months=1)
    assert not r.get("error"), r
    assert [e["period"] for e in r["months_excluded"]] == ["2019-12"]
    assert r["latest"]["month"] == "2019-11"                        # was 2019-12 with -3.09
    assert r["min_month_fraction"] == 1.0 and r["n_months_complete"] == r["n_months"] == 359 and r["n_months_in_span"] == 360


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


def _fetch_stub(frames, failing=()):
    import types

    def fetch(var, geom, start, end, **kw):
        if var in frames:
            fr = frames[var]
            return types.SimpleNamespace(data=fr.copy(), product="CHIRPS", source="gee", units="mm/day",
                                         coverage_complete=False, days_missing_head=0, days_missing_tail=0,
                                         days_missing_internal=int(fr.iloc[:, 1].isna().sum()),
                                         missing_ranges=[{"start": "2020-01-01"}], notes=["COVERAGE INCOMPLETE"])
        raise RuntimeError("ALL_BACKENDS_FAILED")
    return fetch


GEOM = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}


def test_forcing_failed_variables_leave_a_record_and_nan_days_are_not_zero(monkeypatch):
    from ai_hydro.data import forcing as fo
    import aihydro_data
    d = pd.date_range("2020-01-01", "2020-12-31", freq="D")
    pr = np.full(d.size, 3.0); pr[::2] = np.nan                              # every other day missing
    fetch = _fetch_stub({"precipitation": pd.DataFrame({"date": d, "precipitation": pr})})
    monkeypatch.setattr(fo, "_adata_fetch", fetch, raising=False)
    monkeypatch.setattr(aihydro_data, "fetch", fetch)
    res = fo.fetch_forcing_data_routed(GEOM, "2020-01-01", "2020-12-31", variables=["pr", "pet"])
    d_ = res.data
    assert d_["mean_annual_precip_mm"] == pytest.approx(3.0 * 365.25, abs=0.01)   # old rule: about half
    assert d_["precip_n_missing_days"] == 183 and d_["precip_n_finite_days"] == 183
    assert {f["variable"] for f in d_["variables_failed"]} == {"pet"}
    assert d_["forcing_complete"] is False and d_["variables_with_missing_days"] == {"prcp_mm": 183}
    prov = d_["variable_provenance"]["prcp_mm"]
    assert prov["product"] == "CHIRPS" and prov["coverage_complete"] is False and prov["units"] == "mm/day"
    assert d_["_warnings"]


def test_forcing_complete_is_false_for_nan_cells_even_when_the_provider_reports_coverage(monkeypatch):
    import types
    from ai_hydro.data import forcing as fo
    import aihydro_data
    d = pd.date_range("2020-01-01", "2020-12-31", freq="D")
    pr = np.full(d.size, 3.0); pr[10] = np.nan

    def fetch(var, geom, start, end, **kw):
        return types.SimpleNamespace(data=pd.DataFrame({"date": d, "precipitation": pr}), product="CHIRPS", source="gee",
                                     units="mm/day", coverage_complete=True, days_missing_head=0, days_missing_tail=0,
                                     days_missing_internal=0, missing_ranges=[], notes=[])
    monkeypatch.setattr(fo, "_adata_fetch", fetch, raising=False)
    monkeypatch.setattr(aihydro_data, "fetch", fetch)
    res = fo.fetch_forcing_data_routed(GEOM, "2020-01-01", "2020-12-31", variables=["pr"])
    assert res.data["forcing_complete"] is False and res.data["variables_with_missing_days"] == {"prcp_mm": 1}


def _season_flux(index, wet=5.0, dry=0.1):
    return pd.Series(np.where(index.month.isin([10, 11, 12, 1, 2, 3]), wet, dry), index=index)


def test_mean_annual_total_is_not_biased_by_a_missing_season():
    from ai_hydro.data.forcing import _mean_annual_total
    d = pd.date_range("2001-01-01", "2010-12-31")
    s = _season_flux(d)
    true = float(s.sum() / (len(d) / 365.25))
    full, why = _mean_annual_total(d, s)
    assert why is None and full == pytest.approx(true, rel=2e-3)             # monthly climatology of a complete record
    # a random 30% of days missing: unbiased (each calendar month keeps its own mean)
    gap = s.copy(); gap[np.random.default_rng(1).random(d.size) < 0.3] = np.nan
    v, why = _mean_annual_total(d, gap)
    assert why is None and v == pytest.approx(true, rel=0.01)
    # every day of June-September missing: no honest annual total, not a number from the wet months
    dry_missing = s.copy(); dry_missing[d.month.isin([6, 7, 8, 9])] = np.nan
    v, why = _mean_annual_total(d, dry_missing)
    assert v is None and "[6, 7, 8, 9]" in why
    # one dry season missing in a ten-year record: other years supply those months
    one = s.copy(); one[(d.year == 2005) & d.month.isin([6, 7, 8, 9])] = np.nan
    assert _mean_annual_total(d, one)[0] == pytest.approx(true, rel=2e-3)


def test_aridity_index_uses_the_same_basis_for_precipitation_and_pet(monkeypatch):
    from ai_hydro.data import forcing as fo
    import aihydro_data
    d = pd.date_range("2001-01-01", "2002-12-31")
    p = _season_flux(d).values.copy(); p[(d.month >= 6) & (d.month <= 9)] = np.nan     # no June-Sep precipitation
    pet = np.where(d.month.isin([4, 5, 6, 7, 8, 9]), 6.0, 1.0)
    fetch = _fetch_stub({"precipitation": pd.DataFrame({"date": d, "precipitation": p}),
                         "pet": pd.DataFrame({"date": d, "pet": pet})})
    monkeypatch.setattr(fo, "_adata_fetch", fetch, raising=False)
    monkeypatch.setattr(aihydro_data, "fetch", fetch)
    res = fo.fetch_forcing_data_routed(GEOM, "2001-01-01", "2002-12-31", variables=["pr", "pet"]).data
    assert res["mean_annual_precip_mm"] is None and "[6, 7, 8, 9]" in res["mean_annual_precip_reason"]
    assert res["mean_annual_pet_mm"] is not None
    assert res["aridity_index"] is None and res["aridity_index_reason"]        # never PET-full / precip-partial


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


def test_signatures_refuses_a_sub_daily_slot_like_the_other_daily_tools(monkeypatch):
    import ai_hydro.mcp.tools_analysis as ta
    s = HydroSession(SID)
    s.site_id = "OWN-GAUGE"                                   # non-USGS: the slot is the only source
    path = store.write_session_data_file(SID, "sf_sig_t1p.json", _sub_daily_slot())
    s.set("streamflow", {"data": {"_data_file": path}, "meta": {}})
    poly = {"type": "Polygon", "coordinates": [[[-69, 46], [-68, 46], [-68, 47], [-69, 47], [-69, 46]]]}
    gpath = store.write_session_data_file(SID, "ws_t1p.geojson", poly)
    s.set("watershed", {"data": {"area_km2": 100.0, "gauge_id": "OWN-GAUGE", "geometry_geojson_path": gpath}, "meta": {}})
    s.save()
    fn = getattr(ta.extract_hydrological_signatures, "fn", ta.extract_hydrological_signatures)
    r = fn(SID, start_date="2020-01-01", end_date="2020-12-31")
    assert r.get("code") == "STREAMFLOW_NOT_DAILY", r
    assert r["median_step_s"] == 900.0


def test_every_tool_side_filterDate_goes_through_filter_end():
    import pathlib
    import ai_hydro
    root = pathlib.Path(ai_hydro.__file__).parent
    bad = [f"{p.relative_to(root)}:{n}" for p in root.rglob("*.py") if p.name != "dates.py"
           for n, line in enumerate(p.read_text().splitlines(), 1)
           if ".filterDate(" in line and "filter_end(" not in line]
    assert not bad, bad


def test_drought_tool_passes_the_source_calendar_for_gridmet(monkeypatch):
    """GridMET has no 31 Dec in leap years: the tool tells the kernel, so leap Decembers are not dropped."""
    from ai_hydro.mcp.tools_analysis import compute_drought_index, _source_calendar
    d = pd.date_range("1989-10-01", "2019-09-30", freq="D")
    d = d[~((d.month == 12) & (d.day == 31) & d.is_leap_year)]
    p = np.random.default_rng(1).gamma(0.5, 6, d.size)
    s = HydroSession(SID)
    path = store.write_session_data_file(SID, "forcing_gm_t1p.json", {
        "dates": _iso(d), "prcp_mm": p.tolist(), "n_days": len(d), "product": "GRIDMET_PRECIP",
        "variable_provenance": {"prcp_mm": {"product": "GRIDMET_PRECIP"}}})
    s.set("forcing", {"data": {"n_days": len(d), "_data_file": path}, "meta": {}})
    s.save()
    assert _source_calendar({"product": "GRIDMET_PRECIP"}) == "no_leap_dec31"
    assert _source_calendar({"product": "ERA5L_PRECIP | CHIRPS"}) is None                 # not a single product
    assert _source_calendar({}) is None and _source_calendar({"product": "GRIDMET_PRECIP | GRIDMET_PET"}) is None
    # CHIRPS precipitation with GridMET PET: SPEI must NOT forgive a real CHIRPS gap on 31 Dec
    mix = {"variable_provenance": {"prcp_mm": {"product": "CHIRPS"}, "pet_mm": {"product": "GRIDMET_PET"}}}
    assert _source_calendar(mix, ("prcp_mm", "pet_mm")) is None
    assert _source_calendar(mix, ("prcp_mm",)) is None                                    # SPI reads only precipitation
    assert _source_calendar(mix, ("pet_mm",)) == "no_leap_dec31"
    both = {"variable_provenance": {"prcp_mm": {"product": "GRIDMET_PRECIP"}, "pet_mm": {"product": "GRIDMET_PET"}}}
    assert _source_calendar(both, ("prcp_mm", "pet_mm")) == "no_leap_dec31"
    assert _source_calendar({"variable_provenance": {"pet_mm": {"product": "GRIDMET_PET"}}}, ("prcp_mm", "pet_mm")) is None
    r = compute_drought_index(SID, index="spi", scale_months=12)
    assert not r.get("error"), r
    assert r["months_excluded"] == [] and r["source_calendar"] == "no_leap_dec31"
    assert len(r["months_calendar_adjusted"]) == 7


def test_locked_q100_fixture_partial_years_inflate_the_design_flood():
    """Reproducible recipe (audit probe p7): the numbers quoted in the CHANGELOG come from this test.

    Record 2000-07-15..2015-01-03, base flow 5 m3/s, one March flood a year 2001-2014 drawn from
    Gumbel(300, 60) with rng seed 0. The pre-fix grouping (calendar years, no completeness rule) enters
    the partial 2000 and 2015 as annual maxima of 5.0.
    """
    from aihydro_watershed.signatures.flood_frequency import annual_maxima, flood_frequency
    d = pd.date_range("2000-07-15", "2015-01-03", freq="D")
    rng = np.random.default_rng(0)
    q = np.full(d.size, 5.0)
    for y in range(2001, 2015):
        idx = np.where((np.asarray(d.year) == y) & (np.asarray(d.month) == 3))[0]
        q[idx[10]] = rng.gumbel(300, 60)
    iso = _iso(d)

    def levels(am):
        r = flood_frequency(am["annual_max"], n_bootstrap=0)
        return {x["return_period"]: x["value"] for x in r["return_levels"] if x["return_period"] in (2.0, 100.0)}

    old = annual_maxima(q, dates=iso, year_type="calendar_year", min_fraction=0.001)
    new = annual_maxima(q, dates=iso)
    assert (old["n_years"], new["n_years"]) == (16, 14)
    assert levels(old)[100.0] == pytest.approx(988.1, abs=0.1) and levels(new)[100.0] == pytest.approx(688.2, abs=0.1)
    assert levels(old)[2.0] == pytest.approx(292.5, abs=0.1) and levels(new)[2.0] == pytest.approx(335.5, abs=0.1)


def test_monthly_basis_for_temperature_and_the_80_percent_year_rule():
    from ai_hydro.data.forcing import _mean_annual_temp, _mean_annual_total
    d = pd.date_range("2001-01-01", "2010-12-31")
    temp = pd.Series(10.0 + 10.0 * np.sin(2 * np.pi * (d.dayofyear.values - 110) / 365.25), index=d)
    full, why = _mean_annual_temp(d, temp)
    assert why is None and full == pytest.approx(10.0, abs=0.05)
    cold_missing = temp.copy(); cold_missing[d.month.isin([12, 1, 2])] = np.nan
    assert _mean_annual_temp(d, cold_missing)[0] is None                    # a plain mean would read warm
    assert round(float(cold_missing.mean()), 1) > 11.5
    # a month with data in 7 of 10 years (70%) is refused, in 8 of 10 (80%) it is accepted
    for have, ok in ((7, False), (8, True)):
        s = pd.Series(3.0, index=d)
        s[(d.month == 7) & (d.year > 2000 + have)] = np.nan
        v, reason = _mean_annual_total(d, s)
        assert (v is not None) is ok, (have, reason)
        if not ok:
            assert "[7]" in reason and "80%" in reason and "by analogy" in reason


def test_calculate_water_balance_metrics_uses_the_same_basis():
    from ai_hydro.data.forcing import calculate_water_balance_metrics
    d = pd.date_range("2001-01-01", "2010-12-31")
    pr = pd.Series(np.where(d.month.isin([10, 11, 12, 1, 2, 3]), 5.0, 0.1), index=d)
    pet = pd.Series(np.where(d.month.isin([4, 5, 6, 7, 8, 9]), 6.0, 1.0), index=d)
    full = pd.DataFrame({"date": d, "prcp_mm": pr.values, "pet_mm": pet.values})
    ok = calculate_water_balance_metrics(full)
    assert ok["mean_annual_precip_mm"] == pytest.approx(float(pr.sum() / (len(d) / 365.25)), rel=2e-3)
    gap = full.copy(); gap.loc[d.month.isin([6, 7, 8, 9]), "prcp_mm"] = np.nan
    r = calculate_water_balance_metrics(gap)
    assert r["mean_annual_precip_mm"] is None and "[6, 7, 8, 9]" in r["mean_annual_precip_reason"]
    assert r["aridity_index"] is None                                      # never PET-full / precip-partial


def test_year_start_month_is_exposed_on_the_flood_and_low_flow_tools():
    from ai_hydro.mcp.tools_analysis import compute_flood_frequency, compute_flow_duration_curve
    d = pd.date_range("1999-07-01", "2012-06-30", freq="D")                # 13 July-June years
    q = np.full(d.size, 5.0)
    for y in range(1999, 2012):
        q[np.where(d == f"{y + 1}-01-15")[0][0]] = 100.0 + y - 1999       # austral-summer flood
    s = HydroSession(SID)
    path = store.write_session_data_file(SID, "sf_sh_t1p.json", {"dates": _iso(d), "q_cms": q.tolist(), "interval": "daily",
                                                                  "start_date": "1999-07-01", "end_date": "2012-06-30"})
    s.set("streamflow", {"data": {"interval": "daily", "_data_file": path}, "meta": {}})
    s.save()
    r = compute_flood_frequency(SID, year_start_month=7)
    assert not r.get("error"), r
    assert r["year_type"] == "custom_year" and r["years"] == list(range(1999, 2012)) and r["excluded_years"] == []
    f = compute_flow_duration_curve(SID, year_start_month=7)
    assert f["low_flow_year_type"] == "custom_year" and f["low_flow_years"] == list(range(1999, 2012))
