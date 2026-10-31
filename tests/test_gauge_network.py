"""gauge_network_context: the client (canned SYNTHETIC service answers, no network) and the tool wrapper."""
from __future__ import annotations

import json

import pytest

from test_series_tools import SID, call, env, mcp_server, run_log  # noqa: F401  (fixtures)

from ai_hydro.data import gauge_network as gn

ORIGIN = "USGS-01000500"


def nldi_feature(ident, name, lon, lat, comid=1, mainstem=None):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"identifier": ident, "name": name, "comid": comid, "reachcode": "01010003000373",
                           "mainstem": mainstem, "uri": f"https://waterdata.usgs.gov/monitoring-location/{ident}"}}


UT = [nldi_feature(ORIGIN, "ORIGIN", -68.0, 47.0, 10, "https://geoconnex.us/ref/mainstems/1"),
      nldi_feature("USGS-01000300", "Upper gauge", -68.1, 47.1, 11),
      nldi_feature("USGS-470142068262200", "A LAKE STATION", -68.4, 47.0, 12),          # no discharge series
      nldi_feature("USGS-01000400", "Tributary gauge", -68.2, 47.2, 13)]
DM = [nldi_feature(ORIGIN, "ORIGIN", -68.0, 47.0, 10),
      nldi_feature("USGS-01001000", "Lower gauge", -67.9, 46.9, 14),
      nldi_feature("USGS-464843068455200", "ANOTHER LAKE", -67.8, 46.8, 15)]
SERIES = {ORIGIN: ("1950-01-01", "2026-10-01"), "USGS-01000300": ("1990-10-01", "2026-10-01"),
          "USGS-01000400": ("1996-05-01", "2005-09-30"), "USGS-01001000": ("1930-01-01", "2026-10-01")}
AREAS = {ORIGIN: 100.0, "USGS-01000300": 40.0, "USGS-01000400": 25.0, "USGS-01001000": 300.0}


def fake_get(calls=None, *, missing=(), status=200, drop_area=()):
    def get(url, params):
        if calls is not None:
            calls.append((url, dict(params or {})))
        if status != 200:
            return status, b"{}"
        if "/navigation/UT/" in url:
            if any(m in url for m in missing):
                return 404, b"{}"
            return 200, json.dumps({"features": UT}).encode()
        if "/navigation/DM/" in url:
            return 200, json.dumps({"features": DM}).encode()
        if url.endswith("time-series-metadata/items"):
            ids = params["monitoring_location_id"].split(",")
            feats = [{"properties": {"monitoring_location_id": i, "parameter_code": "00060", "statistic_id": "00003",
                                     "begin": SERIES[i][0], "end": SERIES[i][1]}} for i in ids if i in SERIES]
            return 200, json.dumps({"features": feats}).encode()
        if url.endswith("monitoring-locations/items"):
            ids = params["id"].split(",")
            feats = [{"geometry": {"type": "Point", "coordinates": [-68.0, 47.0]},
                      "properties": {"id": i, "monitoring_location_name": f"{i} name", "site_type": "Stream",
                                     "drainage_area": None if i in drop_area else AREAS[i]}} for i in ids if i in AREAS]
            return 200, json.dumps({"features": feats}).encode()
        return 404, b"{}"
    return get


def run(**kw):
    calls = kw.pop("calls", None)
    cl = kw.pop("client", None) or gn.Client(fake_get(calls, **{k: kw.pop(k) for k in ("missing", "status", "drop_area") if k in kw}))
    return gn.gauge_network("01000500", kw.pop("distance_km", 100), client=cl,
                            geoconnex=kw.pop("geoconnex", lambda s: type("L", (), {"status": "resolved", "uri": "https://geoconnex.us/ref/gages/9"})()))


