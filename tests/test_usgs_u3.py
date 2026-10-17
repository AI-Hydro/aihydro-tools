"""Wave U slice U3: USGS Water Data API consumers in aihydro-tools.

Offline. The adapter's single transport seam (``usgs_waterdata._http_get``) replays the
recorded API bodies in ``tests/fixtures/usgs_waterdata`` (copied verbatim from the
aihydro-data recordings; sha256 checked against the index when copied). A test that
needs a response the archive does not hold says so and builds it from a recorded body;
values labelled SYNTHETIC are test inputs, not data.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pandas as pd
import pytest

from _usgs_replay import EMPTY, INDEX, Replay, body, doc, ok

from aihydro_data.exceptions import SourceUnavailable, TerminalSourceError
from aihydro_data.sources import usgs_waterdata as uw

from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

PROVISIONAL = ("01013500", "2026-06-19", "2026-10-17")     # 77 Approved + 43 Provisional (recorded)
FISH_BBOX = (-68.70, 47.10, -68.40, 47.40)


@pytest.fixture
def world(tmp_path, monkeypatch):
    from ai_hydro.session import chat_binding

    monkeypatch.setenv("HOME", str(tmp_path / "home"))          # aihydro-data cache is isolated
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    replay = Replay()
    monkeypatch.setattr(uw, "_http_get", replay)
    HydroSession("u3").save()
    return replay


def _fetch(start_end=PROVISIONAL, session="u3", **kw):
    from ai_hydro.mcp.tools_data_fetch import data_fetch

    gauge, start, end = start_end
    return data_fetch("streamflow", gauge, start, end, session_id=session, **kw)


# --------------------------------------------------------------- slot writer (D6) + validator
def test_data_fetch_writes_streamflow_slot_with_quality_meta(world):
    r = _fetch()
    assert not r.get("error"), r
    slot = HydroSession.load("u3").streamflow
    assert slot is not None
    data, meta = slot["data"], slot["meta"]
    assert data["gauge_id"] == "01013500" and data["units"] == "m3/s"
    assert meta["params"] == {"gauge_id": "01013500", "start_date": "2026-06-19",
                              "end_date": "2026-10-17", "interval": "daily"}
    # per-value qualification codes, aligned with the served rows: 77 A, 43 P (recorded counts)
    codes = meta["qualification_codes"]
    assert len(codes) == data["n_days"]
    assert set(codes) == {"A", "P"} and codes.count("P") == 43 - 0 and codes.count("A") <= 77
    qs = meta["quality_summary"]
    assert qs["approval_counts"] == {"Approved": 77, "Provisional": 43}
    assert qs["provisional_fraction"] == pytest.approx(43 / 120)
    assert {"n_negative", "n_missing"} <= set(qs)
    assert meta["qualifiers_other"] == {}
    assert meta["site_resolution"]["method"] == "explicit_id" and meta["site_resolution"]["site"] == "USGS-01013500"
    assert meta["upstream"]["backend"] == "usgs_waterdata" and meta["upstream"]["response_sha256"]
    assert "X-Api-Key" not in json.dumps(meta)
    # the retained-file digest is in the slot and in the call's sealed lineage
    rs = meta["retained_series"]
    from aihydro_core.records import digest_bytes
    assert digest_bytes(Path(rs["path"]).read_bytes()) == rs["digest"]
    assert Path(data["_data_file"]).samefile(rs["path"])
    saved = json.loads(Path(rs["path"]).read_text())
    assert len(saved["q_cms"]) == len(saved["qualification_codes"]) == data["n_days"]
    # the response does not carry the per-value list
    assert r["streamflow_slot"]["qualification_codes_n"] == data["n_days"]
    assert "qualification_codes" not in r["streamflow_slot"]


def test_validator_fires_on_data_fetch_and_warns_on_provisional(world):
    r = _fetch()
    flags = {f["validator"]: f for f in r["quality_flags"]}
    q = flags["usgs_qualification_codes"]
    assert q["status"] == "warning" and q["metadata"]["quality_flag"] == "provisional_data"
    assert q["metadata"]["approval_counts"] == {"Approved": 77, "Provisional": 43}
    from ai_hydro.mcp.tools_validators import check_usgs_qualification_codes
    assert check_usgs_qualification_codes("u3")["status"] == "warning"


def test_regression_before_the_fix_data_fetch_slot_was_insufficient_data(world):
    """Before U3 data_fetch wrote no slot, so the validator always answered insufficient_data."""
    from ai_hydro.mcp.tools_validators import check_usgs_qualification_codes

    assert check_usgs_qualification_codes("u3")["status"] == "insufficient_data"      # nothing fetched
    _fetch()
    assert check_usgs_qualification_codes("u3")["status"] != "insufficient_data"


def test_non_ape_qualifier_is_a_warning_with_raw_values_never_unreadable(world):
    # 03245500 2010-2020 (recorded): ESTIMATED x212 and FORCEINTERPOLATION x35, all Approved
    r = _fetch(("03245500", "2010-01-01", "2020-12-31"))
    assert not r.get("error"), r
    meta = HydroSession.load("u3").streamflow["meta"]
    assert meta["qualifiers_other"] == {"FORCEINTERPOLATION": 35}
    q = {f["validator"]: f for f in r["quality_flags"]}["usgs_qualification_codes"]
    assert q["status"] == "warning"
    assert q["metadata"]["quality_flag"] == "other_qualifiers"
    assert q["metadata"]["qualifiers_other"] == {"FORCEINTERPOLATION": 35}
    assert "FORCEINTERPOLATION" in q["message"] and "unreadable" not in q["message"].lower()


def test_estimated_only_series_warns_estimated(world):
    r = _fetch(("01013500", "1989-10-01", "2009-09-30"))          # all Approved, ESTIMATED x2100
    q = {f["validator"]: f for f in r["quality_flags"]}["usgs_qualification_codes"]
    assert q["status"] == "warning" and q["metadata"]["quality_flag"] == "estimated_data"


def _slot_session(tmp_path, monkeypatch, meta, sid="v1"):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)
    s = HydroSession(sid)
    s.set("streamflow", {"data": {"n_days": 8000}, "meta": meta})
    s.save()
    return sid


def test_all_approved_counts_pass(tmp_path, monkeypatch):
    """SYNTHETIC summary (the archive holds no series that is approved with no qualifier)."""
    from ai_hydro.mcp.tools_validators import check_usgs_qualification_codes

    sid = _slot_session(tmp_path, monkeypatch, {
        "qualification_codes": ["A"] * 10,
        "quality_summary": {"approval_counts": {"Approved": 10}, "qualifier_counts": {},
                            "qualifiers_other": {}, "provisional_fraction": 0.0}})
    assert check_usgs_qualification_codes(sid)["status"] == "pass"


def test_counts_alone_are_enough_and_other_tokens_in_codes_warn(tmp_path, monkeypatch):
    from ai_hydro.mcp.tools_validators import check_usgs_qualification_codes

    sid = _slot_session(tmp_path, monkeypatch, {"quality_summary": {
        "approval_counts": {"Approved": 5}, "qualifier_counts": {"ICE": 2}, "qualifiers_other": {"ICE": 2}}})
    r = check_usgs_qualification_codes(sid)
    assert r["status"] == "warning" and r["metadata"]["qualifiers_other"] == {"ICE": 2}
    sid = _slot_session(tmp_path, monkeypatch, {"qualification_codes": ["A", "A ICE", "P"]}, sid="v2")
    r = check_usgs_qualification_codes(sid)
    assert r["status"] == "warning"                                  # provisional wins, ICE still reported
    assert "ICE" in r["message"]


# --------------------------------------------------------------- terminal refusals
def _empty_locations(url, params):
    if url.endswith("/collections/monitoring-locations/items"):
        return ok(EMPTY)
    return None


def test_shim_propagates_terminal_refusal(monkeypatch):
    import aihydro_data
    from ai_hydro.mcp.helpers import _legacy_data_shim

    def refuse(*a, **k):
        raise TerminalSourceError(code="GAUGE_AMBIGUOUS", message="two gauges", details={"candidates": []})

    monkeypatch.setattr(aihydro_data, "fetch", refuse)
    with pytest.raises(TerminalSourceError) as ei:
        _legacy_data_shim("streamflow", "01013500", "2000-01-01", "2000-12-31")
    assert ei.value.code == "GAUGE_AMBIGUOUS"


def test_shim_still_returns_err_for_transient_failure(monkeypatch):
    import aihydro_data
    from ai_hydro.mcp.helpers import _legacy_data_shim

    def down(*a, **k):
        raise SourceUnavailable(code="USGS_RATE_LIMITED", message="429")

    monkeypatch.setattr(aihydro_data, "fetch", down)
    res, err = _legacy_data_shim("streamflow", "01013500", "2000-01-01", "2000-12-31")
    assert res is None and err["_aihydro_data_unavailable"] is True and err["code"] == "USGS_RATE_LIMITED"


def test_fetch_streamflow_tool_refuses_and_does_not_fall_back(world, monkeypatch):
    """A terminal refusal comes back as the refusal; no other fetcher is consulted."""
    import fastmcp, asyncio
    from ai_hydro.mcp import app

    world.override = _empty_locations
    s = HydroSession("u3"); s.site_id, s.site_type = "00000000", "usgs_gauge"; s.save()

    async def go():
        async with fastmcp.Client(app.mcp) as c:
            res = await c.call_tool("fetch_streamflow_data", {
                "session_id": "u3", "gauge_id": "00000000",
                "start_date": "2020-01-01", "end_date": "2020-12-31"}, raise_on_error=False)
            return res.is_error, json.loads(res.content[0].text)

    is_err, out = asyncio.run(go())
    assert is_err and out["code"] == "GAUGE_NOT_FOUND" and out["terminal"] is True
    assert all("/daily/" not in u for u, _ in world.calls)           # never went on to ask for values
    assert HydroSession.load("u3").streamflow is None


def test_adapter_no_data_is_a_terminal_toolerror(world):
    from ai_hydro.core import ToolError
    from ai_hydro.data.streamflow import fetch_streamflow_data

    world.override = lambda url, params: (ok(EMPTY) if url.endswith("/daily/items") else None)
    with pytest.raises(ToolError) as ei:
        fetch_streamflow_data("01013500", "2020-01-01", "2020-12-31")
    assert ei.value.code == "USGS_NO_DATA" and ei.value.details["terminal"] is True


def test_transient_failure_is_not_marked_terminal(world):
    from ai_hydro.core import ToolError
    from ai_hydro.data.streamflow import fetch_streamflow_data

    def limited(url, params):
        if url.endswith("/daily/items"):
            return 429, {}, b"{}"
        return None

    world.override = limited
    with pytest.raises(ToolError) as ei:
        fetch_streamflow_data("01013500", "2020-01-01", "2020-12-31")
    assert ei.value.code == "USGS_RATE_LIMITED" and ei.value.details["terminal"] is False


def _capsule_slot(params):
    return {"data": {}, "meta": {"params": params}}


def test_capsule_requery_records_terminal_refusal(monkeypatch):
    import aihydro_data
    from ai_hydro.capsule import data_artifacts as da

    def refuse(*a, **k):
        assert k.get("product") == "NWIS_STREAMFLOW" and k.get("mode") == "manual"   # observed only
        raise TerminalSourceError(code="USGS_NO_DATA", message="no rows")

    monkeypatch.setattr(aihydro_data, "fetch", refuse)
    series, info = da._from_aihydro_data({"gauge_id": "01013500", "start_date": "2020-01-01", "end_date": "2020-12-31"})
    assert series is None and info["error"] == {"code": "USGS_NO_DATA", "message": "[USGS_NO_DATA] no rows", "terminal": True}


def test_capsule_requery_records_transient_failure_distinctly(monkeypatch):
    import aihydro_data
    from ai_hydro.capsule import data_artifacts as da

    def down(*a, **k):
        raise SourceUnavailable(code="USGS_TIMEOUT", message="slow")

    monkeypatch.setattr(aihydro_data, "fetch", down)
    series, info = da._from_aihydro_data({"gauge_id": "01013500", "start_date": "2020-01-01", "end_date": "2020-12-31"})
    assert series is None and info["error"]["terminal"] is False and info["error"]["code"] == "USGS_TIMEOUT"


@pytest.mark.parametrize("terminal,word", [(True, "refused (terminal)"), (False, "failed (transient)")])
def test_capsule_entry_says_which_kind_of_failure(monkeypatch, tmp_path, terminal, word):
    import aihydro_data
    from ai_hydro.capsule import data_artifacts as da

    exc = (TerminalSourceError if terminal else SourceUnavailable)(code="X_CODE", message="m")
    monkeypatch.setattr(aihydro_data, "fetch", lambda *a, **k: (_ for _ in ()).throw(exc))

    class S:
        def get(self, name):
            return _capsule_slot({"gauge_id": "01013500", "start_date": "2020-01-01", "end_date": "2020-12-31"}) \
                if name == "streamflow" else None

    [entry] = da.collect_data_artifacts(S(), tmp_path)
    assert entry["status"] == "unavailable" and word in entry["reason"] and "X_CODE" in entry["reason"]
    assert entry["requery_error"]["terminal"] is terminal


# --------------------------------------------------------------- sealed provenance
def _sealed_row(monkeypatch, tmp_path, gauge, start, end, area_km2):
    import numpy as np
    import aihydro_watershed.signatures.signatures as impl
    from ai_hydro.mcp.enforcement import post_run
    from ai_hydro.mcp.streamflow_slot import mirror_streamflow_source
    from ai_hydro.session import chat_binding

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    HydroSession("seal").save()
    monkeypatch.setattr(impl, "_fetch_precipitation_data_bygeom",
                        lambda g, s, e: pd.Series(np.full(len(pd.date_range(s, e)), 3.0), index=pd.date_range(s, e)))
    square = {"type": "Polygon", "coordinates": [[[-77.5, 39.2], [-77.4, 39.2], [-77.4, 39.3],
                                                   [-77.5, 39.3], [-77.5, 39.2]]]}
    res = impl.extract_hydrological_signatures(gauge_id=gauge, watershed_geojson=square, area_km2=area_km2,
                                               start_date=start, end_date=end)
    d = {"data": dict(res.data), "meta": {"version": "x"}}
    mirror_streamflow_source(d["data"], d["data"].get("_streamflow_source"))
    out = post_run("extract_hydrological_signatures", "seal", d)
    return (HydroSession.load("seal").get("_run_log") or {})[out["_run_id"]]


def test_signatures_streamflow_provenance_is_sealed(world, monkeypatch, tmp_path):
    row = _sealed_row(monkeypatch, tmp_path, "01013500", "1989-10-01", "2009-09-30", 2253.0)
    ev = row["evidence"]["data"]
    assert "_streamflow_source" not in ev                              # underscore block is dropped ...
    assert ev["streamflow_observation"] == "observed"                  # ... the mirrors are sealed
    assert ev["streamflow_product"] == "NWIS_STREAMFLOW" and ev["streamflow_site"] == "USGS-01013500"
    assert ev["streamflow_quality_summary"]["approval_counts"] == {"Approved": 7305}
    assert ev["streamflow_provisional_fraction"] == 0.0
    assert ev["streamflow_upstream"]["backend"] == "usgs_waterdata"
    assert row["key_outputs"]["streamflow_observation"] == "observed"


def test_signatures_refusal_reason_is_visible_in_sealed_row_and_key_outputs(world, monkeypatch, tmp_path):
    """observation 'none' used to show NaN with no reason (N2): the reason is now sealed."""
    # no such site: the recorded empty daily body, and (not archived for this id) an empty
    # time-series-metadata answer in the API's own empty shape
    world.override = lambda url, params: ok(EMPTY) if url.endswith("/time-series-metadata/items") else None
    row = _sealed_row(monkeypatch, tmp_path, "00000000", "2020-01-01", "2020-12-31", 250.0)
    ev = row["evidence"]["data"]
    assert ev["streamflow_observation"] == "none"
    assert ev["streamflow_error_code"] in uw.TERMINAL_CODES and ev["streamflow_error_terminal"] is True
    ko = row["key_outputs"]
    assert ko["streamflow_error_code"] == ev["streamflow_error_code"]
    assert ko["streamflow_error_terminal"] is True and ko["streamflow_error_message"]


# --------------------------------------------------------------- gauge identity on the MCP surface
class _Seen(Exception):
    pass


@pytest.fixture
def seen(monkeypatch):
    """Capture the kwargs aihydro-data's pipeline fetch() receives; end the call with a refusal."""
    import aihydro_data as pipe

    box = {}

    def fake(*a, **k):
        box.update(k)
        raise TerminalSourceError(code="GAUGE_REQUIRED", message="captured")

    monkeypatch.setattr(pipe, "fetch", fake)
    return box


