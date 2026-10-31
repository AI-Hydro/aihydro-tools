"""gauge_flowlines: NLDI flowline geometry for the map (canned SYNTHETIC service answers, no network)."""
from __future__ import annotations

import json

import pytest

from test_series_tools import SID, call, env, mcp_server, run_log  # noqa: F401  (fixtures)
from ai_hydro.data import nldi_flowlines as nf
from ai_hydro.session.store import HydroSession


def line(comid, x0, y0, n=3, step=0.01):
    return {"type": "Feature", "properties": {"nhdplus_comid": comid},
            "geometry": {"type": "LineString", "coordinates": [[x0 + i * step, y0] for i in range(n)]}}


UT = [line(1, -68.0, 47.0), line(2, -68.1, 47.1), line(2, -68.1, 47.1), line(3, -68.2, 47.2)]     # comid 2 repeated
DM = [line(10, -67.9, 46.9, n=4)]


def fake(calls=None, *, status=200, missing=False, ut=None, dm=None, big=False):
    def get(url, params, timeout=None):
        if calls is not None:
            calls.append((url, dict(params or {})))
        if missing:
            return 404, b"{}"
        if status != 200:
            return status, b"{}"
        feats = (ut if ut is not None else UT) if "/UT/" in url else (dm if dm is not None else DM)
        return 200, json.dumps({"type": "FeatureCollection", "features": feats}).encode()
    return get


def test_both_directions_summary_lengths_and_geometry():
    out = nf.gauge_flowlines("01031500", "both", 50, get=fake())
    s, gj = out["summary"], out["geojson"]
    assert s["available"] and s["direction"] == "both" and s["distance_km"] == 50
    assert s["directions"]["upstream"]["n_flowlines"] == 3                       # the repeated comid counted once
    assert s["directions"]["downstream"]["n_flowlines"] == 1
    assert s["n_features_total"] == s["n_features_returned"] == 4 and s["truncated"] is False
    assert s["directions"]["upstream"]["length_km"] == pytest.approx(3 * 2 * 0.01 * 111.2 * 0.68, rel=0.05)   # 2 segments of 0.01 deg at 47 N
    assert s["bbox"] == [-68.2, 46.9, -67.87, 47.2] and "west,south,east,north" in s["bbox_order"]
    assert {f["properties"]["direction"] for f in gj["features"]} == {"upstream", "downstream"}
    assert all(f["properties"]["length_km"] > 0 and f["geometry"]["type"] == "LineString" for f in gj["features"])


def test_one_direction_makes_one_request_and_a_distance_is_always_sent():
    calls = []
    out = nf.gauge_flowlines("01031500", "upstream", 25, get=fake(calls))
    assert len(calls) == 1 and "/UT/flowlines" in calls[0][0] and calls[0][1]["distance"] == 25
    calls.clear()
    nf.gauge_flowlines("01031500", "both", 25, get=fake(calls))
    assert len(calls) == 2 and all(c[1]["distance"] == 25 for c in calls)
    s = out["summary"]
    assert len(s["request_urls"].splitlines()) == 1 and len(s["response_sha256"].split()) == 1 and isinstance(s["request_urls"], str)


def test_the_feature_cap_truncates_and_says_so():
    many = [line(100 + i, -68.0, 47.0 + i * 0.001) for i in range(30)]
    out = nf.gauge_flowlines("01031500", "upstream", 10, max_features=10, get=fake(ut=many))
    s = out["summary"]
    assert s["truncated"] is True and s["n_features_total"] == 30 and s["n_features_returned"] == 10 == len(out["geojson"]["features"])
    assert s["directions"]["upstream"]["n_flowlines"] == 30 and s["directions"]["upstream"]["n_flowlines_returned"] == 10


def test_non_line_and_empty_geometry_is_skipped_and_an_empty_answer_has_no_bbox():
    bad = [{"type": "Feature", "properties": {"nhdplus_comid": 5}, "geometry": {"type": "Point", "coordinates": [0, 0]}},
           {"type": "Feature", "properties": {"nhdplus_comid": 6}, "geometry": {"type": "LineString", "coordinates": [[0, 0]]}}]
    s = nf.gauge_flowlines("01031500", "upstream", 5, get=fake(ut=bad))["summary"]
    assert s["n_features_returned"] == 0 and s["bbox"] is None


