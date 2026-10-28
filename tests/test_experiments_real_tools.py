"""Multi-basin experiments through the REAL tools, offline (WP-1a).

Defects reproduced here (review-experiments-modelling-sessions-2026-10-19, E1-E5):

* E1  an experiment over several gauges returned the SESSION's basin for every row
      (unregistered ids fell back to ``__legacy__``; the cache key, the streamflow
      gauge and the area all came from the session), and reported ``complete``;
* E2  ``separate_baseflow`` experiments always failed (no ``feature`` argument);
* E3  default metric names that the tools never emit (two permanently empty columns);
* E4  rows served from the per-feature cache carried no run id;
* E5  a feature that matched no tracked metric vanished from the table.

Nothing here is mocked above the network. ``define_experiment`` / ``run_experiment`` /
``get_experiment_table`` and the real ``extract_hydrological_signatures`` run on a
scratch HOME. The only fakes are at the lowest layer: the USGS adapter's single
transport seam (``usgs_waterdata._http_get``) answers the site records from the recorded
fixtures and the daily values with a per-gauge SYNTHETIC series in the API's own response
shape (the labelled stand-in for network data: no recorded fixture covers three gauges
over one window). Precipitation is skipped (``precipitation="skip"``), so
``runoff_ratio`` / ``stream_elas`` are honestly null.
"""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd
import pytest

from ai_hydro.session import chat_binding, store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

from _usgs_replay import Replay, ok

SID = "exp-real-tools"
# 12 complete water years: aihydro-watershed 0.2.0 refuses an interval from fewer than 10 (the per-row CIs below
# are what shows rows do not share each other's numbers).
START, END = "1990-10-01", "2002-09-30"

# Three gauges whose site records are recorded fixtures (tests/fixtures/usgs_waterdata).
# SYNTHETIC daily discharge (ft^3/s): (base, amplitude, seed) differ per gauge.
GAUGES = {
    "01013500": (200.0, 150.0, 7),
    "01109000": (90.0, 40.0, 11),
    "03245500": (700.0, 400.0, 23),
}
# Drainage areas (km2) from the recorded site records, so the gauge-vs-polygon area check passes.
AREA = {"basin_a": 2253.29, "basin_b": 112.15, "basin_c": 3115.76}
GAUGE_AREA = {"01013500": 2253.29, "01109000": 112.15, "03245500": 3115.76}
SQUARE = {"type": "Polygon", "coordinates": [[[-77.5, 39.2], [-77.4, 39.2], [-77.4, 39.3],
                                              [-77.5, 39.3], [-77.5, 39.2]]]}


def _square(i: int) -> dict:
    d = 0.2 * i
    return {"type": "Polygon", "coordinates": [[[-77.5 + d, 39.2], [-77.4 + d, 39.2], [-77.4 + d, 39.3],
                                                [-77.5 + d, 39.3], [-77.5 + d, 39.2]]]}


def _daily_body(gauge: str, start: str, end: str) -> dict:
    """SYNTHETIC daily discharge in the Water Data API's response shape (ft^3/s strings)."""
    base, amp, seed = GAUGES[gauge]
    idx = pd.date_range(start, end, freq="D")
    rng = np.random.default_rng(seed)
    cfs = base + amp * np.abs(np.sin(np.arange(len(idx)) / (20.0 + seed))) + rng.uniform(0, base * 0.2, len(idx))
    feats = [{"type": "Feature", "id": f"f{i}", "properties": {
        "time": d.strftime("%Y-%m-%d"), "value": repr(float(v)), "unit_of_measure": "ft^3/s",
        "approval_status": "Approved", "qualifier": None, "time_series_id": "ts-synthetic",
        "monitoring_location_id": f"USGS-{gauge}", "parameter_code": "00060", "statistic_id": "00003",
        "last_modified": "2026-10-17T00:00:00+00:00"}} for i, (d, v) in enumerate(zip(idx, cfs))]
    return {"type": "FeatureCollection", "features": feats, "numberReturned": len(feats), "links": []}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "home" / ".aihydro"))
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))

    # The session describes ONE basin (its own gauge, its own area, its own streamflow).
    s = HydroSession(SID)
    s.site_id, s.site_type = "01013500", "usgs_gauge"
    s.set("watershed", {"data": {"area_km2": 999.0, "geometry_geojson": SQUARE},
                        "meta": {"tool": "delineate_watershed"}})
    s.save()

    nwis_calls: list[tuple] = []

    def override(url, params):
        if url.endswith("/collections/daily/items"):
            start, end = params["time"].split("/")
            gauge = params["monitoring_location_id"].removeprefix("USGS-")
            nwis_calls.append((gauge, start, end))
            return ok(_daily_body(gauge, start, end))
        return None                       # site records: the recorded fixtures

    from aihydro_data.sources import usgs_waterdata as uw
    monkeypatch.setattr(uw, "_http_get", Replay(override))
    return nwis_calls


