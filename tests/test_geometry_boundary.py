"""One boundary for every tool's ``geometry`` argument, and gauge-first fetches.

Owner feedback on the installed 0.4.3 / tools 2.8.4: the agent called
``data_fetch(variable="streamflow", geometry=[-68.5828, 47.2375], ..., gauge_id="01013500")``.
The pair is GeoJSON order (lon, lat); aihydro-data read it as (lat, lon), put it in the Southern
Ocean, and the gauge request was refused with "geometry is in region 'global'". Without a gauge the
same pair would have been routed to a modelled product for the wrong place with no refusal at all.

Offline: USGS monitoring locations come from the recorded API bodies (``_usgs_replay``); aihydro-data's
fetch is replaced by a capture. Coordinates of USGS 01013500 (Fish River, ME) are the recorded
monitoring-location record, not invented.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from _usgs_replay import EMPTY, Replay, ok
from aihydro_data.exceptions import TerminalSourceError
from aihydro_data.sources import usgs_waterdata as uw

from ai_hydro.mcp.geometry_boundary import (
    GEOMETRY_PARAMS, check_arguments, check_coordinate_arg, check_geometry_arg, param_kind, tools_taking_coordinates)
from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession
from test_run_record_middleware import call, needs_fastmcp2

GAUGE = "01013500"
LON, LAT = -68.5828, 47.2375            # recorded location of USGS 01013500
START, END = "1990-01-01", "2024-12-31"


@pytest.fixture
def world(tmp_path, monkeypatch):
    from ai_hydro.session import chat_binding

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    replay = Replay()
    monkeypatch.setattr(uw, "_http_get", replay)
    HydroSession("gb").save()
    return replay


@pytest.fixture
def seen(monkeypatch):
    """Capture the kwargs of aihydro-data's public fetch(); the call ends in a refusal."""
    import aihydro_data as pipe

    box = {}

    def fake(*a, **k):
        box.update(k)
        raise TerminalSourceError(code="GAUGE_REQUIRED", message="captured")

    monkeypatch.setattr(pipe, "fetch", fake)
    return box


@pytest.fixture
def modelled(monkeypatch):
    """Capture the unpinned (modelled-product) path: aihydro-data's own MCP data_fetch."""
    import aihydro_data.mcp as adm

    box = {"called": 0}

    def fake(**k):
        box["called"] += 1
        box.update(k)
        return {"error": True, "code": "CAPTURED", "message": "captured"}

    monkeypatch.setattr(adm, "_data_fetch", fake)
    return box


def _df(**kw):
    from ai_hydro.mcp.tools_data_fetch import data_fetch

    return data_fetch("streamflow", START, END, session_id="gb", **kw)


# ----------------------------------------------------------------- the check itself (pure)
def test_bare_pair_in_range_is_ambiguous_and_shows_both_readings():
    e = check_geometry_arg([-68.5828, 47.2375], "data_fetch")
    assert e["code"] == "GEOMETRY_AXIS_AMBIGUOUS" and e["error"] is True
    assert e["retryable"] is True and e["terminal"] is False and e["next_tools"] == ["data_fetch"]
    by = {i["order"]: i for i in e["interpretations"]}
    assert by["lat_lon"] == {"order": "lat_lon", "lat": -68.5828, "lon": 47.2375}
    assert by["lon_lat"] == {"order": "lon_lat", "lat": 47.2375, "lon": -68.5828}
    assert '{"lat": 47.2375, "lon": -68.5828}' in e["recovery"] and '{"lat": -68.5828, "lon": 47.2375}' in e["recovery"]