def test_lists_streamgages_each_side_drops_other_sites_and_the_gauge_itself():
    r = run()
    assert r["available"] is True and r["incomplete"] is False
    assert list(r["upstream_gauges"]) == ["USGS-01000300", "USGS-01000400"]            # nearest first, keyed by id
    assert list(r["downstream_gauges"]) == ["USGS-01001000"]
    assert r["upstream_excluded_without_daily_discharge"] == 1 and r["downstream_excluded_without_daily_discharge"] == 1
    every = {**r["upstream_gauges"], **r["downstream_gauges"]}
    assert ORIGIN not in every and not any("LAKE" in (g["name"] or "").upper() for g in every.values())


def test_areas_ratio_record_and_identifiers_are_labelled_and_exact():
    r = run()
    up = r["upstream_gauges"]["USGS-01000300"]
    assert up["drainage_area_sqmi"] == 40.0 and up["drainage_area_km2"] == pytest.approx(40.0 * 2.589988, abs=0.1)
    assert up["drainage_area_ratio_to_gauge"] == 0.4
    assert up["daily_discharge_record"] == {"begin": "1990-10-01", "end": "2026-10-01"}
    assert r["downstream_gauges"]["USGS-01001000"]["drainage_area_ratio_to_gauge"] == 3.0
    g = r["gauge"]
    assert g["usgs_id"] == ORIGIN and g["drainage_area_sqmi"] == 100.0
    assert g["mainstem_pid"] == "https://geoconnex.us/ref/mainstems/1"
    assert (g["geoconnex_gage_status"], g["geoconnex_gage_pid"]) == ("resolved", "https://geoconnex.us/ref/gages/9")
    assert "United States only" in r["limits"] and "not evidence that its flows are independent" in r["limits"]


def test_a_missing_drainage_area_is_none_not_zero_or_a_guess():
    r = run(drop_area=(ORIGIN,))
    assert r["gauge"]["drainage_area_sqmi"] is None
    assert all(g["drainage_area_ratio_to_gauge"] is None for g in r["upstream_gauges"].values())
    r = run(drop_area=("USGS-01000300",))
    g = r["upstream_gauges"]["USGS-01000300"]
    assert g["drainage_area_sqmi"] is None and g["drainage_area_km2"] is None


def test_provenance_is_strings_includes_geoconnex_and_requests_are_bounded_and_public_only():
    calls = []
    r = run(calls=calls, distance_km=250)
    urls, hashes = r["request_urls"], r["response_sha256"]
    assert isinstance(urls, str) and isinstance(hashes, str)                              # citable: not lists
    assert len(urls.splitlines()) == len(calls) + 1 and "reference.geoconnex.us" in urls   # UT, DM, series, locations + Geoconnex
    assert len(hashes.split()) == len(calls) == 4 and all(len(h) == 64 for h in hashes.split())
    nav = [c for c in calls if "/navigation/" in c[0]]
    assert {c[1]["distance"] for c in nav} == {250}
    assert all(u.startswith(("https://api.water.usgs.gov/", "https://api.waterdata.usgs.gov/")) for u, _ in calls)


def test_outside_the_us_and_unknown_gauges_are_not_available_not_guessed():
    for bad in ("GB-39001", "", "abc", "0101", "\u0660\u0661\u0660\u0661\u0660\u0660\u0665\u0660"):     # the last is Arabic-Indic digits
        with pytest.raises(gn.NotAvailable) as e:
            gn.parse_gauge_id(bad)
        assert e.value.code == "US_ONLY"
    with pytest.raises(gn.NotAvailable) as e:
        gn.gauge_network("01000500", 100, client=gn.Client(fake_get(missing=("USGS-01000500",))))
    assert e.value.code == "GAUGE_NOT_IN_NLDI" and "outside the United States" in e.value.message
    assert gn.parse_gauge_id("USGS-01013500") == "01013500" and gn.parse_gauge_id(" 01013500 ") == "01013500"


