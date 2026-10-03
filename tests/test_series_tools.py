"""Deterministic series/geometry tools and the data_fetch / _run_id wiring.

All series here are SYNTHETIC (built in the test). Calls go through the real
low-level MCP request handler on the real server, as the extension does, with
aihydro-data's fetch replaced by a fake that serves a synthetic DataFrame and
writes it to a throwaway cache.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession
from test_run_record_middleware import call, needs_fastmcp2

SID = "series-session"
pytestmark = needs_fastmcp2


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    from ai_hydro.session import chat_binding

    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    cache = tmp_path / "datacache"
    cache.mkdir()
    monkeypatch.setattr("aihydro_data.cache.cache_dir", lambda: cache)
    HydroSession(SID).save()
    rr.reset_stats()
    return tmp_path


@pytest.fixture
def mcp_server(env):
    import ai_hydro.mcp  # noqa: F401  (registers every tool, wires data_fetch)
    from ai_hydro.mcp.app import mcp

    return mcp


@pytest.fixture
def serve(env, monkeypatch, mcp_server):
    """``serve(dates, values, units=..., product=...)`` -> run_id of a real data_fetch call."""
    import aihydro_data._pipeline as pipeline
    from aihydro_data.cache import cache_write
    from aihydro_data.contracts import FetchRequest, FetchResult

    counter = {"n": 0}

    def make(dates, values, *, units="m3/s", product="USGS_NWIS", variable="streamflow", cache=True):
        counter["n"] += 1
        df = pd.DataFrame({"date": pd.to_datetime(dates), variable: values})

        def fake_fetch(**kw):
            req = FetchRequest(variable=variable, geometry=kw["geometry"], start=kw["start"],
                               end=kw["end"], aggregation="basin_mean")
            res = FetchResult(variable=variable, product=product, source="direct_api", request=req,
                              data=df, units=units, timestep="daily",
                              cache_key=f"ck{counter['n']}", spatial_support="point")
            if kw.get("cache", True):
                cache_write(res)
            return res

        monkeypatch.setattr(pipeline, "fetch", fake_fetch)
        err, body, _ = call(mcp_server, "data_fetch", {
            "variable": variable, "geometry": [41.0, -71.0], "start": str(dates[0])[:10],
            "end": str(dates[-1])[:10], "cache": cache, "session_id": SID})
        assert not err and "error" not in body, body
        return body

    return make


def run_log():
    return HydroSession.load(SID).get("_run_log") or {}


def days(start, n, skip=()):
    d = pd.date_range(start, periods=n, freq="D")
    return [x.strftime("%Y-%m-%d") for i, x in enumerate(d) if i not in skip]


def run(server, tool, **args):
    err, body, _ = call(server, tool, {"session_id": SID, **args})
    return err, body


# ------------------------------------------------------------ data_fetch wiring

def test_data_fetch_leaves_a_sealed_record_with_a_retained_addressable_series(serve):
    body = serve(days("2020-01-01", 6), [1.0, 2.0, None, 4.0, 5.0, 6.0])
    rid = body["_run_id"]
    assert body["data"]["series_retained"] is True
    assert body["data"]["retained_series"]["n"] == 6 and body["data"]["retained_series"]["n_finite"] == 5
    row = run_log()[rid]
    assert rr.verify_run_log_entry(row)["record_ok"] is True
    (kept,) = row["record"]["extra"]["retained_files"]
    assert kept["digest"] == body["data"]["retained_series"]["file_digest"]
    assert row["record"]["tool"] == "data_fetch"


def test_data_fetch_without_cache_is_recorded_aggregate_only(serve):
    body = serve(days("2020-01-01", 4), [1.0, 2.0, 3.0, 4.0], cache=False)
    assert body["_run_id"] in run_log()
    assert body["data"]["series_retained"] is False
    assert "retained_files" not in run_log()[body["_run_id"]]["record"]["extra"]


def test_data_fetch_accepts_session_id_and_without_a_session_returns_unrecorded(mcp_server, monkeypatch, env):
    import aihydro_data._pipeline as pipeline
    from aihydro_data.contracts import FetchRequest, FetchResult

    def fake_fetch(**kw):
        req = FetchRequest(variable="streamflow", geometry=kw["geometry"], start=kw["start"], end=kw["end"])
        return FetchResult(variable="streamflow", product="P", source="direct_api", request=req,
                           data=pd.DataFrame({"date": pd.to_datetime(["2020-01-01"]), "streamflow": [1.0]}),
                           cache_key="ckx")

    monkeypatch.setattr(pipeline, "fetch", fake_fetch)
    # No session_id, no chat/study context, and no session on disk: nothing to record into.
    import shutil
    shutil.rmtree(store._SESSIONS_DIR, ignore_errors=True)
    err, body, _ = call(mcp_server, "data_fetch", {
        "variable": "streamflow", "geometry": [41.0, -71.0], "start": "2020-01-01", "end": "2020-01-01"})
    assert not err and "_run_id" not in body and body["variable"] == "streamflow"


# ------------------------------------------------------------------- summarize

def test_summarize_series_values_calendar_and_recorded_metadata(serve, mcp_server):
    vals = [3.0, 1.0, None, 7.0, 5.0, 9.0, 2.0]
    d = days("2020-01-01", 10, skip=(4, 5, 8))          # 7 dates present of 10; 3 missing
    body = serve(d, vals, units="cfs", product="SYNTH_PRODUCT")
    err, out = run(mcp_server, "summarize_series", series=body["_run_id"])
    assert not err and "error" not in out
    data = out["data"]
    fin = np.array([3.0, 1.0, 7.0, 5.0, 9.0, 2.0])
    assert data["n"] == 7 and data["n_finite"] == 6
    assert data["mean"] == pytest.approx(fin.mean())
    assert (data["min"], data["max"]) == (1.0, 9.0)
    assert data["median"] == pytest.approx(np.median(fin))
    assert [q["probability"] for q in data["quantiles"]] == [0.05, 0.25, 0.5, 0.75, 0.95]
    assert data["quantile_p25"] == pytest.approx(np.quantile(fin, 0.25))
    assert "linear" in data["quantile_method"]
    assert data["first_date"] == "2020-01-01" and data["last_date"] == "2020-01-10"
    assert data["expected_calendar_days"] == 10 and data["n_missing_dates"] == 3
    assert data["missing_dates"] == ["2020-01-05", "2020-01-06", "2020-01-09"]
    assert data["units"] == "cfs" and data["product"] == "SYNTH_PRODUCT"
    assert data["values_available"] is True and out["_run_id"] in run_log()


def test_summarize_series_window_and_custom_quantiles(serve, mcp_server):
    body = serve(days("2020-01-01", 10), [float(i) for i in range(10)])
    err, out = run(mcp_server, "summarize_series", series=body["_run_id"],
                   start="2020-01-03", end="2020-01-12", quantiles=[0.1, 0.9])
    data = out["data"]
    assert data["n"] == 8 and data["expected_calendar_days"] == 10 and data["n_missing_dates"] == 2
    assert data["missing_dates"] == ["2020-01-11", "2020-01-12"]
    assert [q["probability"] for q in data["quantiles"]] == [0.1, 0.9]


def test_summarize_series_on_an_aggregate_only_record_returns_recorded_statistics(serve, mcp_server):
    body = serve(days("2020-01-01", 4), [1.0, 2.0, 3.0, 4.0], cache=False)
    err, out = run(mcp_server, "summarize_series", series=body["_run_id"])
    assert not err and out["data"]["values_available"] is False
    assert out["data"]["recorded_statistics"]["series_retained"] is False
    assert out["_run_id"] in run_log()


def test_consumer_record_carries_parent_lineage_and_input_digest(serve, mcp_server):
    body = serve(days("2020-01-01", 5), [1.0, 2.0, 3.0, 4.0, 5.0])
    err, out = run(mcp_server, "summarize_series", series=body["_run_id"])
    rec = run_log()[out["_run_id"]]["record"]
    assert rec["parents"] == [body["_run_id"]]
    digests = {r["digest"] for r in rec["input_refs"]}
    assert body["data"]["retained_series"]["file_digest"] in digests


def test_unknown_run_is_a_plain_input_error(mcp_server, env):
    err, out = run(mcp_server, "summarize_series", series="nope.1")
    assert out["error"] is True and out["code"] == "RUN_NOT_FOUND"
    assert set(out) == {"error", "code", "message"}


def test_tampered_retained_series_is_refused(serve, mcp_server):
    body = serve(days("2020-01-01", 5), [1.0, 2.0, 3.0, 4.0, 5.0])
    rec = run_log()[body["_run_id"]]["record"]
    from ai_hydro.session.refs import resolve_ref

    path = resolve_ref(rec["extra"]["retained_files"][0]["path"], SID)
    doc = json.loads(path.read_text())
    doc["values"][0] = 99.0
    path.write_text(json.dumps(doc))
    err, out = run(mcp_server, "summarize_series", series=body["_run_id"])
    assert out["code"] == "RETAINED_SERIES_DIGEST_MISMATCH"


# ------------------------------------------------------------------------ runs

def test_runs_break_on_a_missing_calendar_date_and_skip_bridges_it(serve, mcp_server):
    # values above 5 on d1-d3, a missing calendar day (d4), then above again d5-d6; d7 low.
    d = days("2020-01-01", 7, skip=(3,))
    vals = [9.0, 9.0, 9.0, 9.0, 9.0, 1.0]
    body = serve(d, vals)
    err, out = run(mcp_server, "detect_threshold_runs", series=body["_run_id"], threshold=5.0)
    data = out["data"]
    assert [(r["start"], r["end"], r["length"]) for r in data["runs"]] == [
        ("2020-01-01", "2020-01-03", 3), ("2020-01-05", "2020-01-06", 2)]
    assert data["n_runs"] == 2 and data["longest_run_length"] == 3 and data["gap_policy"] == "break"
    assert data["expected_calendar_days"] == 7 and data["n_present_dates"] == 6
    assert data["missing_dates"] == ["2020-01-04"]

    err, out = run(mcp_server, "detect_threshold_runs", series=body["_run_id"], threshold=5.0,
                   gap_policy="skip")
    data = out["data"]
    assert data["n_runs"] == 1 and data["runs"][0]["length"] == 5 and data["runs"][0]["calendar_days"] == 6
    assert data["gap_policy"] == "skip"


def test_a_date_with_no_value_ends_a_run_under_break(serve, mcp_server):
    body = serve(days("2020-01-01", 5), [9.0, 9.0, None, 9.0, 9.0])
    err, out = run(mcp_server, "detect_threshold_runs", series=body["_run_id"], threshold=5.0)
    assert [r["length"] for r in out["data"]["runs"]] == [2, 2]
    assert out["data"]["n_dates_without_value"] == 1


def test_relative_threshold_and_comparison(serve, mcp_server):
    body = serve(days("2020-01-01", 8), [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 9.0])
    err, out = run(mcp_server, "detect_threshold_runs", series=body["_run_id"],
                   threshold_relative={"stat": "median", "factor": 3})
    data = out["data"]
    assert data["threshold_value"] == pytest.approx(3.0) and data["threshold_basis"]["kind"] == "relative"
    assert data["n_runs"] == 1 and data["runs"][0]["start"] == "2020-01-08"
    err, out = run(mcp_server, "detect_threshold_runs", series=body["_run_id"], threshold=1.0,
                   comparison="le")
    assert out["data"]["longest_run_length"] == 7


def test_runs_input_errors_are_plain(serve, mcp_server):
    body = serve(days("2020-01-01", 5), [1.0, 2.0, 3.0, 4.0, 5.0])
    for args in ({}, {"threshold": 1.0, "threshold_relative": {"stat": "mean", "factor": 1}},
                 {"threshold": 1.0, "comparison": "eq"}, {"threshold": 1.0, "gap_policy": "fill"}):
        err, out = run(mcp_server, "detect_threshold_runs", series=body["_run_id"], **args)
        assert out["error"] is True and out["code"] == "INVALID_INPUT" and set(out) == {"error", "code", "message"}


# --------------------------------------------------------------------- compare

def _kge(a, b):
    r = np.corrcoef(a, b)[0, 1]
    alpha = b.std() / a.std()
    beta = b.mean() / a.mean()
    return 1 - math.sqrt((r - 1) ** 2 + (alpha - 1) ** 2 + (beta - 1) ** 2)


def test_compare_series_pairs_by_calendar_date_and_echoes_units(serve, mcp_server):
    rng = np.random.default_rng(3)
    a_vals = list(rng.uniform(1, 10, 10))
    b_vals = list(np.array(a_vals) * 1.2 + rng.normal(0, 0.5, 10))
    a = serve(days("2020-01-01", 10), a_vals, units="m3/s", product="A")
    b = serve(days("2020-01-03", 10, skip=(2,)), b_vals[:9], units="cfs", product="B")
    err, out = run(mcp_server, "compare_series", series_a=a["_run_id"], series_b=b["_run_id"])
    assert not err and "error" not in out
    d = out["data"]
    # common dates: 2020-01-03..2020-01-10 minus the skipped 2020-01-05 -> a idx 2..9 minus idx 4
    a_idx = [i for i in range(2, 10) if i != 4]
    b_dates = days("2020-01-03", 10, skip=(2,))[:9]
    pairs = [(a_vals[i], b_vals[b_dates.index(days("2020-01-01", 10)[i])]) for i in a_idx
             if days("2020-01-01", 10)[i] in b_dates]
    pa, pb = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
    assert d["n_pairs"] == len(pairs)
    assert d["mean_a"] == pytest.approx(pa.mean()) and d["mean_b"] == pytest.approx(pb.mean())
    assert d["percent_bias"] == pytest.approx(100 * (pb.mean() - pa.mean()) / pa.mean())
    assert d["r"] == pytest.approx(np.corrcoef(pa, pb)[0, 1])
    assert d["sd_ratio"] == pytest.approx(pb.std() / pa.std())
    assert d["kge"] == pytest.approx(_kge(pa, pb))
    assert "ddof=0" in d["sd_convention"] and "Gupta" in d["kge_convention"]
    assert (d["units_a"], d["units_b"]) == ("m3/s", "cfs")          # mismatch echoed, not refused
    assert d["convert_to"] is None and d["conversion_a"] is None
    assert d["period_a"]["first_date"] == "2020-01-01" and d["period_b"]["first_date"] == "2020-01-03"


def test_compare_series_converts_only_when_asked(serve, mcp_server):
    a = serve(days("2020-01-01", 6), [1.0, 2.0, 3.0, 4.0, 5.0, 7.0], units="m3/s")
    b = serve(days("2020-01-01", 6), [v / 0.028316846592 for v in [1.0, 2.0, 3.0, 4.0, 5.0, 7.0]], units="cfs")
    _, plain = run(mcp_server, "compare_series", series_a=a["_run_id"], series_b=b["_run_id"])
    assert plain["data"]["percent_bias"] > 1000
    _, conv = run(mcp_server, "compare_series", series_a=a["_run_id"], series_b=b["_run_id"],
                  convert_to="m3/s")
    d = conv["data"]
    assert d["percent_bias"] == pytest.approx(0.0, abs=1e-9) and d["kge"] == pytest.approx(1.0)
    assert d["conversion_b"]["from"] == "cfs" and d["conversion_a"]["from"] == "m3/s"
    err, bad = run(mcp_server, "compare_series", series_a=a["_run_id"], series_b=b["_run_id"],
                   convert_to="mm")
    assert bad["code"] == "INVALID_INPUT"


# ------------------------------------------------------------------- bootstrap

def test_bootstrap_requires_a_method(serve, mcp_server):
    body = serve(days("2020-01-01", 30), [float(i % 7) for i in range(30)])
    err, out, _ = call(mcp_server, "bootstrap_statistic",
                       {"session_id": SID, "series": body["_run_id"], "statistic": "mean"})
    assert "method" in json.dumps(out)
    err, out = run(mcp_server, "bootstrap_statistic", series=body["_run_id"], statistic="mean",
                   method="jackknife")
    assert out["code"] == "INVALID_INPUT"


def test_bootstrap_matches_the_library_and_is_seeded(serve, mcp_server):
    from aihydro_core.science._bootstrap import block_bootstrap_ci, bootstrap_ci

    rng = np.random.default_rng(1)
    vals = list(rng.gamma(2.0, 3.0, 60))
    body = serve(days("2020-01-01", 60), vals)
    arr = np.array(vals)
    _, iid = run(mcp_server, "bootstrap_statistic", series=body["_run_id"], statistic="mean",
                 method="iid", n_resamples=200, level=0.9, seed=7)
    d = iid["data"]
    ref = bootstrap_ci(lambda a: float(np.mean(a)), arr, n=200, ci=0.9, random_state=7)
    assert d["estimate"] == pytest.approx(ref["value"]) and d["ci_low"] == pytest.approx(ref["ci_low"])
    assert d["ci_high"] == pytest.approx(ref["ci_high"])
    assert (d["method"], d["n_resamples"], d["seed"], d["block_length"]) == ("bootstrap_iid", 200, 7, None)
    assert d["_uncertainty"]["estimate"]["ci_level"] == 0.9 and d["_uncertainty"]["estimate"]["n"] == 60

    _, blk = run(mcp_server, "bootstrap_statistic", series=body["_run_id"], statistic="median",
                 method="block", block_length=5, n_resamples=200, level=0.9, seed=7)
    d = blk["data"]
    ref = block_bootstrap_ci(lambda a: float(np.median(a)), arr, block_size=5, n=200, ci=0.9, random_state=7)
    assert d["ci_low"] == pytest.approx(ref["ci_low"]) and d["ci_high"] == pytest.approx(ref["ci_high"])
    assert d["block_length"] == 5 and d["block_length_source"] == "caller"
    _, blk2 = run(mcp_server, "bootstrap_statistic", series=body["_run_id"], statistic="median",
                  method="block", n_resamples=200, level=0.9, seed=7)
    assert blk2["data"]["block_length"] == 5 and "default" in blk2["data"]["block_length_source"]
    _, again = run(mcp_server, "bootstrap_statistic", series=body["_run_id"], statistic="mean",
                   method="iid", n_resamples=200, level=0.9, seed=7)
    assert again["data"]["ci_low"] == iid["data"]["ci_low"]


def test_bootstrap_on_a_record_with_no_daily_values_is_a_plain_input_error(serve, mcp_server):
    body = serve(days("2020-01-01", 30), [float(i) for i in range(30)], cache=False)
    err, out = run(mcp_server, "bootstrap_statistic", series=body["_run_id"], statistic="mean", method="iid")
    assert out["error"] is True and out["code"] == "INVALID_INPUT" and set(out) == {"error", "code", "message"}


# ------------------------------------------------------------- measure_feature

SQUARE = {"type": "Polygon", "coordinates": [[[0.0, 0.0], [0.1, 0.0], [0.1, 0.1], [0.0, 0.1], [0.0, 0.0]]]}


def test_measure_feature_registered_and_inline(mcp_server, env):
    from pyproj import Geod
    from shapely.geometry import shape

    err, reg, _ = call(mcp_server, "register_feature", {"geojson": json.dumps(SQUARE), "name": "sq",
                                                         "session_id": SID})
    assert not err
    area_m2, perim_m = Geod(ellps="WGS84").geometry_area_perimeter(shape(SQUARE))
    for ref in (reg["feature_id"], "sq", json.dumps(SQUARE)):
        err, out = run(mcp_server, "measure_feature", feature=ref)
        d = out["data"]
        assert d["area_km2"] == pytest.approx(abs(area_m2) / 1e6, rel=1e-9)
        assert d["perimeter_km"] == pytest.approx(abs(perim_m) / 1e3, rel=1e-9)
        assert "WGS84" in d["method"] and d["geometry_type"] == "Polygon"
    assert out["_run_id"] in run_log()
    n_features = len(HydroSession.load(SID).list_features())
    run(mcp_server, "measure_feature", feature=json.dumps(SQUARE))
    assert len(HydroSession.load(SID).list_features()) == n_features       # reads only


def test_measure_feature_unknown_feature_is_a_plain_input_error(mcp_server, env):
    err, out = run(mcp_server, "measure_feature", feature="no-such-feature")
    assert out["error"] is True and out["code"] == "INVALID_INPUT" and set(out) == {"error", "code", "message"}


# ------------------------------------------------------------------- neutrality

NEUTRALITY_FORBIDDEN = ("next_steps", "warning", "recommend", "advice", "adequa", "insufficient")


def test_results_carry_no_warnings_recommendations_or_next_steps(serve, mcp_server):
    short = serve(days("2020-01-01", 12), [1.0] * 5 + [None] * 3 + [2.0] * 4)     # short, gappy, constant-ish
    reg = call(mcp_server, "register_feature", {"geojson": json.dumps(SQUARE), "session_id": SID})[1]
    outs = [
        run(mcp_server, "summarize_series", series=short["_run_id"])[1],
        run(mcp_server, "detect_threshold_runs", series=short["_run_id"], threshold=1.5)[1],
        run(mcp_server, "compare_series", series_a=short["_run_id"], series_b=short["_run_id"])[1],
        run(mcp_server, "bootstrap_statistic", series=short["_run_id"], statistic="mean", method="iid")[1],
        run(mcp_server, "measure_feature", feature=reg["feature_id"])[1],
    ]
    for out in outs:
        assert "error" not in out, out
        assert out["quality_flags"] == [] and "next_steps" not in out
        text = json.dumps(out).lower()
        for word in NEUTRALITY_FORBIDDEN:
            assert word not in text, (word, out)
        assert out["_run_id"] in run_log()


# --------------------------------------------------------------- _run_id wiring

def test_every_recorded_tool_result_gets_a_run_id_through_the_middleware(env):
    """compute_flow_duration_curve never called post_run; its result still carries _run_id."""
    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp.app import mcp

    d = pd.date_range("2000-01-01", periods=3 * 365, freq="D")
    q = [float(5 + 4 * math.sin(i / 20.0)) for i in range(len(d))]
    path = store.write_session_data_file(
        SID, "streamflow_synth.json", {"dates": [x.strftime("%Y-%m-%d") for x in d], "q_cms": q})
    s = HydroSession.load(SID)
    s.set("streamflow", {"data": {"_data_file": path, "q_cms_n": len(q)},
                         "meta": {"tool": "fetch_streamflow_data"}})
    s.save()
    err, body, _ = call(mcp, "compute_flow_duration_curve", {"session_id": SID})
    assert not err and "error" not in body, body
    rid = body["_run_id"]
    assert rr.verify_run_log_entry(run_log()[rid])["record_ok"] is True
    assert run_log()[rid]["record"]["tool"] == "compute_flow_duration_curve"
    assert body["percentile_flows"]        # the result itself is otherwise unchanged


def test_run_id_injection_rules(env):
    from fastmcp import FastMCP

    from ai_hydro.mcp import app
    from ai_hydro.mcp.enforcement import post_run

    srv = FastMCP(name="probe")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())

    @srv.tool()
    def plain(session_id: str) -> dict:
        return {"data": {"x": 1}}

    @srv.tool()
    def sealed(session_id: str) -> dict:
        return post_run("sealed", session_id, {"data": {"x": 1}})

    @srv.tool()
    def failing(session_id: str) -> dict:
        return {"error": True, "code": "NOPE", "message": "m"}

    @srv.tool()
    def a_list(session_id: str) -> list:
        return [1, 2]

    _, p, _ = call(srv, "plain", {"session_id": SID})
    assert p["_run_id"] in run_log() and p["data"] == {"x": 1}
    _, s, _ = call(srv, "sealed", {"session_id": SID})
    assert s["_run_id"] in run_log() and list(run_log()).count(s["_run_id"]) == 1
    _, f, _ = call(srv, "failing", {"session_id": SID})
    assert f == {"error": True, "code": "NOPE", "message": "m"}
    _, lst, _ = call(srv, "a_list", {"session_id": SID})
    assert lst == [1, 2]
    # _run_id is a transport key: the sealed output digest does not depend on it.
    rec = run_log()[p["_run_id"]]["record"]
    from aihydro_core.records import digest
    assert rec["output_digest"] == digest({"x": 1})


def test_series_retained_by_fetch_streamflow_data_format_is_readable(mcp_server, env):
    """A run that retained the legacy ``dates`` + ``q_cms`` file (fetch_streamflow_data) is addressable too."""
    from pathlib import Path

    from aihydro_core.records import digest_bytes
    from ai_hydro.session.refs import to_ref

    def legacy_fetch(session_id):
        path = store.write_session_data_file(
            session_id, "streamflow_legacy.json",
            {"dates": days("2020-01-01", 4), "q_cms": [1.0, None, 3.0, 4.0], "units": "m3/s",
             "_aihydro_data_product": "LEGACY"})
        rr.declare_lineage(retained_files=[{"path": to_ref(path), "digest": digest_bytes(Path(path).read_bytes()),
                                            "role": "artifact"}])
        return {"data": {"n_days": 4}}

    result = rr.recorded_call("fetch_streamflow_data", legacy_fetch, session_id=SID)
    (rid,) = [k for k, v in run_log().items() if v["record"]["tool"] == "fetch_streamflow_data"]
    err, out = run(mcp_server, "summarize_series", series=rid)
    d = out["data"]
    assert d["n"] == 4 and d["n_finite"] == 3 and d["units"] == "m3/s" and d["product"] == "LEGACY"


# ----------------------------------------------- skeptic follow-ups (S1, S2, minors)

def test_swapped_retained_pointer_in_a_sealed_producer_is_refused(serve, mcp_server):
    """S1: the pointer inside the producer's record is changed to a forged file; the seal then fails."""
    from aihydro_core.records import digest_bytes
    from ai_hydro.session.refs import resolve_ref

    body = serve(days("2020-01-01", 3), [1.0, 2.0, 3.0])
    rid = body["_run_id"]
    forged = store.write_session_data_file(SID, "forged.json", {"dates": days("2020-01-01", 2), "values": [1e6, 1e6]})
    row = run_log()[rid]
    row["record"]["extra"]["retained_files"][0] = {
        "path": f"session-data:{forged.rsplit('/', 1)[-1]}",
        "digest": digest_bytes(open(forged, "rb").read()), "role": "artifact"}
    assert rr.verify_run_log_entry(row)["record_ok"] is False
    import sqlite3
    conn = sqlite3.connect(store._run_log_db_path(SID))
    conn.execute("UPDATE runs SET entry_json = ? WHERE run_id = ?", (json.dumps(row), rid))
    conn.commit()
    conn.close()
    err, out = run(mcp_server, "summarize_series", series=rid)
    assert out["code"] == "RETAINED_SERIES_RECORD_INVALID" and set(out) == {"error", "code", "message"}