def test_pair_with_a_first_value_beyond_90_can_only_be_lon_first_and_is_still_refused():
    e = check_geometry_arg([-122.3, 47.6], "data_fetch")
    assert e["code"] == "GEOMETRY_AXIS_SWAPPED" and e["retryable"] is True
    assert e["interpretations"] == [{"order": "lon_lat", "lat": 47.6, "lon": -122.3}]
    assert '{"lat": 47.6, "lon": -122.3}' in e["recovery"]
    e2 = check_geometry_arg([47.6, 122.3], "data_fetch")          # second value beyond 90: lat is first
    assert e2["code"] == "GEOMETRY_AXIS_SWAPPED" and e2["interpretations"][0]["lat"] == 47.6
    assert check_geometry_arg([200.0, 100.0])["code"] == "GEOMETRY_RANGE_INVALID"


@pytest.mark.parametrize("form", ["-68.5828,47.2375", "-68.5828, 47.2375", "(47.2375 -68.5828)", "[47.2375, -68.5828]",
                                  (47.2375, -68.5828)])
def test_every_spelling_of_a_bare_pair_is_refused(form):
    assert check_geometry_arg(form, "t")["code"] == "GEOMETRY_AXIS_AMBIGUOUS"


@pytest.mark.parametrize("ok", [
    None, {"lat": LAT, "lon": LON}, [-120.0, 30.0, -110.0, 40.0], {"west": -69.0, "south": 47.0, "east": -68.0, "north": 48.0}, "POINT (-68.5828 47.2375)", "01013500",
    "USGS-01013500", {"type": "Point", "coordinates": [LON, LAT]},
    {"type": "Polygon", "coordinates": [[[-69, 47], [-68, 47], [-68, 48], [-69, 47]]]},
])
def test_unambiguous_forms_pass(ok):
    assert check_geometry_arg(ok, "t") is None


@pytest.mark.parametrize("bad", [
    {"lat": 120.0, "lon": 10.0}, {"lat": "47", "lon": -68.0}, [-130.0, 47.0, -120.0, 91.0], {"west": -68.0, "south": 47.0, "east": -69.0, "north": 48.0},
    [1, 2, 3],
])
def test_invalid_explicit_forms_are_refused(bad):
    assert check_geometry_arg(bad, "t")["code"].startswith("GEOMETRY_")


def test_a_collection_is_checked_member_by_member():
    assert check_geometry_arg([{"lat": 1, "lon": 2}, "01013500"], "t") is None
    assert check_geometry_arg([{"lat": 1, "lon": 2}, [47.0, -68.0]], "t")["code"] == "GEOMETRY_AXIS_AMBIGUOUS"
    assert check_geometry_arg({"a": {"lat": 1, "lon": 2}, "b": [47.0, -68.0]}, "t")["code"] == "GEOMETRY_AXIS_AMBIGUOUS"


# ----------------------------------------------------------------- the owner's exact call
def test_owner_call_gauge_only_needs_no_geometry(world, seen):
    r = _df(gauge_id=GAUGE)
    assert seen["gauge_id"] == GAUGE and seen["mode"] == "manual"
    assert seen["product"] == "NWIS_STREAMFLOW" and seen["fallback"] == []
    # the place is the gauge's recorded location, found by the tool and not invented by the caller
    assert (seen["geometry"]["lat"], seen["geometry"]["lon"]) == pytest.approx((LAT, LON))
    assert r["code"] == "GAUGE_REQUIRED"          # the capture's own refusal: the call reached the pinned fetch


def test_owner_call_with_the_correct_point_is_accepted(world, seen):
    _df(gauge_id=GAUGE, geometry={"lat": LAT, "lon": LON})
    assert seen["gauge_id"] == GAUGE and seen["product"] == "NWIS_STREAMFLOW"


def test_owner_call_with_the_swapped_bare_pair_is_not_fetched(world, seen, modelled):
    r = _df(gauge_id=GAUGE, geometry=[LON, LAT])
    assert r["code"] == "GEOMETRY_AXIS_AMBIGUOUS" and not seen and not modelled["called"]