def test_service_trouble_is_a_partial_answer_with_the_reason_never_a_bare_error():
    for status, code in ((503, "SERVICE_ERROR"), (429, "RATE_LIMITED")):
        r = run(status=status)
        assert r["available"] is True and r["incomplete"] is True and code in r["incomplete_reasons"]
        assert r["upstream_gauges"] == {} and r["downstream_gauges"] == {}
    for bad in (0, 501, "100", True):
        with pytest.raises(ValueError):
            run(distance_km=bad)


def test_a_429_part_way_keeps_what_was_confirmed():
    inner = fake_get()
    n = {"i": 0}

    def get(url, params):
        n["i"] += 1
        return (429, b"{}") if url.endswith("monitoring-locations/items") else inner(url, params)
    r = run(client=gn.Client(get))
    assert r["incomplete"] is True and "site attributes" in r["incomplete_reasons"]
    assert set(r["upstream_gauges"]) == {"USGS-01000300", "USGS-01000400"}          # confirmed streamgages kept, without attributes
    assert r["upstream_gauges"]["USGS-01000300"]["drainage_area_sqmi"] is None


def test_the_request_budget_and_deadline_stop_the_call_with_a_partial_answer():
    r = run(client=gn.Client(fake_get(), max_requests=2))
    assert r["incomplete"] is True and "BUDGET_EXHAUSTED" in r["incomplete_reasons"]
    assert len(r["request_urls"].splitlines()) <= 3
    r = run(client=gn.Client(fake_get(), deadline_s=-1.0))
    assert r["incomplete"] is True and "DEADLINE_EXCEEDED" in r["incomplete_reasons"]


def test_candidates_are_checked_nearest_first_and_capped(monkeypatch):
    global UT
    keep = UT
    try:
        far = [nldi_feature(f"USGS-0300{i:04d}", f"far{i}", -60.0 - i * 0.01, 40.0, 200 + i) for i in range(100)]
        near = nldi_feature("USGS-01000999", "near", -68.0, 47.001, 999)
        UT = keep + far + [near]
        SERIES["USGS-01000999"] = ("2000-01-01", "2020-01-01"); AREAS["USGS-01000999"] = 5.0
        r = run()
        assert r["candidates_nldi_total"] > gn.MAX_CANDIDATES and r["candidates_not_checked"] > 0
        assert "USGS-01000999" in r["upstream_gauges"]                  # the near one made the cut, the far ones did not
        assert list(r["upstream_gauges"])[0] == "USGS-01000999"          # and sorts first
    finally:
        UT = keep
        SERIES.pop("USGS-01000999", None); AREAS.pop("USGS-01000999", None)


def test_a_failing_geoconnex_lookup_is_reported_and_does_not_hide_the_network():
    def boom(site):
        raise RuntimeError("geoconnex down")
    r = run(geoconnex=boom)
    assert r["gauge"]["geoconnex_gage_status"] == "unresolved" and r["upstream_n"] == 2


def test_the_usgs_key_goes_to_the_usgs_data_host_only(monkeypatch):
    import requests
    seen = []

    class R:
        status_code = 200
        content = b"{}"

    monkeypatch.setattr(requests, "get", lambda url, **kw: seen.append((url, kw["headers"])) or R())
    monkeypatch.setenv("API_USGS_PAT", "k")
    gn._default_get("https://api.waterdata.usgs.gov/ogcapi/v1/x", None)
    gn._default_get("https://api.water.usgs.gov/nldi/linked-data/x", None)
    assert seen[0][1].get("X-Api-Key") == "k" and "X-Api-Key" not in seen[1][1]
    monkeypatch.delenv("API_USGS_PAT")
    gn._default_get("https://api.waterdata.usgs.gov/ogcapi/v1/x", None)
    assert "X-Api-Key" not in seen[2][1]
    with pytest.raises(gn.NetworkLookupError):
        gn._default_get("https://example.com/", None)


