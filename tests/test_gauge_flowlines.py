"""gauge_flowlines: NLDI flowline geometry for the map (canned SYNTHETIC service answers, no network)."""
from __future__ import annotations

import json

import pytest

from test_series_tools import SID, call, env, mcp_server, run_log  # noqa: F401  (fixtures)
from ai_hydro.data import gauge_network as gn
from ai_hydro.data import nldi_flowlines as nf
from ai_hydro.session.store import HydroSession


def line(comid, x0, y0, n=3, step=0.01):
    return {"type": "Feature", "properties": {"nhdplus_comid": comid},
            "geometry": {"type": "LineString", "coordinates": [[x0 + i * step, y0] for i in range(n)]}}


UT = [line(1, -68.0, 47.0), line(2, -68.1, 47.1), line(2, -68.1, 47.1), line(3, -68.2, 47.2)]     # comid 2 repeated
DM = [line(10, -67.9, 46.9, n=4)]


def cl(get, **kw):
    return gn.Client(get, max_requests=2, deadline_s=kw.pop("deadline_s", 45.0), bytes_cap=nf.MAX_RESPONSE_BYTES, **kw)


def fake(calls=None, *, status=200, missing=False, ut=None, dm=None, big=False):
    def get(url, params, timeout=None, bytes_cap=None):
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
    out = nf.gauge_flowlines("01031500", "both", 50, client=cl(fake()))
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
    out = nf.gauge_flowlines("01031500", "upstream", 25, client=cl(fake(calls)))
    assert len(calls) == 1 and "/UT/flowlines" in calls[0][0] and calls[0][1]["distance"] == 25
    calls.clear()
    nf.gauge_flowlines("01031500", "both", 25, client=cl(fake(calls)))
    assert len(calls) == 2 and all(c[1]["distance"] == 25 for c in calls)
    s = out["summary"]
    assert len(s["request_urls"].splitlines()) == 1 and len(s["response_sha256"].split()) == 1 and isinstance(s["request_urls"], str)


def test_the_cap_is_per_direction_so_a_big_upstream_cannot_push_the_main_stem_out():
    many = [line(100 + i, -68.0, 47.0 + i * 0.001) for i in range(30)]
    out = nf.gauge_flowlines("01031500", "both", 10, max_features=10, client=cl(fake(ut=many)))
    s, gj = out["summary"], out["geojson"]
    assert s["truncated"] is True and s["n_features_total"] == 31 and s["n_features_returned"] == 11 == len(gj["features"])
    assert s["directions"]["upstream"]["n_flowlines"] == 30 and s["directions"]["upstream"]["n_flowlines_returned"] == 10
    assert s["directions"]["upstream"]["truncated"] is True and s["directions"]["downstream"]["truncated"] is False
    assert [f["properties"]["direction"] for f in gj["features"]].count("downstream") == 1        # the main stem survived


def test_non_line_and_empty_geometry_is_skipped_and_an_empty_answer_has_no_bbox():
    bad = [{"type": "Feature", "properties": {"nhdplus_comid": 5}, "geometry": {"type": "Point", "coordinates": [0, 0]}},
           {"type": "Feature", "properties": {"nhdplus_comid": 6}, "geometry": {"type": "LineString", "coordinates": [[0, 0]]}}]
    s = nf.gauge_flowlines("01031500", "upstream", 5, client=cl(fake(ut=bad)))["summary"]
    assert s["n_features_returned"] == 0 and s["bbox"] is None


def test_us_only_unknown_gauge_and_service_trouble():
    for bad in ("GB-1", "", "abc", "0101"):
        with pytest.raises(nf.NotAvailable) as e:
            nf.parse_gauge_id(bad)
        assert e.value.code == "US_ONLY"
    with pytest.raises(nf.NotAvailable) as e:
        nf.gauge_flowlines("01031500", "both", 5, client=cl(fake(missing=True)))
    assert e.value.code == "GAUGE_NOT_IN_NLDI"
    for status, code in ((429, "RATE_LIMITED"), (503, "SERVICE_ERROR"), (400, "HTTP_ERROR")):
        with pytest.raises(nf.NetworkLookupError) as e:
            nf.gauge_flowlines("01031500", "both", 5, client=cl(fake(status=status)))
        assert e.value.code == code and e.value.retryable == (status != 400)
    for kw in ({"direction": "sideways"}, {"distance_km": 0}, {"distance_km": 201}, {"distance_km": True}, {"max_features": 0}, {"max_features": 751}):
        with pytest.raises(ValueError):
            nf.gauge_flowlines("01031500", **{"direction": "both", "distance_km": 5, **kw}, client=cl(fake()))