def test_owner_call_with_a_swapped_dict_is_a_named_mismatch_not_region_global(world, seen, modelled):
    r = _df(gauge_id=GAUGE, geometry={"lat": LON, "lon": LAT})          # a real-looking spot in the Southern Ocean
    assert r["code"] == "GAUGE_LOCATION_MISMATCH" and r["terminal"] is True
    assert r["gauge"]["site"] == "USGS-01013500" and r["gauge"]["lat"] == pytest.approx(LAT)
    assert r["geometry_centroid"]["lat"] == pytest.approx(LON)
    assert "global" not in r["message"] and "47.23" in r["message"] and "-68.58" in r["message"]
    assert not seen and not modelled["called"]


def test_a_gauge_request_is_never_answered_by_a_modelled_product(world, seen, modelled):
    _df(gauge_id=GAUGE)
    _df(gauge_id=GAUGE, geometry={"lat": LAT, "lon": LON})
    assert seen["product"] == "NWIS_STREAMFLOW" and seen["fallback"] == [] and not modelled["called"]
    r = _df(gauge_id=GAUGE, geometry={"lat": LAT, "lon": LON}, product="GLOFAS_STREAMFLOW")
    assert r["code"] == "GAUGE_ID_NOT_APPLICABLE"


def test_unknown_gauge_is_a_terminal_refusal_not_a_guess(world, seen):
    world.override = lambda url, params: ok(EMPTY) if "monitoring-locations" in url else None
    r = _df(gauge_id="00000000")
    assert r["error"] is True and r["terminal"] is True and not seen


def test_lookup_failure_for_a_gauge_only_call_is_transient(world, seen):
    world.override = lambda url, params: (429, {}, b"{}") if "monitoring-locations" in url else None
    r = _df(gauge_id=GAUGE)
    assert r["error"] is True and r["terminal"] is False and not seen


# ----------------------------------------------------------------- without a gauge
def test_swapped_pair_without_a_gauge_never_reaches_a_global_product(world, modelled):
    for bad in ([LON, LAT], f"{LON},{LAT}", (LON, LAT)):
        r = _df(geometry=bad)
        assert r["code"] == "GEOMETRY_AXIS_AMBIGUOUS"
    assert modelled["called"] == 0
    # through the real MCP handler, as the extension calls it
    err, body, _ = call_tool("data_fetch", {"variable": "precipitation", "geometry": [LON, LAT],
                                            "start": START, "end": END})
    assert err and body["code"] == "GEOMETRY_AXIS_AMBIGUOUS" and modelled["called"] == 0


def test_explicit_dict_reaches_the_router_unchanged(world, modelled):
    _df(geometry={"lat": LAT, "lon": LON})
    assert modelled["geometry"] == {"lat": LAT, "lon": LON}


def test_geometry_is_required_when_nothing_can_locate_the_request(world, modelled):
    r = _df()
    assert r["code"] == "GEOMETRY_REQUIRED" and r["retryable"] is True and not modelled["called"]
    from ai_hydro.mcp.tools_data_fetch import data_fetch

    r = data_fetch("precipitation", START, END, session_id="gb", gauge_id=GAUGE)
    assert r["code"] == "GAUGE_ID_NOT_APPLICABLE"
    r = data_fetch("precipitation", START, END, session_id="gb")
    assert r["code"] == "GEOMETRY_REQUIRED"


def test_session_usgs_gauge_locates_a_streamflow_request(world, seen):
    s = HydroSession.load("gb")
    s.site_id, s.site_type = GAUGE, "usgs_gauge"
    s.save()
    _df()
    assert seen["gauge_id"] == GAUGE and seen["product"] == "NWIS_STREAMFLOW"
    assert seen["geometry"]["lat"] == pytest.approx(LAT)


