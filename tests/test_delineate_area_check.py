"""``delineate_watershed_from_point`` uses the area check of aihydro-watershed >= 0.4.6 (gauge_id -> reported-area
lookup), surfaces ``area_check`` / ``reliability_state`` / ``ambiguous_outlet`` and warns on a mismatch. No network:
the stand-in router runs the real ``area_verification`` code with an injected ``http_get`` (the watershed tests' seam)."""
from __future__ import annotations

import importlib.metadata as md

import pytest

from ai_hydro.mcp import watershed_reliability as wr
from test_train_289_fixes import PB, _fn  # noqa: F401

pytest.importorskip("aihydro_watershed.delineation.area_verification")
from aihydro_watershed.delineation import area_verification as av  # noqa: E402

POLY_KM2 = 50.0
SQMI = av.SQMI_TO_KM2


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _usgs_body(km2):
    return {"features": [{"properties": {"drainage_area": km2 / SQMI}}]}


@pytest.fixture
def router(monkeypatch):
    """A stand-in ``delineate_from_point`` with the 0.4.6 signature; ``state['http']`` is the injected lookup."""
    from ai_hydro.core import HydroMeta, HydroResult
    import ai_hydro.analysis.delineation as dl
    import ai_hydro.mcp.map_events as me

    state = {"http": lambda *a, **k: _Resp(200, _usgs_body(POLY_KM2)), "http_calls": [], "extra": {}}
    calls: list[dict] = []

    def fake(lat, lon, *, expected_area_km2=None, gauge_id=None, lookup_reported_area=False, **kw):
        calls.append({"expected_area_km2": expected_area_km2, "gauge_id": gauge_id,
                      "lookup_reported_area": lookup_reported_area, **kw})

        def http(*a, **k):
            state["http_calls"].append(a)
            return state["http"](*a, **k)

        reported = av.resolve_reported_area(
            expected_area_km2=expected_area_km2, gauge_id=gauge_id, lookup=lookup_reported_area,
            providers=[lambda g: av.usgs_reported_area(g, http_get=http)])
        check = av.build_area_check(POLY_KM2, reported)
        mismatch = check["status"] == av.STATUS_MISMATCH
        data = {"geometry_geojson": PB, "area_km2": POLY_KM2, "method_used": "stub", "quality_flags": [],
                "area_check": check,
                "reliability_state": av.reliability_state(check, False, legacy_unreliable=mismatch),
                **({"reliability": "unreliable", "reliability_reason": "AREA_MISMATCH", "quality_flags": ["AREA_MISMATCH"]}
                   if mismatch else {}),
                **state["extra"]}
        return HydroResult(data=data, meta=HydroMeta(tool="x", version="0"))

    layers: list[dict] = []
    monkeypatch.setattr(dl, "delineate_from_point", fake)
    monkeypatch.setattr(me, "push_layer", lambda *a, **k: layers.append(k) or True)
    monkeypatch.setattr(me, "push_gauge_point", lambda *a, **k: True)
    return calls, state, layers


def _tool(**kw):
    from ai_hydro.mcp import tools_analysis as ta

    return _fn(ta.delineate_watershed_from_point)(lat=44.5, lon=-69.7, **kw)


def test_installed_watershed_has_the_area_check():
    assert tuple(int(x) for x in md.version("aihydro-watershed").split(".")[:3]) >= (0, 4, 6)


def test_gauge_id_triggers_the_lookup_and_verifies(router):
    calls, state, layers = router
    out = _tool(session_id="ac-ok", gauge_id="1013500")  # short id is zero-padded like the other gauge tools
    assert not out.get("error"), out
    assert calls[0]["gauge_id"] == "01013500" and calls[0]["lookup_reported_area"] is True
    assert len(state["http_calls"]) == 1
    d = out["data"]
    assert d["area_check"]["status"] == "verified_by_area" and d["area_check"]["source"] == wr.AREA_LOOKUP_SOURCE
    assert d["reliability_state"] == "verified"
    assert "AREA CHECK" in out["_note"] and "not proof" in out["_note"]
    assert "quality_flags" not in out and "watershed_reliability_warning" not in out
    prov = out["meta"]["area_lookup"]
    assert prov["host"] == "api.waterdata.usgs.gov" and prov["attempted"] is True and prov["gauge_id"] == "01013500"
    assert layers[0]["metadata"]["reliability_state"] == "verified"
    assert layers[0]["metadata"]["area_check_status"] == "verified_by_area"


def test_expected_area_takes_precedence_and_no_request_is_made(router):
    calls, state, _ = router
    out = _tool(session_id="ac-exp", gauge_id="01013500", expected_area_km2=POLY_KM2)
    assert calls[0]["expected_area_km2"] == POLY_KM2 and calls[0]["gauge_id"] == "01013500"
    assert state["http_calls"] == []
    assert out["data"]["area_check"]["source"] == "caller_supplied"
    assert out["meta"]["area_lookup"]["attempted"] is False