POLY = {"type": "Polygon", "coordinates": [[[-68.7, 47.1], [-68.4, 47.1], [-68.4, 47.4],
                                           [-68.7, 47.4], [-68.7, 47.1]]]}


def _ref(verified=True, site="01013500"):
    return {"aliases": [{"scheme": "usgs", "id": site, "relation": "same_as", "verified": verified}]}


def _session_with_basin(basin_ref, geom=POLY):
    s = HydroSession("u3")
    s.set("watershed", {"data": {"basin_ref": basin_ref, "geometry_geojson": geom}, "meta": {"tool": "t"}})
    s.save()


def test_data_fetch_exposes_gauge_id_and_forwards_it(world, seen):
    from ai_hydro.mcp.tools_data_fetch import data_fetch

    r = data_fetch("streamflow", POLY, "2000-01-01", "2000-12-31", session_id="u3", gauge_id="01013500")
    assert seen["gauge_id"] == "01013500" and seen["basin_ref"] is None
    assert r["error"] is True and r["code"] == "GAUGE_REQUIRED"        # the refusal is returned as is


def test_gauge_id_only_applies_to_streamflow(world, seen):
    from ai_hydro.mcp.tools_data_fetch import data_fetch

    r = data_fetch("precipitation", POLY, "2000-01-01", "2000-12-31", session_id="u3", gauge_id="01013500")
    assert r["code"] == "GAUGE_ID_NOT_APPLICABLE" and not seen