def _register(gauge_by_feature: dict[str, str], areas: dict[str, float] | None = None) -> None:
    """Register each feature as its own basin through the real tool."""
    from ai_hydro.mcp.tools_session import register_feature
    for i, (fid, gauge) in enumerate(gauge_by_feature.items()):
        r = register_feature(session_id=SID, geojson=json.dumps(_square(i)), name=fid, feature_id=fid,
                             gauge_id=gauge, area_km2=(areas or {}).get(fid) or GAUGE_AREA[gauge],
                             set_active=False)
        assert not r.get("error"), r


def _run(features, *, name="three gauges", tool="extract_hydrological_signatures", metrics=None,
         params=None):
    from ai_hydro.mcp.tools_experiments import define_experiment, get_experiment_table, run_experiment
    d = define_experiment(session_id=SID, name=name, tool=tool, features=features,
                          params={"start_date": START, "end_date": END, "precipitation": "skip",
                                  **(params or {})},
                          metrics=metrics)
    assert not d.get("error"), d
    r = run_experiment(session_id=SID, experiment_id=d["experiment_id"])
    t = get_experiment_table(session_id=SID, experiment_id=d["experiment_id"])
    return d, r, t


# ---------------------------------------------------------------------------
# E1 + E4: three gauges, three basins, three run ids
# ---------------------------------------------------------------------------

def test_three_gauges_give_three_distinct_rows_each_with_its_own_run_id(world):
    _register({"basin_a": "01013500", "basin_b": "01109000", "basin_c": "03245500"},
              areas=AREA)
    d, r, t = _run(["basin_a", "basin_b", "basin_c"])

    assert r["status"] == "complete" and r["n_success"] == 3 and r["n_error"] == 0, r
    rows = {row["feature_id"]: row for row in t["rows"]}
    assert set(rows) == {"basin_a", "basin_b", "basin_c"}

    # distinct numbers (the defect: identical q_mean / baseflow_index / CI for every row)
    for m in ("q_mean", "baseflow_index", "flow_variability", "slope_fdc"):
        vals = [rows[f][m] for f in rows]
        assert all(v is not None for v in vals), (m, vals)
        assert len({round(v, 9) for v in vals}) == 3, (m, vals)
    assert len({(rows[f]["q_mean_ci_low"], rows[f]["q_mean_ci_high"]) for f in rows}) == 3
    # q_mean is a depth (mm/day): it divides by the area, which must be each feature's own
    assert rows["basin_b"]["q_mean"] > rows["basin_a"]["q_mean"]      # small basin, similar order of flow

    # each row carries its own run id, made for this experiment
    ids = [rows[f]["run_id"] for f in rows]
    assert all(ids) and len(set(ids)) == 3, ids
    assert {rows[f]["run_id_origin"] for f in rows} == {"fresh"}
    assert {rows[f]["status"] for f in rows} == {"ok"}
    assert t["status_counts"] == {"ok": 3}

    # the streamflow of EACH gauge was acquired, not the session's (01013500) three times
    assert sorted({c[0] for c in world}) == sorted(GAUGES)

    # the area and gauge each series was judged against are the feature's own, sealed with the result
    for fid, area, gauge in (("basin_a", AREA["basin_a"], "01013500"), ("basin_b", AREA["basin_b"], "01109000"),
                             ("basin_c", AREA["basin_c"], "03245500")):
        sess = HydroSession.load(SID)
        key = next(iter(sess.list_results("signatures")[fid]))
        data = sess.get_result("signatures", fid, key)["data"]
        assert data["basin_area_km2"] == area and data["basin_gauge_id"] == gauge