def test_verify_area_false_skips_the_lookup(router):
    calls, state, _ = router
    out = _tool(session_id="ac-off", gauge_id="01013500", verify_area=False)
    assert calls[0]["lookup_reported_area"] is False
    assert state["http_calls"] == []
    assert out["data"]["area_check"]["status"] == "area_unverified"
    assert out["data"]["reliability_state"] == "unverified"
    assert "area_lookup" not in out["meta"]
    assert "AREA CHECK NOT PERFORMED" not in out["_note"]  # the caller turned it off; not a failure


def test_mismatch_is_a_warning_and_not_verified(router):
    _, state, _ = router
    state["http"] = lambda *a, **k: _Resp(200, _usgs_body(500.0))  # polygon 50 vs reported 500 km2
    out = _tool(session_id="ac-bad", gauge_id="01013500")
    assert not out.get("error"), out
    d = out["data"]
    assert d["area_check"]["status"] == "area_mismatch" and d["reliability_state"] != "verified"
    assert d["reliability"] == "unreliable"  # the existing reliability mechanics see it
    assert "AREA CHECK MISMATCH" in out["_note"] and "NOT verified" in out["_note"]
    flag = out["quality_flags"][0]
    assert flag["validator"] == "watershed_reliability" and flag["metadata"]["quality_flag"] == "area_mismatch"
    assert flag["status"] == "warning" and "NOT verified" in flag["message"]
    assert out["watershed_reliability_warning"] == flag["message"]
    assert wr.reliability_of(d) == "unreliable"


@pytest.mark.parametrize("resp", [
    lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")),
    lambda *a, **k: _Resp(503, {}),
    lambda *a, **k: _Resp(200, {"features": []}),
    lambda *a, **k: _Resp(200, ["not", "an", "object"]),
])
def test_lookup_failure_is_reported_honestly_and_never_raises(router, resp):
    _, state, _ = router
    state["http"] = resp
    out = _tool(session_id="ac-fail", gauge_id="01013500")
    assert not out.get("error"), out
    chk = out["data"]["area_check"]
    assert chk["status"] == "area_unverified" and chk["reason"] and chk["reported_area_km2"] is None
    assert out["data"]["reliability_state"] == "unverified"
    assert "AREA CHECK NOT PERFORMED" in out["_note"] and "unverified" in out["_note"]
    assert "quality_flags" not in out
    assert out["meta"]["area_lookup"]["attempted"] is True and out["meta"]["area_lookup"]["status"] == "area_unverified"


def test_fields_are_passed_through_including_ambiguous_outlet(router):
    _, state, _ = router
    state["extra"] = {"ambiguous_outlet": {"affects_reliability": False, "location_class": "conus",
                                           "candidates": [{"area_km2": 12.0}, {"area_km2": 900.0}]}}
    out = _tool(session_id="ac-amb", gauge_id="01013500")
    amb = out["data"]["ambiguous_outlet"]
    assert amb["location_class"] == "conus" and amb["affects_reliability"] is False and len(amb["candidates"]) == 2
    assert "informational inside the conterminous US" in out["_note"] and "location_class=conus" in out["_note"]
    state["extra"] = {"ambiguous_outlet": {"affects_reliability": True, "location_class": "non_conus", "candidates": []}}
    out = _tool(session_id="ac-amb2", gauge_id="01013500")
    assert "affects reliability" in out["_note"] and "non_conus" in out["_note"]


def test_call_without_gauge_id_is_unchanged(router):
    calls, state, layers = router
    out = _tool(session_id="ac-old")
    assert calls[0]["gauge_id"] is None and calls[0]["lookup_reported_area"] is False
    assert state["http_calls"] == []
    assert "meta" in out and "area_lookup" not in out["meta"]
    assert "AREA CHECK" not in out["_note"] and "quality_flags" not in out
    assert out["data"]["area_check"]["status"] == "area_unverified"  # the package's own "no area" answer, passed through


def test_blank_gauge_id_is_treated_as_absent_and_bad_one_is_an_error(router):
    calls, _, _ = router
    _tool(session_id="ac-blank", gauge_id="  ")
    assert calls[0]["gauge_id"] is None and calls[0]["lookup_reported_area"] is False
    out = _tool(session_id="ac-bad-id", gauge_id="not-a-gauge")
    assert out.get("error") and len(calls) == 1


def test_older_watershed_result_without_the_keys_is_unchanged(router, monkeypatch):
    import ai_hydro.analysis.delineation as dl
    from ai_hydro.core import HydroMeta, HydroResult

    monkeypatch.setattr(dl, "delineate_from_point", lambda lat, lon, **kw: HydroResult(
        data={"geometry_geojson": PB, "area_km2": 50.0, "method_used": "stub", "quality_flags": []},
        meta=HydroMeta(tool="x", version="0")))
    out = _tool(session_id="ac-nokeys")
    assert "AREA CHECK" not in out["_note"] and "AMBIGUOUS" not in out["_note"]
    assert wr.area_check_note({}) == "" and wr.area_mismatch_flag({}) is None