def test_us_only_unknown_gauge_and_service_trouble():
    for bad in ("GB-1", "", "abc", "0101"):
        with pytest.raises(nf.NotAvailable) as e:
            nf.parse_gauge_id(bad)
        assert e.value.code == "US_ONLY"
    with pytest.raises(nf.NotAvailable) as e:
        nf.gauge_flowlines("01031500", "both", 5, get=fake(missing=True))
    assert e.value.code == "GAUGE_NOT_IN_NLDI"
    for status, code in ((429, "RATE_LIMITED"), (503, "SERVICE_ERROR"), (400, "HTTP_ERROR")):
        with pytest.raises(nf.LookupFailed) as e:
            nf.gauge_flowlines("01031500", "both", 5, get=fake(status=status))
        assert e.value.code == code and e.value.retryable == (status != 400)
    for kw in ({"direction": "sideways"}, {"distance_km": 0}, {"distance_km": 201}, {"distance_km": True}, {"max_features": 0}):
        with pytest.raises(ValueError):
            nf.gauge_flowlines("01031500", **{"direction": "both", "distance_km": 5, **kw}, get=fake())


def test_the_default_getter_refuses_other_hosts_and_oversize_bodies(monkeypatch):
    with pytest.raises(nf.LookupFailed) as e:
        nf._default_get("https://example.com/x", None)
    assert e.value.code == "HOST_NOT_ALLOWED"
    import requests

    class Raw:
        def read(self, n, decode_content=True):
            return b"x" * n

    class R:
        status_code = 200
        raw = Raw()

    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    with pytest.raises(nf.LookupFailed) as e:
        nf._default_get(nf.NLDI_BASE + "/nwissite/USGS-1/navigation/UT/flowlines", None)
    assert e.value.code == "RESPONSE_TOO_LARGE"


# ---------------------------------------------------------------- the tool

def test_tool_writes_geometry_to_the_workspace_records_it_and_keeps_it_out_of_the_result(env, mcp_server, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    s = HydroSession.load(SID)
    s.workspace_dir = str(ws)
    s.save()
    monkeypatch.setattr(nf, "_default_get", fake())
    err, body, _ = call(mcp_server, "gauge_flowlines", {"gauge_id": "USGS-01031500", "direction": "both", "distance_km": 20, "session_id": SID})
    assert not err and body["data"]["available"] is True, body
    d = body["data"]
    assert "FeatureCollection" not in json.dumps(body) and "coordinates" not in json.dumps(body)      # no geometry in the result
    f = ws / d["geometry_file"]
    assert f.is_file() and d["geometry_file"].startswith("flowlines_01031500_both_20km_") and d["geometry_file"].endswith(".geojson")
    gj = json.loads(f.read_text())
    assert gj["type"] == "FeatureCollection" and len(gj["features"]) == d["n_features_returned"] == 4
    import hashlib
    assert hashlib.sha256(f.read_bytes()).hexdigest() == d["geojson_sha256"]
    row = run_log()[body["_run_id"]]
    assert row["key_outputs"]["n_features_returned"] == 4 and isinstance(row["key_outputs"]["request_urls"], str)
    assert row["key_outputs"]["directions"]["upstream"]["n_flowlines"] == 3


def test_tool_without_a_study_returns_the_summary_and_says_nothing_was_written(mcp_server, monkeypatch):
    import shutil
    from ai_hydro.session import store
    shutil.rmtree(store._SESSIONS_DIR, ignore_errors=True)
    from ai_hydro.mcp import tools_flowlines as t

    fn = t.gauge_flowlines.fn if hasattr(t.gauge_flowlines, "fn") else t.gauge_flowlines
    monkeypatch.setattr(nf, "_default_get", fake())
    out = fn("01031500", "upstream", 10)
    assert out["data"]["geometry_file"] is None and "pass session_id" in out["data"]["geometry_note"]


def test_tool_typed_answers(env, mcp_server, monkeypatch):
    from ai_hydro.mcp import tools_flowlines as t

    fn = t.gauge_flowlines.fn if hasattr(t.gauge_flowlines, "fn") else t.gauge_flowlines
    out = fn("GB-39001")
    assert out["available"] is False and out["code"] == "US_ONLY"
    monkeypatch.setattr(nf, "_default_get", fake(missing=True))
    assert fn("01031500", session_id=SID)["code"] == "GAUGE_NOT_IN_NLDI"
    monkeypatch.setattr(nf, "_default_get", fake(status=503))
    out = fn("01031500", session_id=SID)
    assert out["error"] is True and out["retryable"] is True
    assert fn("01031500", "diagonal")["code"] == "INVALID_PARAMETER"