# ----------------------------------------------------------------- background
def _bg(monkeypatch, **kw):
    from ai_hydro.mcp import tools_data_async as tda

    started = {}
    monkeypatch.setattr(tda.jobs, "start_job", lambda **k: started.update(k) or {
        "job_id": "j", "artifact_dir": str(k["artifact_dir"]), "log_path": "l", "started_at": "t"})
    fn = getattr(tda.data_fetch_background, "fn", tda.data_fetch_background)
    return fn("streamflow", START, END, **kw), started


def test_background_gauge_only_starts_a_pinned_job_at_the_gauge(world, monkeypatch, tmp_path):
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "ah"))
    out, started = _bg(monkeypatch, gauge_id=GAUGE)
    assert out["status"] == "pending"
    cfg = started["config"]
    assert cfg["gauge_id"] == GAUGE and cfg["geometry"]["lat"] == pytest.approx(LAT)


def test_background_refuses_the_swapped_pair_and_starts_no_job(world, monkeypatch, tmp_path):
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "ah"))
    out, started = _bg(monkeypatch, gauge_id=GAUGE, geometry=[LON, LAT])
    assert out["code"] == "GEOMETRY_AXIS_AMBIGUOUS" and not started
    out, started = _bg(monkeypatch, gauge_id=GAUGE, geometry={"lat": LON, "lon": LAT})
    assert out["code"] == "GAUGE_LOCATION_MISMATCH" and not started


# ----------------------------------------------------------------- one boundary for every tool
def call_tool(name, args):
    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp.app import mcp

    return call(mcp, name, args)


def _geometry_tools():
    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp.app import mcp

    out = {}
    for t in asyncio.run(mcp.list_tools()):
        props = set(((t.to_mcp_tool().inputSchema or {}).get("properties") or {}))
        hit = props & set(GEOMETRY_PARAMS)
        if hit:
            out[t.name] = sorted(hit)
    return out


def test_every_geometry_taking_tool_is_behind_the_shared_boundary(world):
    tools = _geometry_tools()
    # the known set must be seen (a rename would silently empty this scan)
    assert {"data_fetch", "data_fetch_background", "data_validate_request", "data_batch_fetch"} <= set(tools), tools
    for name, params in tools.items():
        pair = [LON, LAT]
        value = [pair, pair] if params == ["geometries"] else pair
        err, body, _ = call_tool(name, {params[0]: value})
        assert err and isinstance(body, dict) and body.get("code") == "GEOMETRY_AXIS_AMBIGUOUS", (name, body)
        # the envelope survives FastMCP intact: retryable, both readings, next_tools
        assert body["retryable"] is True and len(body["interpretations"]) == 2 and body["next_tools"] == [name]


def test_the_direct_entry_points_call_the_same_helper():
    from ai_hydro.mcp import tools_data_async, tools_data_fetch

    for fn in (tools_data_fetch.data_fetch, getattr(tools_data_async.data_fetch_background, "fn",
                                                    tools_data_async.data_fetch_background)):
        assert "check_geometry_arg" in inspect.getsource(fn), fn.__name__


