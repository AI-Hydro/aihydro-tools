"""
The REAL fetch_streamflow_data -> extract_hydrological_signatures path (D1).

Slice 1a's lineage test used a stand-in fetch tool. In the first real
end-to-end run the signatures record had ``parents=[]`` because the session had
no workspace, so the lean session JSON kept only ``q_cms_n`` and the signatures
tool never saw the series (it silently refetched its own NWIS data).

Here both real tools run through the server (FastMCP Client, recording
middleware on). Only the network is mocked, at the lowest layers:
``dataretrieval.nwis.get_dv`` (NWIS HTTP client) and the precipitation fetch.
"""
from __future__ import annotations

import asyncio
import json

import fastmcp
import numpy as np
import pandas as pd
import pytest

from aihydro_core.records import digest

from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

pytestmark = pytest.mark.skipif(
    int(fastmcp.__version__.split(".")[0]) >= 3,
    reason="real ai_hydro tools need the pinned FastMCP 2.x API",
)

SID = "real-lineage-session"
GAUGE = "01013500"
START, END = "2000-01-01", "2001-12-31"
SQUARE = json.dumps({"type": "Polygon", "coordinates": [[[-77.5, 39.2], [-77.4, 39.2], [-77.4, 39.3],
                                                         [-77.5, 39.3], [-77.5, 39.2]]]})


def _cfs_frame(start, end):
    idx = pd.date_range(start, end, freq="D")
    rng = np.random.default_rng(7)
    cfs = 200 + 150 * np.abs(np.sin(np.arange(len(idx)) / 30.0)) + rng.uniform(0, 40, len(idx))
    df = pd.DataFrame({"00060_Mean": cfs, "00060_Mean_cd": "A"}, index=idx)
    df.index.name = "datetime"
    return df


@pytest.fixture
def world(tmp_path, monkeypatch):
    from ai_hydro.session import chat_binding

    # aihydro-data caches fetches under ~/.aihydro/cache: isolate it per test so
    # the mocked NWIS call is really made (and never reads a real cache).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    session = HydroSession(SID)             # no workspace_dir, as in the real run
    session.site_id, session.site_type = GAUGE, "usgs_gauge"
    session.set("watershed", {"data": {"area_km2": 250.0}, "meta": {"tool": "delineate_watershed"}})
    session.save()

    calls = []

    def fake_get_dv(sites=None, parameterCd=None, statCd=None, start=None, end=None, **kw):
        calls.append((sites, start, end))
        return _cfs_frame(start, end), object()

    import dataretrieval.nwis as nwis
    monkeypatch.setattr(nwis, "get_dv", fake_get_dv)

    def fake_get_info(sites=None, **kw):
        # Site-metadata lookup made by the internal NWIS fetch (the other NWIS
        # HTTP entry point); mocked so the refetch path never leaves the box.
        df = pd.DataFrame({"site_no": [str(sites)], "station_nm": ["Synthetic test gauge"],
                           "dec_lat_va": [45.0], "dec_long_va": [-68.5]})
        return df, object()

    monkeypatch.setattr(nwis, "get_info", fake_get_info)

    def fake_precip(geom, start_date, end_date):
        idx = pd.date_range(start_date, end_date, freq="D")
        return pd.Series(np.full(len(idx), 3.0), index=idx)

    monkeypatch.setattr("aihydro_watershed.signatures.signatures._fetch_precipitation_data_bygeom", fake_precip)
    from ai_hydro.mcp import app
    return app.mcp, calls


def _call(server, name, arguments):
    async def go():
        async with fastmcp.Client(server) as client:
            res = await client.call_tool(name, arguments, raise_on_error=False)
            return res.is_error, json.loads(res.content[0].text)
    return asyncio.run(go())


def _rows():
    return HydroSession.load(SID).get("_run_log")