def test_the_repro_script_scenario_no_longer_collapses(world):
    """The review's repro: three gauge ids that are NOT registered, session with its own series.

    Before: status complete, three identical rows, run id only on the first.
    Now: each unregistered id is a visible per-feature error and nothing is computed."""
    d, r, t = _run(["01013500", "02177000", "06352000"])
    assert r["status"] == "error" and r["n_success"] == 0 and r["n_error"] == 3
    assert not world, "an unregistered feature must not trigger any acquisition"
    for row in t["rows"]:
        assert row["status"] == "error" and "FEATURE_NOT_RESOLVED" in row["reason"]
        assert row["run_id"] is None and row["q_mean"] is None
    assert t["aggregate_stats"] == {}


def test_unregistered_id_fails_closed_never_the_sessions_own_basin(world):
    _register({"basin_a": "01013500"}, areas={"basin_a": AREA["basin_a"]})
    d, r, t = _run(["basin_a", "ghost"])
    rows = {row["feature_id"]: row for row in t["rows"]}
    assert r["status"] == "partial" and r["n_success"] == 1 and "ghost" in r["errors"]
    assert rows["ghost"]["status"] == "error"
    assert "FEATURE_NOT_RESOLVED" in rows["ghost"]["reason"]
    assert "Registered features" in rows["ghost"]["reason"]
    assert rows["ghost"]["q_mean"] is None and rows["ghost"]["run_id"] is None
    assert "__legacy__" not in json.dumps(HydroSession.load(SID).list_results("signatures"))


def test_the_tool_itself_refuses_an_unregistered_feature(world):
    from ai_hydro.mcp.tools_analysis import extract_hydrological_signatures
    r = extract_hydrological_signatures(session_id=SID, start_date=START, end_date=END,
                                        feature="ghost", precipitation="skip")
    assert r["error"] and r["code"] == "FEATURE_NOT_RESOLVED"
    assert not world


def test_feature_without_a_gauge_is_refused_not_given_the_sessions_streamflow(world):
    from ai_hydro.mcp.tools_analysis import extract_hydrological_signatures
    from ai_hydro.mcp.tools_session import register_feature
    register_feature(session_id=SID, geojson=json.dumps(_square(0)), name="nogauge", feature_id="nogauge",
                     set_active=False)
    r = extract_hydrological_signatures(session_id=SID, start_date=START, end_date=END,
                                        feature="nogauge", precipitation="skip")
    assert r["error"] and r["code"] == "FEATURE_GAUGE_UNKNOWN" and "gauge_id" in r["recovery"]
    assert not world


def test_cache_key_includes_the_feature_gauge(world):
    _register({"basin_a": "01013500"})
    _, _, t1 = _run(["basin_a"], name="first")
    q1 = t1["rows"][0]["q_mean"]
    # the same feature id re-registered against a different gauge must not serve the old numbers
    _register({"basin_a": "03245500"})
    _, _, t2 = _run(["basin_a"], name="second")
    q2 = t2["rows"][0]["q_mean"]
    assert q1 != q2
    assert t2["rows"][0]["run_id_origin"] == "fresh" and t2["rows"][0]["gauge_id"] == "03245500"


def test_cache_hit_row_carries_the_run_that_produced_it(world):
    _register({"basin_a": "01013500", "basin_b": "01109000"})
    _, r1, _ = _run(["basin_a", "basin_b"], name="first")
    n_calls = len(world)
    # a second experiment over the same features and params is served from the per-feature cache
    d2, r2, t2 = _run(["basin_a", "basin_b"], name="second, same tool and params")
    assert len(world) == n_calls, "cache hits must not re-acquire"
    rows = {row["feature_id"]: row for row in t2["rows"]}
    for fid in ("basin_a", "basin_b"):
        assert rows[fid]["run_id_origin"] == "cache"
        assert rows[fid]["run_id"] == r1["run_ids"][fid], "the cache row names the ORIGINAL run"
        assert rows[fid]["status"] == "ok"