def test_no_tool_hides_a_pair_reader_of_its_own():
    """The boundary is the only reader of a bare pair: no tool module re-implements 'treat [a, b] as (lat, lon)'."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "ai_hydro" / "mcp"
    offenders = [p.name for p in root.glob("tools_*.py")
                 if re.search(r"lat,\s*lon\s*=\s*geometry", p.read_text())]
    assert offenders == []


# ----------------------------------------------------------------- deprecated tool spelling
def test_deprecated_streamflow_tool_accepts_start_end():
    from ai_hydro.mcp.arg_repair import repair_arguments

    props = {"session_id", "gauge_id", "start_date", "end_date", "interval"}
    fixed, notes = repair_arguments({"gauge_id": GAUGE, "start": START, "end": END}, props, {})
    assert fixed == {"gauge_id": GAUGE, "start_date": START, "end_date": END} and len(notes) == 2
    # data_fetch has start/end of its own: untouched
    fixed, notes = repair_arguments({"start": START, "end": END}, {"start", "end", "variable"}, {})
    assert fixed == {"start": START, "end": END} and not notes
    # and the inverse alias still works for a tool that has start/end
    fixed, _ = repair_arguments({"start_date": START}, {"start", "end"}, {})
    assert fixed == {"start": START}


def test_deprecated_streamflow_tool_with_start_end_is_repaired_on_its_real_schema():
    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp.app import mcp
    from ai_hydro.mcp.arg_repair import repair_arguments

    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "fetch_streamflow_data")
    props = set(tool.to_mcp_tool().inputSchema["properties"])
    fixed, _ = repair_arguments({"gauge_id": GAUGE, "start": "2020-01-01", "end": "2020-01-05"}, props, {})
    assert set(fixed) <= props and fixed["start_date"] == "2020-01-01" and fixed["end_date"] == "2020-01-05"


# ----------------------------------------------------------------- audit R-11 / D43: every coordinate, one validator
def test_lat_lon_parameters_are_range_checked_by_name():
    for name, bad in (("lat", -100), ("lon", 500), ("outlet_lat", 95), ("outlet_lon", -181), ("max_lat", 91),
                      ("latitude", "95"), ("longitude", 181)):
        assert check_coordinate_arg(name, bad, "register_feature")["code"] == "GEOMETRY_RANGE_INVALID", name
    for name, good in (("lat", 47.2), ("lon", -68.6), ("outlet_lat", 0), ("longitude", "-68.6"), ("lat", None), ("other", 999)):
        assert check_coordinate_arg(name, good, "t") is None, name


MAINE = {"type": "Polygon", "coordinates": [[[-68.7, 47.1], [-68.4, 47.1], [-68.4, 47.4], [-68.7, 47.4], [-68.7, 47.1]]]}


def test_outlet_bare_pair_is_ambiguous_and_dict_form_is_converted():
    e = check_coordinate_arg("outlet", [LAT, LON], "data_fetch")
    assert e["code"] == "GEOMETRY_AXIS_AMBIGUOUS" and "outlet" in e["recovery"] and "geometry" not in e["recovery"]
    assert check_coordinate_arg("outlet", {"lat": LAT, "lon": LON}, "t") is None
    assert check_coordinate_arg("outlet", {"lat": 95, "lon": 1}, "t")["code"] == "GEOMETRY_RANGE_INVALID"
    fixed, err = check_arguments({"geometry": MAINE, "outlet": {"lat": LAT, "lon": LON}}, "t")
    assert err is None and fixed["outlet"] == [LAT, LON]


def test_in_range_swapped_outlet_cannot_drive_a_modelled_product(world, modelled):
    """The review's case: a Maine polygon with outlet (lon, lat) read as (lat, lon) snaps to the Southern Ocean."""
    from ai_hydro.mcp.tools_data_fetch import data_fetch

    swapped = {"lat": LON, "lon": LAT}
    r = data_fetch("streamflow", START, END, geometry=MAINE, outlet=swapped, session_id="gb")
    assert r["code"] == "GEOMETRY_OUTLET_MISMATCH" and r["retryable"] is True
    assert r["outlet"]["lat"] == pytest.approx(LON) and not modelled["called"]
    # a bare pair never gets that far
    assert data_fetch("streamflow", START, END, geometry=MAINE, outlet=[LON, LAT])["code"] == "GEOMETRY_AXIS_AMBIGUOUS"
    # through the real MCP handler too, and for a point geometry
    err, body, _ = call_tool("data_fetch", {"variable": "streamflow", "geometry": MAINE, "outlet": swapped,
                                            "start": START, "end": END})
    assert err and body["code"] == "GEOMETRY_OUTLET_MISMATCH" and not modelled["called"]
    err, body, _ = call_tool("data_fetch", {"variable": "streamflow", "geometry": {"lat": LAT, "lon": LON},
                                            "outlet": swapped, "start": START, "end": END})
    assert err and body["code"] == "GEOMETRY_OUTLET_MISMATCH"