def test_legacy_unsealed_producer_is_still_readable(env):
    from ai_hydro.session.series import load_run_series

    path = store.write_session_data_file(SID, "legacy.json", {"dates": days("2020-01-01", 2), "q_cms": [1.0, 2.0]})
    s = HydroSession.load(SID)
    # a legacy row: no record at all, so nothing to verify and no retained pointer -> aggregate-only
    s.set("_run_log", {"old.1": {"run_id": "old.1", "tool_name": "x", "key_outputs": {"a": 1}}})
    s.save()
    got = load_run_series(SID, "old.1")
    assert got.values_available is False and got.recorded_statistics == {"a": 1}


def test_data_fetch_wrapper_signature_is_a_superset_of_aihydro_data():
    """S2: fail loudly if aihydro-data renames _data_fetch or adds/changes a parameter."""
    import inspect

    from aihydro_data.mcp import _data_fetch
    from ai_hydro.mcp.tools_data_fetch import data_fetch

    theirs = inspect.signature(_data_fetch).parameters
    ours = inspect.signature(data_fetch).parameters
    missing = [n for n in theirs if n not in ours]
    assert not missing, f"aihydro-data's data_fetch gained parameters the wrapper hides: {missing}"
    for n, p in theirs.items():
        assert ours[n].default == p.default, n
        assert ours[n].annotation == p.annotation, n
    assert set(ours) - set(theirs) == {"session_id"}


