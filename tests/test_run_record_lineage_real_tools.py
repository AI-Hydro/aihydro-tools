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
    acq = child["extra"]["streamflow_acquisition"]
    assert acq["mode"] == "internal_nwis_fetch" and "start_date" in acq["reason"]
    assert "fetched its own" in child["extra"]["parent_unresolved"]