def test_correct_outlet_inside_or_within_tolerance_reaches_the_router_as_lat_lon_list(world, modelled):
    from ai_hydro.mcp.tools_data_fetch import data_fetch

    data_fetch("streamflow", START, END, geometry=MAINE, outlet={"lat": LAT, "lon": LON}, session_id="gb")
    assert modelled["called"] == 1 and modelled["outlet"] == [LAT, LON]
    near = {"lat": 47.4 + 0.02, "lon": -68.55}            # ~2 km north of the polygon edge (< 5 km)
    data_fetch("streamflow", START, END, geometry=MAINE, outlet=near, session_id="gb")
    assert modelled["called"] == 2


def test_antimeridian_distance_uses_wrapped_longitudes_and_refuses_a_crossing_geometry():
    from ai_hydro.mcp.geometry_boundary import check_outlet_matches_geometry
    from ai_hydro.mcp.gauge_identity import AntimeridianGeometry, _distance_m

    def box(w, so, e, n):
        return {"type": "Polygon", "coordinates": [[[w, so], [e, so], [e, n], [w, n], [w, so]]]}

    east = box(-179.95, 60.0, -179.5, 60.5)
    # 179.99 and -179.95 are 0.06 degrees apart across the line (about 3.3 km at 60 N), not 359.94 degrees
    assert _distance_m(east, 60.1, 179.99) == pytest.approx(3300, abs=300)
    assert check_outlet_matches_geometry(east, [60.1, 179.99], "t") is None
    # 179.95 is 0.1 degrees (about 5.6 km) from the polygon: refused as a real mismatch, not as a 359 degree one
    e = check_outlet_matches_geometry(east, [60.1, 179.95], "t")
    assert e["code"] == "GEOMETRY_OUTLET_MISMATCH" and e["distance_m"] < 7000
    # a polygon written 179.5 .. -179.5 spans 359 degrees in planar lon/lat: refused with its own code, never mis-measured
    crossing = box(179.5, 60.0, -179.5, 60.5)
    with pytest.raises(AntimeridianGeometry):
        _distance_m(crossing, 60.2, 0.0)
    e = check_outlet_matches_geometry(crossing, [60.2, 0.0], "t")
    assert e["code"] == "GEOMETRY_ANTIMERIDIAN_UNSUPPORTED" and e["retryable"] is True


def test_valid_dict_outlet_through_mcp_reaches_the_router_as_lat_lon_list(world, modelled):
    ok = {"variable": "streamflow", "geometry": MAINE, "start": START, "end": END}
    err, body, _ = call_tool("data_fetch", {**ok, "outlet": {"lat": LAT, "lon": LON}})
    assert modelled["called"] == 1 and list(modelled["outlet"]) == [LAT, LON], body
    # a caller's bare pair is still refused, and never reaches the router
    err, body, _ = call_tool("data_fetch", {**ok, "outlet": [LAT, LON]})
    assert err and body["code"] == "GEOMETRY_AXIS_AMBIGUOUS" and modelled["called"] == 1
    # the swapped dict too
    err, body, _ = call_tool("data_fetch", {**ok, "outlet": {"lat": LON, "lon": LAT}})
    assert err and body["code"] == "GEOMETRY_OUTLET_MISMATCH" and modelled["called"] == 1


def test_outlet_is_sealed_in_the_call_inputs(world, monkeypatch):
    import aihydro_data.mcp as adm
    import ai_hydro.mcp.enforcement as enf
    from ai_hydro.mcp.tools_data_fetch import data_fetch

    monkeypatch.setattr(adm, "_data_fetch", lambda **k: {"variable": "streamflow", "data": {}})
    box = {}
    monkeypatch.setattr(enf, "post_run", lambda tool, sid, result, inputs=None: box.update(inputs=inputs) or result)
    r = data_fetch("streamflow", START, END, geometry=MAINE, outlet={"lat": LAT, "lon": LON}, session_id="gb")
    assert box["inputs"]["outlet"] == [LAT, LON]
    assert box["inputs"]["resolved_place"]["bbox_order"] == "west,south,east,north"
    data_fetch("streamflow", START, END, geometry=MAINE, session_id="gb")
    assert "outlet" not in box["inputs"]