def test_registered_data_fetch_is_the_retaining_wrapper_and_returns_run_id(serve, mcp_server):
    import asyncio

    tool = asyncio.run(mcp_server.get_tool("data_fetch"))
    assert tool.fn.__module__ == "ai_hydro.mcp.tools_data_fetch"
    body = serve(days("2020-01-01", 3), [1.0, 2.0, 3.0])
    assert body["_run_id"] in run_log()


def test_measure_feature_inline_geojson_caps_are_plain_errors(mcp_server, env):
    deep = "[" * 100000 + "]" * 100000
    for feature in ('{"type":"Polygon","coordinates":' + deep + "}",):
        err, out = run(mcp_server, "measure_feature", feature=feature)
        assert out["code"] == "INVALID_INPUT" and set(out) == {"error", "code", "message"}
    from ai_hydro.analysis import series_ops

    big = {"type": "Polygon", "coordinates": [[[0.0, 0.0]] * 10]}
    series_ops.MAX_GEOJSON_VERTICES, old = 5, series_ops.MAX_GEOJSON_VERTICES
    try:
        with pytest.raises(series_ops.SeriesInputError):
            series_ops.measure_geometry(big)
    finally:
        series_ops.MAX_GEOJSON_VERTICES = old


def test_zero_variance_inputs_give_none_components(serve, mcp_server):
    a = serve(days("2020-01-01", 5), [2.0] * 5)
    b = serve(days("2020-01-01", 5), [1.0, 2.0, 3.0, 4.0, 5.0])
    err, out = run(mcp_server, "compare_series", series_a=a["_run_id"], series_b=b["_run_id"])
    d = out["data"]
    assert d["r"] is None and d["sd_ratio"] is None and d["kge"] is None