def test_verified_session_basin_ref_is_passed(world, seen):
    from ai_hydro.mcp.tools_data_fetch import data_fetch

    ref = _ref(True)
    _session_with_basin(ref)
    data_fetch("streamflow", POLY, "2000-01-01", "2000-12-31", session_id="u3")
    assert seen["basin_ref"] == ref and seen["gauge_id"] is None


@pytest.mark.parametrize("case", ["unverified", "other_geometry", "no_alias", "gauge_string"])
def test_basin_ref_is_not_passed_unless_verified_and_for_this_basin(world, monkeypatch, case):
    import aihydro_data as pipe
    import aihydro_data.mcp as adm
    from ai_hydro.mcp.tools_data_fetch import data_fetch

    seen_pipe, seen_plain = {}, {}
    monkeypatch.setattr(pipe, "fetch", lambda *a, **k: seen_pipe.update(k) or (_ for _ in ()).throw(
        TerminalSourceError(code="GAUGE_REQUIRED", message="c")))
    monkeypatch.setattr(adm, "_data_fetch", lambda **k: seen_plain.update(k) or {"error": True, "code": "X"})
    geom = POLY
    if case == "unverified":
        _session_with_basin(_ref(False))
    elif case == "other_geometry":
        _session_with_basin(_ref(True))
        geom = {"type": "Polygon", "coordinates": [[[-70, 40], [-69, 40], [-69, 41], [-70, 41], [-70, 40]]]}
    elif case == "no_alias":
        _session_with_basin({"aliases": []})
    else:
        _session_with_basin(_ref(True))
        geom = "01013500"
    data_fetch("streamflow", geom, "2000-01-01", "2000-12-31", session_id="u3")
    assert not seen_pipe                                              # identity path not taken
    assert seen_plain and "gauge_id" not in seen_plain and "basin_ref" not in seen_plain