def test_station_id_geometry_with_an_outlet_is_not_compared(world):
    """A station id routes to an observation product, not to outlet snapping: nothing to compare the outlet with."""
    from ai_hydro.mcp.geometry_boundary import check_outlet_matches_geometry

    assert check_outlet_matches_geometry("01013500", [10.0, 10.0], "t") is None
    assert check_outlet_matches_geometry("USGS-01013500", [10.0, 10.0], "t") is None


def test_every_result_echoes_the_resolved_place_and_session_gauge_is_noted(world, seen, modelled):
    from ai_hydro.mcp.tools_data_fetch import _resolved_place, data_fetch

    p = _resolved_place({"lat": LAT, "lon": LON}, None)
    assert p == {"lat": LAT, "lon": LON, "region": "CONUS"}
    q = _resolved_place(MAINE, None)
    assert q["bbox"] == [-68.7, 47.1, -68.4, 47.4] and q["region"] == "CONUS" and "centroid" in q
    # an in-order-but-wrong point is valid and undetectable, so the result must show where it went
    r = data_fetch("precipitation", START, END, geometry="POINT (47.2375 -68.5828)", session_id="gb")
    assert r["resolved_place"] == {"lat": -68.5828, "lon": 47.2375, "region": "global"}      # the wrong spot is visible
    assert q["bbox_order"] == "west,south,east,north"
    s = HydroSession.load("gb")
    s.site_id, s.site_type = GAUGE, "usgs_gauge"
    s.save()
    r = _df()
    assert "site_id" in r["session_note"] and r["gauge_source"].endswith(GAUGE)


def test_geojson_positions_are_range_checked():
    proj = {"type": "Point", "coordinates": [500000, 5200000]}
    assert check_geometry_arg(proj, "t")["code"] == "GEOMETRY_RANGE_INVALID"
    feat = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": proj}]}
    assert check_geometry_arg(feat, "t")["code"] == "GEOMETRY_RANGE_INVALID"
    assert check_geometry_arg(json.dumps(proj), "t")["code"] == "GEOMETRY_RANGE_INVALID"        # *_geojson string params
    ok = {"type": "Polygon", "coordinates": [[[-69, 47], [-68, 47], [-68, 48], [-69, 47]]]}
    assert check_geometry_arg(ok, "t") is None and check_geometry_arg(json.dumps(ok), "t") is None


def test_a_bare_bbox_inside_90_is_axis_ambiguous_and_the_dict_form_is_converted():
    e = check_geometry_arg([47.0, -69.0, 48.0, -68.0], "data_fetch")          # the audit's lat-first bbox
    assert e["code"] == "GEOMETRY_BBOX_AXIS_AMBIGUOUS" and e["retryable"] is True and len(e["interpretations"]) == 2
    assert '"west": 47.0' in e["recovery"] and '"west": -69.0' in e["recovery"]
    fixed, err = check_arguments({"geometry": {"west": -69.0, "south": 47.0, "east": -68.0, "north": 48.0}}, "t")
    assert err is None and fixed["geometry"]["type"] == "Polygon"
    assert fixed["geometry"]["coordinates"][0][0] == [-69.0, 47.0] and fixed["geometry"]["coordinates"][0][2] == [-68.0, 48.0]


def _bad_value(kind):
    return {"geometry": [LON, LAT], "geojson": '{"type": "Point", "coordinates": [500000, 5200000]}',
            "lat": 95.0, "lon": 500.0, "outlet": [95.0, 10.0]}[kind]