# ---------------------------------------------------------------------------
# E3: default metrics are emitted keys, and a missing value says why
# ---------------------------------------------------------------------------

def test_default_signature_metrics_are_keys_the_real_tool_emits(world):
    from ai_hydro.mcp.tools_analysis import extract_hydrological_signatures
    from ai_hydro.mcp.tools_experiments import _TOOL_METRICS, _TOOL_OUTPUT_KEYS
    _register({"basin_a": "01013500"})
    res = extract_hydrological_signatures(session_id=SID, start_date=START, end_date=END,
                                          feature="basin_a", precipitation="skip")
    assert not res.get("error"), res
    emitted = set(res["data"])
    tool = "extract_hydrological_signatures"
    assert set(_TOOL_OUTPUT_KEYS[tool]) <= emitted, sorted(set(_TOOL_OUTPUT_KEYS[tool]) - emitted)
    assert set(_TOOL_METRICS[tool]) <= set(_TOOL_OUTPUT_KEYS[tool])


def test_default_geomorphic_metrics_are_keys_the_real_function_emits():
    """The DEM fetch needs the network, so the keys are read from the function source
    (its ``params`` dict literal); the end-to-end routing is covered below with a labelled stand-in."""
    import inspect
    from aihydro_watershed.characterize import geomorphic as g
    from ai_hydro.mcp.tools_experiments import _TOOL_METRICS, _TOOL_OUTPUT_KEYS
    src = inspect.getsource(g.extract_geomorphic_parameters)
    keys = set(re.findall(r'^\s+"([A-Za-z_0-9]+)":', src, re.M))
    tool = "extract_geomorphic_parameters"
    assert set(_TOOL_OUTPUT_KEYS[tool]) <= keys, sorted(set(_TOOL_OUTPUT_KEYS[tool]) - keys)
    assert set(_TOOL_METRICS[tool]) <= set(_TOOL_OUTPUT_KEYS[tool])


def test_no_default_column_is_silently_empty_without_a_reason(world):
    _register({"basin_a": "01013500", "basin_b": "01109000"})
    _, r, t = _run(["basin_a", "basin_b"])           # precipitation skipped -> two honest nulls
    for row in t["rows"]:
        nulls = [m for m in t["columns"] if m in ("q_mean", "runoff_ratio", "baseflow_index", "flow_variability",
                                                 "high_q_freq", "low_q_freq", "slope_fdc", "stream_elas")
                 and row[m] is None]
        assert set(nulls) == {"runoff_ratio", "stream_elas"}, nulls
        assert set(row["missing_metrics"]) == set(nulls)
        assert all(row["missing_metrics"][m] for m in nulls)
    # the permanently-null defaults of the old table are gone
    assert "fdc_slope" not in t["columns"] and "streamflow_elasticity" not in t["columns"]


def test_unknown_metric_is_rejected_at_definition(world):
    from ai_hydro.mcp.tools_experiments import define_experiment
    r = define_experiment(session_id=SID, name="x", tool="extract_hydrological_signatures",
                          features=["a"], metrics=["q_mean", "streamflow_elasticity"])
    assert r["error"] and "streamflow_elasticity" in r["message"] and "stream_elas" in r["message"]


# ---------------------------------------------------------------------------
# E5: a feature that matches no tracked metric stays in the table, with a reason
# ---------------------------------------------------------------------------

def test_feature_matching_no_metric_is_a_row_with_a_reason_not_a_missing_row(world):
    _register({"basin_a": "01013500", "basin_b": "01109000"})
    # runoff_ratio is null for every basin when precipitation is skipped
    d, r, t = _run(["basin_a", "basin_b"], metrics=["runoff_ratio"])
    assert r["n_no_metrics"] == 2 and r["n_success"] == 0
    assert r["status"] == "error"
    assert [row["feature_id"] for row in t["rows"]] == ["basin_a", "basin_b"]
    for row in t["rows"]:
        assert row["status"] == "no_metrics"
        assert "runoff_ratio" in row["reason"] and "precipitation" in row["reason"].lower()
        assert row["error"] == row["reason"]
    assert t["status_counts"] == {"no_metrics": 2}


