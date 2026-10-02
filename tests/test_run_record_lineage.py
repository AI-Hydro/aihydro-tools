"""
One real parent edge (ADR-001, slice 1a, acceptance criterion 5):
fetch_streamflow_data -> extract_hydrological_signatures.

The signatures tool consumes the streamflow slot. Its record names the run
that stored that slot (``parents``) and carries that run's recorded output
digest and the digest of the series it actually read (``input_refs``, role
``served_data``). The slot carries its run id in ``meta.run_id``, stamped by
the run-log writer; a slot without one yields no edge and says so.
"""
from __future__ import annotations

import asyncio
import json

import fastmcp
import mcp.types as mcp_types
import pytest
from fastmcp import FastMCP

from aihydro_core.records import digest

from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

pytestmark = pytest.mark.skipif(
    int(fastmcp.__version__.split(".")[0]) >= 3,
    reason="real ai_hydro tools need the pinned FastMCP 2.x API",
)

SID = "lineage-session"
Q_CMS = [1.0, 2.5, 4.0, 3.0, 2.0, 1.5] * 10
SQUARE = json.dumps({"type": "Polygon", "coordinates": [[[-77.5, 39.2], [-77.4, 39.2], [-77.4, 39.3],
                                                         [-77.5, 39.3], [-77.5, 39.2]]]})


def call(server, name, arguments):
    handler = server._mcp_server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call", params=mcp_types.CallToolRequestParams(name=name, arguments=arguments))
    result = asyncio.run(handler(req))
    root = result.root if hasattr(result, "root") else result
    return root.isError, json.loads(root.content[0].text)


@pytest.fixture
def world(tmp_path, monkeypatch):
    from ai_hydro.mcp import app
    from ai_hydro.mcp.enforcement import post_run
    from ai_hydro.mcp.helpers import _session_store
    from ai_hydro.session import chat_binding

    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    session = HydroSession(SID)
    session.set("watershed", {"data": {"area_km2": 250.0}, "meta": {"tool": "delineate_watershed"}})
    session.save()

    # Stand-in for fetch_streamflow_data (the real one needs the network): it
    # uses the same two writers, _session_store then post_run, in the same order.
    fetch_server = FastMCP(name="fetch")
    fetch_server.add_middleware(app._ContextInjectionMiddleware())
    fetch_server.add_middleware(app.RunRecordMiddleware())

    @fetch_server.tool()
    def fetch_streamflow_data(session_id: str, gauge_id: str = "01031500") -> dict:
        # Like the real tool: the arrays go to a data file, the slot keeps the path
        # (long arrays are stripped from the persisted slot).
        data_file = tmp_path / "streamflow.json"
        data_file.write_text(json.dumps({"q_cms": Q_CMS}))
        d = {"data": {"q_cms": list(Q_CMS), "n_days": len(Q_CMS), "_data_file": str(data_file)},
             "meta": {"tool": "fetch_streamflow_data"}}
        _session_store(session_id, "streamflow", d, tool_name="fetch_streamflow_data")
        compact = {"data": {"n_days": len(Q_CMS), "q_mean_cms": 2.3}, "meta": d["meta"]}
        return post_run("fetch_streamflow_data", session_id, compact, inputs={"gauge_id": gauge_id})

    def fake_signatures(**kwargs):
        assert kwargs["q_cms_series"] == Q_CMS          # the tool really passed the slot's series
        return {"data": {"q_mean": 2.3, "baseflow_index": 0.4}, "meta": {"tool": "extract_hydrological_signatures"}}

    monkeypatch.setattr("ai_hydro.analysis.signatures.extract_hydrological_signatures", fake_signatures)
    return fetch_server, app.mcp


def _signatures(real_mcp):
    is_error, body = call(real_mcp, "extract_hydrological_signatures",
                          {"session_id": SID, "geometry_geojson": SQUARE})
    assert not is_error and not body.get("error"), body
    return body