def test_a2_thresholds_are_not_on_the_agent_surface():
    from ai_hydro.mcp.tools_data_async import data_fetch_background
    from ai_hydro.mcp.tools_data_fetch import data_fetch

    banned = re.compile(r"max_distance|log_area|area_ratio|offset_max|threshold|basin_ref", re.I)
    for fn in (data_fetch, getattr(data_fetch_background, "fn", data_fetch_background)):
        assert not [p for p in inspect.signature(fn).parameters if banned.search(p)], fn
    assert "gauge_id" in inspect.signature(data_fetch).parameters
    assert "gauge_id" in inspect.signature(getattr(data_fetch_background, "fn", data_fetch_background)).parameters


def test_background_forwards_gauge_id_to_the_runner(monkeypatch, tmp_path):
    from ai_hydro.mcp import tools_data_async as tda
    from ai_hydro.mcp.runners.data_fetch_runner import DataFetchRunner
    import aihydro_data

    monkeypatch.setenv("HOME", str(tmp_path))
    started = {}
    monkeypatch.setattr(tda.jobs, "start_job", lambda **k: started.update(k) or {
        "job_id": "j", "artifact_dir": str(tmp_path), "log_path": "l", "started_at": "t"})
    fn = getattr(tda.data_fetch_background, "fn", tda.data_fetch_background)
    out = fn("streamflow", POLY, "2000-01-01", "2000-12-31", gauge_id="01013500")
    assert started["config"]["gauge_id"] == "01013500" and out["status"] == "pending"
    bad = fn("precipitation", POLY, "2000-01-01", "2000-12-31", gauge_id="01013500")
    assert bad["code"] == "GAUGE_ID_NOT_APPLICABLE"

    got = {}
    monkeypatch.setattr(aihydro_data, "fetch", lambda **k: got.update(k) or (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError):
        DataFetchRunner.__new__(DataFetchRunner).run_job(started["config"], tmp_path)
    assert got["gauge_id"] == "01013500"


# --------------------------------------------------------------- gauge search
def _locs_override(url, params):
    """The archive holds these two bodies for other `limit` values; serve them for the tool's."""
    if url.endswith("/collections/monitoring-locations/items") and params:
        if "bbox" in params:
            return 200, {}, body("monitoring_locations_bbox_fish_river")
        if "monitoring_location_name" in params:
            return 200, {}, body("monitoring_locations_name_query")
    return None


def test_search_by_site_id(world):
    from ai_hydro.data.hydro_search import search_hydrology

    hits = search_hydrology("01013500")
    assert [h["meta"]["gauge_id"] for h in hits] == ["01013500"]
    assert hits[0]["source"] == "gauge" and "Fish River" in hits[0]["label"]
    assert hits[0]["meta"]["drainage_area_sqmi"] == "870.0"
    assert all(u.startswith("https://api.waterdata.usgs.gov/ogcapi/v1/") for u, _ in world.calls)


def test_search_by_bbox_filters_names_client_side(world, monkeypatch):
    import ai_hydro.data.hydro_search as hs

    world.override = _locs_override
    monkeypatch.setattr(hs, "fetch_nid_dams_in_bbox", lambda *a: pd.DataFrame())
    all_locs = doc("monitoring_locations_bbox_fish_river")["features"]
    hits = hs.search_hydrology("fish", min_lon=FISH_BBOX[0], min_lat=FISH_BBOX[1],
                               max_lon=FISH_BBOX[2], max_lat=FISH_BBOX[3])
    expect = [f for f in all_locs if "fish" in (f["properties"]["monitoring_location_name"] or "").lower()
              or "fish" in f["properties"]["id"].lower()]
    assert 0 < len(hits) <= min(20, len(expect))
    assert all("fish" in h["label"].lower() for h in hits)


def test_search_by_exact_name_only(world, monkeypatch):
    import ai_hydro.data.hydro_search as hs

    world.override = _locs_override
    exact = "Fish River near Fort Kent, Maine"
    assert [h["meta"]["gauge_id"] for h in hs.search_hydrology(exact)] == ["01013500"]
    # a partial name is never sent to the API as a substring match: exact match only, with no bbox
    seen = []
    world.override = lambda url, params: seen.append(dict(params or {})) or ok(EMPTY)
    assert hs.search_hydrology("Fish River") == []
    assert any(p.get("monitoring_location_name") == "Fish River" for p in seen)      # sent as an exact name
    assert not any("name_contains" in p or "%" in str(p.get("monitoring_location_name", "")) for p in seen)


def test_gauges_in_view_layer_and_size_guard(world):
    import ai_hydro.data.hydro_search as hs

    world.override = _locs_override
    layers = hs.gauges_in_view_layer(min_lon=FISH_BBOX[0], min_lat=FISH_BBOX[1], max_lon=FISH_BBOX[2],
                                     max_lat=FISH_BBOX[3], lat=47.2, lon=-68.5)
    assert layers and layers[0]["id"] == "nwis-gauges-view"
    feats = layers[0]["geojson"]["features"]
    n = len(doc("monitoring_locations_bbox_fish_river")["features"])
    assert len(feats) == n
    lon, lat = feats[0]["geometry"]["coordinates"]
    assert -69 < lon < -68 and 47 < lat < 48                                         # [lon, lat]
    calls = len(world.calls)
    assert hs.gauges_in_view_layer(min_lon=-100, min_lat=30, max_lon=-80, max_lat=45, lat=40, lon=-90) == []
    assert len(world.calls) == calls                                                  # too large: not requested


# --------------------------------------------------------------- grep gate
def test_no_legacy_usgs_calls_in_ai_hydro():
    pat = re.compile(r"waterservices\.usgs\.gov|dataretrieval(\.| import )nwis|hydrofunctions|"
                     r"pygeohydro import .*NWIS|NWIS\(\)")
    root = Path(__import__("ai_hydro").__file__).parent
    hits = [f"{p}:{i}" for p in root.rglob("*.py") for i, line in enumerate(p.read_text().splitlines(), 1)
            if pat.search(line)]
    assert hits == []
    assert not (root / "knowledge" / "library_refs" / "hydrofunctions.json").exists()
    nwis_in_pgh = json.loads((root / "knowledge" / "library_refs" / "pygeohydro.json").read_text())
    assert "NWIS" not in json.dumps(nwis_in_pgh).replace("do not use pygeohydro's NWIS client", "")
    for card in ("usgs_waterdata", "dataretrieval_waterdata"):
        c = json.loads((root / "knowledge" / "library_refs" / f"{card}.json").read_text())
        assert c["library"] == card