def test_real_fetch_then_signatures_records_parent_edge_and_series_digest(world):
    server, nwis_calls = world
    is_err, fetched = _call(server, "fetch_streamflow_data",
                            {"session_id": SID, "gauge_id": GAUGE, "start_date": START, "end_date": END})
    assert not is_err and not fetched.get("error"), fetched
    assert len(nwis_calls) == 1

    slot_run = HydroSession.load(SID).streamflow["meta"]["run_id"]
    parent = _rows()[slot_run]["record"]
    assert parent["tool"] == "fetch_streamflow_data" and parent["output_digest"]

    # The series fetch stored (what signatures must read back, not refetch).
    data_file = HydroSession.load(SID).streamflow["data"]["_data_file"]
    q_stored = json.load(open(data_file))["q_cms"]
    assert len(q_stored) == 731

    is_err, sigs = _call(server, "extract_hydrological_signatures",
                         {"session_id": SID, "start_date": START, "end_date": END,
                          "geometry_geojson": SQUARE})
    assert not is_err and not sigs.get("error"), sigs
    assert len(nwis_calls) == 1, "signatures must consume the session slot, not refetch NWIS"

    child = _rows()[sigs["_run_id"]]["record"]
    assert child["tool"] == "extract_hydrological_signatures"
    assert child["parents"] == [slot_run]
    refs = {r["ref"]: r for r in child["input_refs"]}
    assert refs[slot_run] == {"ref": slot_run, "digest": parent["output_digest"], "role": "served_data"}
    assert refs[f"{slot_run}#q_cms"]["digest"] == digest(q_stored)
    assert all(r["role"] == "served_data" for r in child["input_refs"])
    assert rr.verify_run_log_entry(_rows()[sigs["_run_id"]])["record_ok"]


def test_period_mismatch_refetches_and_declares_it_without_a_false_edge(world):
    server, nwis_calls = world
    _call(server, "fetch_streamflow_data",
          {"session_id": SID, "gauge_id": GAUGE, "start_date": START, "end_date": END})
    is_err, sigs = _call(server, "extract_hydrological_signatures",
                         {"session_id": SID, "start_date": "2000-06-01", "end_date": "2001-12-31",
                          "geometry_geojson": SQUARE})
    assert not is_err and not sigs.get("error"), sigs
    child = _rows()[sigs["_run_id"]]["record"]
    assert child["parents"] == [] and child["input_refs"] == []
    acq = {a["role"]: a for a in child["extra"]["internal_acquisitions"]}
    assert acq["streamflow"]["mode"] == "internal_nwis_fetch"
    assert "start_date" in acq["streamflow"]["session_slot_not_used_because"]
    assert "start_date" in child["extra"]["parent_unresolved"]


def _fetch(server):
    is_err, fetched = _call(server, "fetch_streamflow_data",
                            {"session_id": SID, "gauge_id": GAUGE, "start_date": START, "end_date": END})
    assert not is_err and not fetched.get("error"), fetched
    return fetched


def _sigs(server, **over):
    args = {"session_id": SID, "start_date": START, "end_date": END, "geometry_geojson": SQUARE, **over}
    is_err, body = _call(server, "extract_hydrological_signatures", args)
    assert not is_err and not body.get("error"), body
    return _rows()[body["_run_id"]]["record"]


def test_fetch_record_binds_the_retained_file_digest(world):
    from aihydro_core.records import digest_bytes
    server, _ = world
    _fetch(server)
    slot = HydroSession.load(SID).streamflow
    path = slot["data"]["_data_file"]
    bound = slot["meta"]["retained_series"]
    assert bound == {"path": path, "digest": digest_bytes(open(path, "rb").read())}
    rec = _rows()[slot["meta"]["run_id"]]["record"]
    # The sealed record names the file by location-independent ref, not absolute path.
    from ai_hydro.session.refs import to_ref
    assert {"path": to_ref(path), "digest": bound["digest"], "role": "artifact"} in rec["extra"]["retained_files"]


def test_tampered_retained_series_gives_no_edge_and_is_not_consumed(world):
    server, nwis_calls = world
    _fetch(server)
    path = HydroSession.load(SID).streamflow["data"]["_data_file"]
    tampered = json.load(open(path))
    tampered["q_cms"][10] += 1000.0
    json.dump(tampered, open(path, "w"))
    child = _sigs(server)
    assert child["parents"] == [] and child["input_refs"] == []
    assert child["extra"]["parent_unresolved"] == "retained_series_digest_mismatch"
    assert len(nwis_calls) == 2, "tampered series must not be consumed: signatures refetches"
    acq = {a["role"]: a for a in child["extra"]["internal_acquisitions"]}
    assert acq["streamflow"]["mode"] == "internal_nwis_fetch"


def test_slot_with_empty_params_is_a_mismatch_not_a_match(world):
    server, nwis_calls = world
    _fetch(server)
    session = HydroSession.load(SID)
    session.streamflow["meta"]["params"] = {}
    session.save()
    child = _sigs(server, start_date="1990-01-01", end_date="1995-12-31")
    assert child["parents"] == [] and child["input_refs"] == []
    assert "does not equal requested" in child["extra"]["parent_unresolved"]
    assert len(nwis_calls) == 2


