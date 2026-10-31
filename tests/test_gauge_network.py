"""gauge_network_context: the client (canned SYNTHETIC service answers, no network) and the tool wrapper."""
from __future__ import annotations

import json

import pytest

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
    c = gn.Client(fake_get(calls, **{k: kw.pop(k) for k in ("missing", "status", "drop_area") if k in kw}))
    return gn.gauge_network("01000500", kw.pop("distance_km", 100), client=c,
                            geoconnex=kw.pop("geoconnex", lambda s: type("L", (), {"status": "resolved", "uri": "https://geoconnex.us/ref/gages/9"})()))


def test_lists_streamgages_each_side_drops_other_sites_and_the_gauge_itself():
    r = run()
    assert r["available"] is True
    assert [g["usgs_id"] for g in r["upstream"]["gauges"]] == ["USGS-01000300", "USGS-01000400"]       # largest area first
    assert [g["usgs_id"] for g in r["downstream"]["gauges"]] == ["USGS-01001000"]
    assert r["upstream"]["excluded_without_daily_discharge"] == 1 and r["downstream"]["excluded_without_daily_discharge"] == 1
    assert ORIGIN not in [g["usgs_id"] for g in r["upstream"]["gauges"] + r["downstream"]["gauges"]]
    assert not any("LAKE" in (g["name"] or "").upper() for g in r["upstream"]["gauges"] + r["downstream"]["gauges"])


def test_areas_ratio_record_and_identifiers_are_labelled_and_exact():
    r = run()
    up = r["upstream"]["gauges"][0]
    assert up["drainage_area_sqmi"] == 40.0 and up["drainage_area_km2"] == pytest.approx(40.0 * 2.589988, abs=0.1)
    assert up["drainage_area_ratio_to_gauge"] == 0.4
    assert up["daily_discharge_record"] == {"begin": "1990-10-01", "end": "2026-10-01"}
    assert r["downstream"]["gauges"][0]["drainage_area_ratio_to_gauge"] == 3.0
    g = r["gauge"]
    assert g["usgs_id"] == ORIGIN and g["drainage_area_sqmi"] == 100.0
    assert g["mainstem_pid"] == "https://geoconnex.us/ref/mainstems/1"
    assert g["geoconnex_gage"] == {"status": "resolved", "pid": "https://geoconnex.us/ref/gages/9"}
    assert "United States only" in r["limits"] and "not evidence that its flows are independent" in r["limits"]


def test_a_missing_drainage_area_is_none_not_zero_or_a_guess():
    r = run(drop_area=(ORIGIN,))
    assert r["gauge"]["drainage_area_sqmi"] is None
    assert all(g["drainage_area_ratio_to_gauge"] is None for g in r["upstream"]["gauges"])
    r = run(drop_area=("USGS-01000300",))
    g = next(g for g in r["upstream"]["gauges"] if g["usgs_id"] == "USGS-01000300")
    assert g["drainage_area_sqmi"] is None and g["drainage_area_km2"] is None


def test_provenance_is_returned_and_requests_are_bounded_and_public_only():
    calls = []
    r = run(calls=calls, distance_km=250)
    s = r["sources"]
    assert len(s["request_urls"]) == len(s["response_sha256"]) == len(calls) == 4         # UT, DM, series, locations
    assert all(len(h) == 64 for h in s["response_sha256"])
    nav = [c for c in calls if "/navigation/" in c[0]]
    assert {c[1]["distance"] for c in nav} == {250}                                         # always a bounded radius
    assert all(u.startswith(("https://api.water.usgs.gov/", "https://api.waterdata.usgs.gov/")) for u, _ in calls)
    assert all(c[1].get("monitoring_location_id") or c[1].get("id") or "distance" in c[1] for c in calls)


def test_outside_the_us_and_unknown_gauges_are_not_available_not_guessed():
    for bad in ("GB-39001", "", "abc", "0101"):
        with pytest.raises(gn.NotAvailable) as e:
            gn.parse_gauge_id(bad)
        assert e.value.code == "US_ONLY"
    with pytest.raises(gn.NotAvailable) as e:
        gn.gauge_network("01000500", 100, client=gn.Client(fake_get(missing=("USGS-01000500",))))
    assert e.value.code == "GAUGE_NOT_IN_NLDI" and "outside the United States" in e.value.message
    assert gn.parse_gauge_id("USGS-01013500") == "01013500" and gn.parse_gauge_id(" 01013500 ") == "01013500"


def test_service_trouble_is_a_retryable_error_and_bad_distance_is_refused():
    with pytest.raises(gn.NetworkLookupError) as e:
        run(status=503)
    assert e.value.retryable is True and e.value.code == "SERVICE_ERROR"
    with pytest.raises(gn.NetworkLookupError) as e:
        run(status=429)
    assert e.value.code == "RATE_LIMITED"
    for bad in (0, 501, "100", True):
        with pytest.raises(ValueError):
            run(distance_km=bad)


def test_a_failing_geoconnex_lookup_is_reported_and_does_not_hide_the_network():
    def boom(site):
        raise RuntimeError("geoconnex down")
    r = run(geoconnex=boom)
    assert r["gauge"]["geoconnex_gage"]["status"] == "unresolved" and r["upstream"]["n"] == 2


def test_per_side_limit_is_reported():
    global UT
    keep = UT
    try:
        extra = [nldi_feature(f"USGS-0200{i:04d}", f"g{i}", -68.0, 47.0, 100 + i) for i in range(60)]
        UT = keep + extra
        for e in extra:
            SERIES[e["properties"]["identifier"]] = ("2000-01-01", "2020-01-01")
            AREAS[e["properties"]["identifier"]] = 10.0
        r = run()
        assert r["upstream"]["n"] == gn.MAX_PER_DIRECTION
        assert r["upstream"]["omitted_beyond_limit"] == (2 + 60) - gn.MAX_PER_DIRECTION
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
    assert out["available"] is True and out["upstream"]["n"] == 2

    out = fn("GB-39001")
    assert out == {"available": False, "code": "US_ONLY", "message": out["message"], "limits": gn.LIMITS}

    monkeypatch.setattr(gn, "_default_get", fake_get(status=503))
    out = fn("01000500")
    assert out["error"] is True and out["retryable"] is True

    out = fn("01000500", 9999)
    assert out["code"] == "INVALID_PARAMETER"