def test_one_deadline_covers_both_requests_and_each_timeout_is_what_is_left():
    seen = []
    inner = fake()

    def get(url, params, timeout=None, bytes_cap=None):
        seen.append((timeout, bytes_cap))
        return inner(url, params)
    nf.gauge_flowlines("01031500", "both", 5, client=cl(get, deadline_s=12.0))
    assert len(seen) == 2 and all(0 < t <= 12.0 and cap == nf.MAX_RESPONSE_BYTES for t, cap in seen)
    with pytest.raises(nf.NetworkLookupError) as e:
        nf.gauge_flowlines("01031500", "both", 5, client=cl(fake(), deadline_s=-1.0))
    assert e.value.code == "DEADLINE_EXCEEDED"
    with pytest.raises(nf.NetworkLookupError) as e:
        nf.gauge_flowlines("01031500", "both", 5, client=gn.Client(fake(), max_requests=1, bytes_cap=100))
    assert e.value.code == "BUDGET_EXHAUSTED"


def test_the_shared_getter_caps_the_body_and_always_closes_the_response(monkeypatch):
    import requests

    closed = []

    class Raw:
        def __init__(self, n):
            self.n = n

        def read(self, k, decode_content=True):
            return b"x" * min(k, self.n)

    class R:
        status_code = 200

        def __init__(self, n):
            self.raw, self.content = Raw(n), b"{}"

        def close(self):
            closed.append(1)

    url = nf.NLDI_BASE + "/nwissite/USGS-1/navigation/UT/flowlines"
    monkeypatch.setattr(requests, "get", lambda *a, **k: R(500))
    with pytest.raises(gn.NetworkLookupError) as e:
        gn._default_get(url, None, bytes_cap=100)
    assert e.value.code == "RESPONSE_TOO_LARGE" and closed == [1]
    monkeypatch.setattr(requests, "get", lambda *a, **k: R(50))
    assert gn._default_get(url, None, bytes_cap=100) == (200, b"x" * 50) and closed == [1, 1]
    with pytest.raises(gn.NetworkLookupError) as e:
        gn._default_get("https://example.com/x", None)
    assert e.value.code == "HOST_NOT_ALLOWED"


# ---------------------------------------------------------------- the tool