def test_slot_covering_a_superset_period_is_not_used(world):
    server, nwis_calls = world
    _fetch(server)                                    # 2000-01-01 .. 2001-12-31
    child = _sigs(server, start_date="2000-03-01", end_date="2001-06-30")
    assert child["parents"] == []
    assert len(nwis_calls) == 2


def test_precipitation_acquisition_is_declared_not_silent(world):
    server, _ = world
    _fetch(server)
    child = _sigs(server)
    acq = {a["role"]: a for a in child["extra"]["internal_acquisitions"]}
    assert set(acq) == {"precipitation"}               # streamflow came from the slot
    p = acq["precipitation"]
    assert (p["start_date"], p["end_date"]) == (START, END)
    # D4: the precipitation the signatures received is declared with a digest
    # (the fixture serves a constant 3.0 mm/day series; product not reported).
    assert p["status"] == "used" and p["reason"] is None
    assert p["data_digest"] and p["data_digest"].startswith("sha256:")
    assert p["n_days"] == 731


def test_unavailable_precipitation_is_declared_as_absent(world, monkeypatch):
    server, _ = world
    monkeypatch.setattr("aihydro_watershed.signatures.signatures._fetch_precipitation_data_bygeom",
                        lambda *a, **k: None)
    _fetch(server)
    child = _sigs(server)
    p = {a["role"]: a for a in child["extra"]["internal_acquisitions"]}["precipitation"]
    assert p["status"] == "unavailable" and p["data_digest"] is None and p["reason"]


def test_fill_valued_precipitation_is_rejected_and_validator_does_not_pass(world, monkeypatch):
    server, _ = world

    def fill(geom, start_date, end_date):
        idx = pd.date_range(start_date, end_date, freq="D")
        return pd.Series(np.full(len(idx), 4.3e32), index=idx)

    monkeypatch.setattr("aihydro_watershed.signatures.signatures._fetch_precipitation_data_bygeom", fill)
    _fetch(server)
    is_err, body = _call(server, "extract_hydrological_signatures",
                         {"session_id": SID, "start_date": START, "end_date": END,
                          "geometry_geojson": SQUARE})
    assert not is_err and not body.get("error"), body
    child = _rows()[body["_run_id"]]["record"]
    p = {a["role"]: a for a in child["extra"]["internal_acquisitions"]}["precipitation"]
    assert p["status"] == "rejected" and p["data_digest"]
    assert body["data"]["runoff_ratio"] is None and body["data"]["stream_elas"] is None
    assert body["data"]["_precipitation"]["status"] == "rejected"
    wb = [f for f in body["quality_flags"] if f.get("validator") == "water_balance_consistency"]
    assert wb and all(f["status"] == "insufficient_data" for f in wb), wb
    flags = json.dumps(body)
    assert "Runoff Ratio: 0.00" not in flags


def test_precipitation_skip_makes_no_request_and_is_declared_not_attempted(world, monkeypatch):
    server, _ = world

    def forbidden(*a, **k):
        raise AssertionError("precipitation='skip' must not fetch")

    monkeypatch.setattr("aihydro_watershed.signatures.signatures._fetch_precipitation_data_bygeom", forbidden)
    _fetch(server)
    is_err, body = _call(server, "extract_hydrological_signatures",
                         {"session_id": SID, "start_date": START, "end_date": END,
                          "geometry_geojson": SQUARE, "precipitation": "skip"})
    assert not is_err and not body.get("error"), body
    assert body["data"]["runoff_ratio"] is None
    child = _rows()[body["_run_id"]]["record"]
    p = {a["role"]: a for a in child["extra"]["internal_acquisitions"]}["precipitation"]
    assert p["status"] == "not_attempted" and p["data_digest"] is None and "skip" in p["reason"]
    wb = [f for f in body["quality_flags"] if f.get("validator") == "water_balance_consistency"]
    assert wb and all(f["status"] == "insufficient_data" for f in wb)
    # a later default call is not served the skipped result from the cache
    _, body2 = _call(server, "extract_hydrological_signatures",
                     {"session_id": SID, "start_date": START, "end_date": END, "geometry_geojson": SQUARE,
                      "precipitation": "bogus"})
    assert body2.get("error") and body2["code"] == "INVALID_PARAMETER"