def test_per_side_limit_is_reported():
    global UT
    keep = UT
    try:
        extra = [nldi_feature(f"USGS-0200{i:04d}", f"g{i}", -68.0, 47.0 + i * 0.0001, 100 + i) for i in range(30)]
        UT = keep + extra
        for e in extra:
            SERIES[e["properties"]["identifier"]] = ("2000-01-01", "2020-01-01")
            AREAS[e["properties"]["identifier"]] = 10.0
        r = run()
        assert r["upstream_n"] == gn.MAX_PER_DIRECTION == len(r["upstream_gauges"])
        assert r["upstream_omitted_beyond_limit"] == (2 + 30) - gn.MAX_PER_DIRECTION
    finally:
        UT = keep
        for k in [k for k in SERIES if k.startswith("USGS-0200")]:
            SERIES.pop(k); AREAS.pop(k)


# --------------------------------------------------------------------------- the tool

def test_tool_wrapper_maps_outcomes_to_typed_answers(monkeypatch):
    from ai_hydro.mcp import tools_gauge_network as t

    fn = t.gauge_network_context.fn if hasattr(t.gauge_network_context, "fn") else t.gauge_network_context
    import aihydro_watershed.identity as ident

    monkeypatch.setattr(ident, "resolve_geoconnex_gage",
                        lambda site: ident.GeoconnexLookup(site, "resolved", "https://geoconnex.us/ref/gages/9"))
    monkeypatch.setattr(gn, "_default_get", fake_get())
    out = fn("USGS-01000500", 100)
    assert out["data"]["available"] is True and out["data"]["upstream_n"] == 2

    out = fn("GB-39001")
    assert out == {"available": False, "code": "US_ONLY", "message": out["message"], "limits": gn.LIMITS}

    monkeypatch.setattr(gn, "_default_get", fake_get(status=503))
    out = fn("01000500")
    assert out["data"]["incomplete"] is True

    out = fn("01000500", 9999)
    assert out["code"] == "INVALID_PARAMETER"


def test_the_tool_degrades_when_the_geoconnex_module_cannot_import(monkeypatch):
    import sys
    from ai_hydro.mcp import tools_gauge_network as t

    fn = t.gauge_network_context.fn if hasattr(t.gauge_network_context, "fn") else t.gauge_network_context
    monkeypatch.setitem(sys.modules, "aihydro_watershed.identity", None)       # `import` raises ImportError
    monkeypatch.setattr(gn, "_default_get", fake_get())
    out = fn("01000500")
    assert out["data"]["available"] is True and out["data"]["gauge"]["geoconnex_gage_status"] == "unavailable"


def test_neighbours_are_sealed_and_citable_through_the_real_handler(env, mcp_server, monkeypatch):
    """Through the MCP handler with a study: key_outputs hold the neighbours by id, so a marker can cite one."""
    from ai_hydro.audit.evidence import resolve_marker_path
    import aihydro_watershed.identity as ident

    monkeypatch.setattr(gn, "_default_get", fake_get())
    monkeypatch.setattr(ident, "resolve_geoconnex_gage",
                        lambda site: ident.GeoconnexLookup(site, "resolved", "https://geoconnex.us/ref/gages/9"))
    err, body, _ = call(mcp_server, "gauge_network_context", {"gauge_id": "01000500", "session_id": SID})
    assert not err and body["data"]["available"] is True, body
    row = run_log()[body["_run_id"]]
    ko = row["key_outputs"]
    assert set(ko["upstream_gauges"]) == {"USGS-01000300", "USGS-01000400"} and len(ko["upstream_gauges"]) <= 20
    assert isinstance(ko["request_urls"], str) and isinstance(ko["response_sha256"], str)
    found, val, _p = resolve_marker_path(row, "key_outputs.upstream_gauges.USGS-01000300.drainage_area_sqmi")
    assert found and val == 40.0
    found, val, _p = resolve_marker_path(row, "key_outputs.downstream_gauges.USGS-01001000.drainage_area_ratio_to_gauge")
    assert found and val == 3.0
    found, _v, _p = resolve_marker_path(row, "key_outputs.gauge.drainage_area_sqmi")
    assert found