def test_every_tool_with_a_coordinate_or_geometry_parameter_goes_through_the_validator(world):
    """Scan of the live tool schemas: a new tool with such a parameter is covered by name, and this fails if it is not."""
    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp.app import mcp

    schemas = {t.name: set((t.to_mcp_tool().inputSchema or {}).get("properties") or {})
               for t in asyncio.run(mcp.list_tools())}
    tools = tools_taking_coordinates(schemas)
    assert {"register_feature", "delineate_watershed_from_point", "map_fly_to", "data_fetch", "show_on_map",
            "gee.preview_layer", "fetch_forcing_data"} <= set(tools), sorted(tools)
    for name, params in tools.items():
        for p in params:
            kind = param_kind(p)
            value = _bad_value(kind)
            if p == "geometries":
                value = [value, value]
            err, body, _ = call_tool(name, {p: value})
            assert err and isinstance(body, dict) and str(body.get("code", "")).startswith("GEOMETRY_"), (name, p, body)
            assert body["retryable"] is True and body["next_tools"] == [name]


# ── Review follow-ups (#21) ────────────────────────────────────────────────────────────────

def test_a_world_wide_box_with_an_outlet_gets_an_honest_message_not_an_antimeridian_one():
    from ai_hydro.mcp.geometry_boundary import check_outlet_matches_geometry as chk
    world = {"type": "Polygon", "coordinates": [[[-180, -60], [180, -60], [180, 80], [-180, 80], [-180, -60]]]}
    e = chk(world, [45.0, -69.0], "t")
    assert e["code"] == "GEOMETRY_ANTIMERIDIAN_UNSUPPORTED"
    assert "world-wide" in e["message"] and "not a basin" in e["message"] and "crosses" not in e["message"]
    assert "gauge or outlet" in e["recovery"]
    crossing = {"type": "Polygon", "coordinates": [[[179.5, 60], [-179.5, 60], [-179.5, 60.5], [179.5, 60.5], [179.5, 60]]]}
    assert "crosses the antimeridian" in chk(crossing, [60.2, 0.0], "t")["message"]


def test_boundary_checked_is_set_only_when_an_outlet_was_converted():
    import asyncio
    from fastmcp import Client, FastMCP
    from ai_hydro.mcp.geometry_boundary import BOUNDARY_CHECKED, install_geometry_boundary

    srv = FastMCP("t")
    seen = {}

    @srv.tool()
    def probe(outlet: list[float] | dict | None = None, geometry: dict | None = None) -> dict:
        seen["flag"] = BOUNDARY_CHECKED.get()
        seen["outlet"] = outlet
        return {"ok": True}

    install_geometry_boundary(srv)

    async def go(args):
        async with Client(srv) as c:
            return await c.call_tool("probe", args, raise_on_error=False)

    asyncio.run(go({"outlet": {"lat": 45.0, "lon": -69.0}}))
    assert seen["flag"] is True and seen["outlet"] == [45.0, -69.0]
    seen.clear()
    asyncio.run(go({}))
    assert seen["flag"] is False


def test_only_a_box_shaped_world_geometry_gets_the_world_wide_wording():
    from ai_hydro.mcp.geometry_boundary import check_outlet_matches_geometry as chk
    # fills its bounding box -> world-wide box
    world = {"type": "Polygon", "coordinates": [[[-180, -60], [180, -60], [180, 80], [-180, 80], [-180, -60]]]}
    assert "world-wide" in chk(world, [45.0, -69.0], "t")["message"]
    # reaches -180 and 180 but is a thin sliver (a basin outline written across the line): crossing wording + split recovery
    sliver = {"type": "Polygon", "coordinates": [[[-180, 60], [180, 60.1], [180, 60], [-180, 60]]]}
    e = chk(sliver, [60.0, 0.0], "t")
    assert "world-wide" not in e["message"] and "crosses the antimeridian" in e["message"]
    assert "Split the geometry" in e["recovery"]