def test_fetch_to_signatures_edge_with_served_data_digest(world):
    fetch_server, real_mcp = world
    _, fetched = call(fetch_server, "fetch_streamflow_data", {"session_id": SID})
    rows = HydroSession.load(SID).get("_run_log")
    slot_run = HydroSession.load(SID).streamflow["meta"]["run_id"]
    assert slot_run in rows, "the streamflow slot must name a retained run-log row"
    parent_record = rows[slot_run]["record"]
    assert parent_record["tool"] == "fetch_streamflow_data" and parent_record["output_digest"]

    sigs = _signatures(real_mcp)
    child = HydroSession.load(SID).get("_run_log")[sigs["_run_id"]]["record"]
    assert child["tool"] == "extract_hydrological_signatures"
    assert child["parents"] == [slot_run]
    served = {r["ref"]: r for r in child["input_refs"]}
    assert served[slot_run] == {"ref": slot_run, "digest": parent_record["output_digest"], "role": "served_data"}
    assert served[f"{slot_run}#q_cms"]["digest"] == digest(Q_CMS)
    assert all(r["role"] == "served_data" for r in child["input_refs"])
    assert rr.verify_run_log_entry(HydroSession.load(SID).get("_run_log")[sigs["_run_id"]])["record_ok"]


def test_edge_resolves_through_the_two_rows_the_fetch_call_wrote(world):
    """put_result and post_run both wrote a row for the fetch; both are recorded,
    and the parent named is the one the slot carries."""
    fetch_server, _ = world
    _, fetched = call(fetch_server, "fetch_streamflow_data", {"session_id": SID})
    rows = HydroSession.load(SID).get("_run_log")
    fetch_rows = {rid: r for rid, r in rows.items() if r.get("record", {}).get("tool") == "fetch_streamflow_data"}
    assert len(fetch_rows) == 2 and fetched["_run_id"] in fetch_rows
    assert HydroSession.load(SID).streamflow["meta"]["run_id"] in fetch_rows


def test_slot_without_a_run_id_yields_no_edge_and_says_so(world):
    fetch_server, real_mcp = world
    call(fetch_server, "fetch_streamflow_data", {"session_id": SID})
    session = HydroSession.load(SID)
    session.streamflow["meta"].pop("run_id")           # a slot stored before stamping existed
    session.save()
    sigs = _signatures(real_mcp)
    child = HydroSession.load(SID).get("_run_log")[sigs["_run_id"]]["record"]
    assert child["parents"] == [] and child["input_refs"] == []
    assert "no retained run id" in child["extra"]["parent_unresolved"]


def test_edge_without_a_recorded_producer_is_a_reference_without_a_digest(world, tmp_path):
    """The producer wrote its row outside the middleware (direct call): the edge is
    honest about having no content digest rather than inventing one."""
    from ai_hydro.mcp.helpers import _session_store

    _, real_mcp = world
    data_file = tmp_path / "direct.json"
    data_file.write_text(json.dumps({"q_cms": Q_CMS}))
    _session_store(SID, "streamflow",
                   {"data": {"q_cms": list(Q_CMS), "_data_file": str(data_file)}, "meta": {"tool": "fetch_streamflow_data"}},
                   tool_name="fetch_streamflow_data")
    slot_run = HydroSession.load(SID).streamflow["meta"]["run_id"]
    sigs = _signatures(real_mcp)
    child = HydroSession.load(SID).get("_run_log")[sigs["_run_id"]]["record"]
    assert child["parents"] == [slot_run]
    assert child["input_refs"][0] == {"ref": slot_run, "role": "served_data"}        # no digest key
    assert child["input_refs"][1]["digest"] == digest(Q_CMS)


def test_no_edge_when_the_tool_called_outside_the_middleware(world):
    """Direct Python calls have no recording active; declaring lineage is a no-op."""
    assert rr.declare_lineage(parents=["x"]) is False