def test_tool_writes_geometry_to_the_workspace_records_it_and_keeps_it_out_of_the_result(env, mcp_server, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    s = HydroSession.load(SID)
    s.workspace_dir = str(ws)
    s.save()
    monkeypatch.setattr(gn, "_default_get", fake())
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
    monkeypatch.setattr(gn, "_default_get", fake())
    out = fn("01031500", "upstream", 10)
    assert out["data"]["geometry_file"] is None and "pass session_id" in out["data"]["geometry_note"]


def test_tool_typed_answers(env, mcp_server, monkeypatch):
    from ai_hydro.mcp import tools_flowlines as t

    fn = t.gauge_flowlines.fn if hasattr(t.gauge_flowlines, "fn") else t.gauge_flowlines
    out = fn("GB-39001")
    assert out["available"] is False and out["code"] == "US_ONLY"
    monkeypatch.setattr(gn, "_default_get", fake(missing=True))
    assert fn("01031500", session_id=SID)["code"] == "GAUGE_NOT_IN_NLDI"
    monkeypatch.setattr(gn, "_default_get", fake(status=503))
    out = fn("01031500", session_id=SID)
    assert out["error"] is True and out["retryable"] is True
    assert fn("01031500", "diagonal")["code"] == "INVALID_PARAMETER"


def test_a_flowline_run_goes_to_the_map_through_map_add_layer_from_run(env, mcp_server, monkeypatch, tmp_path):
    """The workspace file the tool wrote is the run's geometry artifact; the map tool finds it by run id."""
    ws = tmp_path / "ws2"
    ws.mkdir()
    s = HydroSession.load(SID)
    s.workspace_dir = str(ws)
    s.save()
    monkeypatch.setattr(gn, "_default_get", fake())
    err, body, _ = call(mcp_server, "gauge_flowlines", {"gauge_id": "01031500", "distance_km": 20, "session_id": SID})
    assert not err and body["data"]["geometry_file"], body
    err, added, _ = call(mcp_server, "map_add_layer_from_run", {"session_id": SID, "run_id": body["_run_id"]})
    assert not err and added.get("error") is not True, added
    assert added["ok"] is True and added["hash_checked"] is True                 # queued, and the file matched the digest the run recorded
    assert added["run_id"] == body["_run_id"] and added["provenance"]["_run_id"] == body["_run_id"]
    assert body["data"]["geometry_file"] in str(added["artifact_id"])           # the artifact the map tool used is the file the tool wrote


def _with_workspace(tmp_path, name):
    ws = tmp_path / name
    ws.mkdir()
    s = HydroSession.load(SID)
    s.workspace_dir = str(ws)
    s.save()


def test_the_layer_carries_what_the_tool_recorded_about_it(env, mcp_server, monkeypatch, tmp_path):
    """gauge, direction, distance, whether it was cut and the count go from the run onto the layer envelope, by the real tools."""
    _with_workspace(tmp_path, "ws3")
    monkeypatch.setattr(gn, "_default_get", fake())
    err, body, _ = call(mcp_server, "gauge_flowlines", {"gauge_id": "USGS-01031500", "direction": "upstream", "distance_km": 20, "session_id": SID})
    assert not err, body
    row = run_log()[body["_run_id"]]
    assert row["layer_meta"] == {"gauge_id": "01031500", "direction": "upstream", "distance_km": "20", "truncated": "false", "n_features": "3"}
    err, added, _ = call(mcp_server, "map_add_layer_from_run", {"session_id": SID, "run_id": body["_run_id"]})
    assert not err and added["ok"] is True, added
    p = added["provenance"]
    assert (p["gauge_id"], p["direction"], p["distance_km"], p["truncated"], p["n_features"]) == ("01031500", "upstream", "20", "false", "3")
    assert p["_run_id"] == body["_run_id"] and p["tool"] == "gauge_flowlines" and p["study_id"] == SID   # the envelope's own keys are untouched


def test_a_cut_trace_says_truncated_true_on_the_layer(env, mcp_server, monkeypatch, tmp_path):
    _with_workspace(tmp_path, "ws4")
    many = [line(1000 + i, -68.0 - i * 0.001, 47.0, n=2) for i in range(nf.MAX_FEATURES_PER_DIRECTION + 1)]
    monkeypatch.setattr(gn, "_default_get", fake(ut=many))
    err, body, _ = call(mcp_server, "gauge_flowlines", {"gauge_id": "01031500", "direction": "upstream", "distance_km": 200, "session_id": SID})
    assert not err and body["data"]["truncated"] is True, body
    err, added, _ = call(mcp_server, "map_add_layer_from_run", {"session_id": SID, "run_id": body["_run_id"]})
    assert added["provenance"]["truncated"] == "true" and added["provenance"]["n_features"] == str(nf.MAX_FEATURES_PER_DIRECTION)


def test_only_the_five_fact_keys_get_through_and_nothing_else_can():
    from ai_hydro.mcp.enforcement import LAYER_FACT_KEYS, sanitize_layer_meta

    assert LAYER_FACT_KEYS == {"gauge_id", "direction", "distance_km", "truncated", "n_features"}
    assert sanitize_layer_meta(None) == {} and sanitize_layer_meta("x") == {} and sanitize_layer_meta({}) == {}
    refused = ["source_path", "workspace_path", "path", "source_status", "validation_status", "claim_id", "uncertainty",
               "raster_path", "geojson_cache_path", "project_id", "item_id", "session_id", "study_id", "tool", "source",
               "_run_id", "retrieved_at", "chat_id", "display_name", "_fact_keys", "unknown"]
    assert sanitize_layer_meta({k: "x" for k in refused}) == {}
    got = sanitize_layer_meta({"gauge_id": "01031500", "truncated": True, "n_features": 3, "distance_km": 20.0,
                               "direction": "  ", "source_path": "/etc/passwd", "claim_id": "c1"})
    assert got == {"gauge_id": "01031500", "truncated": "true", "n_features": "3", "distance_km": "20.0"}
    assert sanitize_layer_meta({"gauge_id": {"a": 1}, "n_features": [1], "distance_km": float("nan")}) == {}
    assert len(sanitize_layer_meta({"gauge_id": "x" * 500})["gauge_id"]) == 200


def test_another_tools_run_row_has_no_layer_meta_key(env, mcp_server):
    """post_run for a result without meta.layer_meta writes the same row shape as before (no new key)."""
    from ai_hydro.mcp.enforcement import post_run

    out = post_run("some_tool", SID, {"data": {"x": 1}, "meta": {"tool": "some_tool"}}, inputs={"a": 1})
    assert "layer_meta" not in run_log()[out["_run_id"]]


def test_the_tool_is_in_the_same_discovery_domain_as_gauge_network_context():
    from ai_hydro.mcp.tools_discovery import _DOMAIN_PREFIXES

    dom = [d for d, pre in _DOMAIN_PREFIXES.items() if "gauge_network_context" in pre]
    assert dom and all("gauge_flowlines" in _DOMAIN_PREFIXES[d] for d in dom)