# ------------------------------------------------ provider-declared units carried through

def test_unit_spellings_with_superscripts_convert(mcp_server):
    from ai_hydro.analysis.series_ops import convert_values

    out, info = convert_values(np.array([1.0]), "ft³/s", "m³/s")
    assert out[0] == pytest.approx(0.028316846592) and info["dimension"] == "discharge"
    assert convert_values(np.array([2.0]), "m3 s-1", "m3/s")[0][0] == 2.0


def test_retained_series_carries_declared_and_spec_units(serve, mcp_server, monkeypatch):
    """units_declared / units_spec in the fetch result are written to the retained file and echoed."""
    import aihydro_data.mcp as adm

    real = adm._data_fetch

    def with_units(**kw):
        out = real(**kw)
        out.update({"units": "ft3/s", "units_spec": "m3/s", "units_declared": "ft3/s"})
        return out

    monkeypatch.setattr(adm, "_data_fetch", with_units)
    body = serve(days("2020-01-01", 4), [1.0, 2.0, 3.0, 4.0], units="m3/s")
    err, out = run(mcp_server, "summarize_series", series=body["_run_id"])
    d = out["data"]
    assert (d["units"], d["units_spec"], d["units_declared"]) == ("ft3/s", "m3/s", "ft3/s")


@pytest.mark.parametrize("spelling,target,expected", [
    ("cubic meters per second", "m3/s", 1.0), ("m³ s⁻¹", "m3/s", 1.0),
    ("m3 s^-1", "m3/s", 1.0), ("cubic feet per second", "m3/s", 0.028316846592),
    ("ft3 s-1", "m3/s", 0.028316846592), ("mm d-1", "mm/day", 1.0), ("mm/day", "mm d-1", 1.0),
    ("deg_C", "K", 274.15), ("°C", "K", 274.15), ("degrees Celsius", "degF", 33.8),
    ("m3/s", "cubic metres per second", 1.0),
])
def test_unit_alias_spellings(spelling, target, expected):
    from ai_hydro.analysis.series_ops import convert_values

    out, _ = convert_values(np.array([1.0]), spelling, target)
    assert out[0] == pytest.approx(expected)