# ---------------------------------------------------------------------------
# E2: separate_baseflow
# ---------------------------------------------------------------------------

def test_separate_baseflow_experiment_is_refused_not_run_into_a_guaranteed_failure(world):
    from ai_hydro.mcp.tools_experiments import define_experiment
    r = define_experiment(session_id=SID, name="bf", tool="separate_baseflow", features=["a", "b"])
    assert r["error"]
    assert "extract_hydrological_signatures" in r["message"] and "baseflow_index" in r["message"]


def test_baseflow_index_per_basin_comes_from_signatures(world):
    _register({"basin_a": "01013500", "basin_b": "03245500"})
    d, r, t = _run(["basin_a", "basin_b"], metrics=["baseflow_index"])
    vals = [row["baseflow_index"] for row in t["rows"]]
    assert r["status"] == "complete" and all(v is not None for v in vals) and vals[0] != vals[1]
    assert all(row["run_id"] for row in t["rows"])


# ---------------------------------------------------------------------------
# geomorphic: per-feature geometry and outlet (labelled stand-in for the DEM step)
# ---------------------------------------------------------------------------

def test_geomorphic_experiment_runs_each_features_own_polygon_and_outlet(world, monkeypatch):
    from aihydro_core import HydroMeta, HydroResult
    from ai_hydro.mcp.tools_experiments import _TOOL_METRICS
    from ai_hydro.mcp.tools_session import register_feature
    seen = []

    def stand_in(watershed_geojson, outlet_lat, outlet_lon, dem_resolution=30):
        # STAND-IN for the DEM-derived computation (needs the network): a deterministic
        # function of the polygon and outlet it was handed, so distinct inputs give distinct rows.
        seen.append((json.dumps(watershed_geojson, sort_keys=True), outlet_lat, outlet_lon))
        x0 = watershed_geojson["coordinates"][0][0][0]
        data = {k: float(i) + x0 + outlet_lat for i, k in enumerate(_TOOL_METRICS["extract_geomorphic_parameters"])}
        return HydroResult(data=data, meta=HydroMeta(tool="stand_in", version="0", params={}))

    monkeypatch.setattr("ai_hydro.analysis.geomorphic.extract_geomorphic_parameters_result", stand_in)
    for i, fid in enumerate(("g1", "g2", "g3")):
        register_feature(session_id=SID, geojson=json.dumps(_square(i + 1)), name=fid, feature_id=fid,
                         outlet_lat=39.2 + i, outlet_lon=-77.5 + i, set_active=False)
    from ai_hydro.mcp.tools_experiments import define_experiment, get_experiment_table, run_experiment
    d = define_experiment(session_id=SID, name="geo2", tool="extract_geomorphic_parameters",
                          features=["g1", "g2", "g3"], params={})
    r = run_experiment(session_id=SID, experiment_id=d["experiment_id"])
    t = get_experiment_table(session_id=SID, experiment_id=d["experiment_id"])
    assert r["status"] == "complete", r
    assert len({s[0] for s in seen}) == 3 and sorted(s[1] for s in seen) == [39.2, 40.2, 41.2]
    rows = {row["feature_id"]: row for row in t["rows"]}
    assert len({rows[f]["DA_km2"] for f in rows}) == 3
    assert all(rows[f]["run_id"] for f in rows) and len({rows[f]["run_id"] for f in rows}) == 3
    # none of it came from the session's own polygon
    assert all(s[0] != json.dumps(SQUARE, sort_keys=True) for s in seen)


# ---------------------------------------------------------------------------
# E13: resource counts
# ---------------------------------------------------------------------------

def test_session_list_counts_the_real_number_of_experiments(world):
    from ai_hydro.mcp.resources import session_experiments, session_list
    _register({"basin_a": "01013500"})
    for i in range(5):
        _run(["basin_a"], name=f"exp number {i}", metrics=["q_mean"])
    entry = next(e for e in json.loads(session_list())["sessions"] if e["session_id"] == SID)
    assert entry["n_experiments"] == 5
    body = json.loads(session_experiments(SID))
    assert body["n_experiments"] == 5 and len(body["experiments"]) == 5
    assert all("defn" in e for e in body["experiments"])
