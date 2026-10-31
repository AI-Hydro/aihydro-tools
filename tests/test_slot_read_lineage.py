"""The edge is recorded where the stored result is read (``HydroSession.get`` / ``get_result``).

Before this, a tool that read the session's streamflow declared the producing run only if it remembered
to (one tool did). Now every recorded call that reads a stored result gets that run as a parent.
SYNTHETIC series throughout; calls go through the real MCP handler.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from test_series_tools import SID, call, env, mcp_server, run_log  # noqa: F401  (fixtures)
from ai_hydro.session import lineage as L
from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession

#: Tools that read the session's streamflow slot and, before this change, declared nothing.
CONSUMERS = ("separate_baseflow", "compute_flow_duration_curve", "compute_flood_frequency",
             "check_record_length", "check_stationarity")


def seed(env):
    dates = [d.strftime("%Y-%m-%d") for d in pd.date_range("2000-01-01", periods=3000)]
    q = list(5 + 3 * np.sin(np.arange(3000) / 58.0) + np.random.default_rng(1).random(3000))
    f = env / "q.json"
    f.write_text(json.dumps({"dates": dates, "q_cms": q}))
    s = HydroSession.load(SID)
    s.set("streamflow", {"data": {"dates": dates[:5], "units": "m3/s", "gauge_id": "01031500", "interval": "daily",
                                  "n_days": 3000, "_data_file": str(f)},
                         "meta": {"tool": "fetch_streamflow_data", "computed_at": "2026-10-01T00:00:00+00:00",
                                  "params": {"gauge_id": "01031500", "start_date": "2000-01-01", "end_date": "2008-03-19"}}})
    s.save()
    return HydroSession.load(SID).get("streamflow")["meta"]["run_id"]


def run_all(mcp_server):
    out = {}
    for tool in CONSUMERS:
        err, body, _ = call(mcp_server, tool, {"session_id": SID})
        assert isinstance(body, dict) and body.get("_run_id"), (tool, body)
        out[tool] = run_log()[body["_run_id"]]["record"]
    return out


def test_every_consumer_of_the_streamflow_slot_now_declares_the_producing_run(env, mcp_server):
    producer = seed(env)
    recs = run_all(mcp_server)
    for tool, rec in recs.items():
        assert rec["parents"] == [producer], (tool, rec["parents"])
        assert rec["extra"]["lineage_basis"] == rr.LINEAGE_BASIS, tool


def test_coverage_before_and_after(env, mcp_server, monkeypatch):
    """The measure: how many of the consumers' records name their input run, with the hook off and on."""
    producer = seed(env)
    real = store._note_slot_read
    monkeypatch.setattr(store, "_note_slot_read", lambda res: None)
    before = run_all(mcp_server)
    monkeypatch.setattr(store, "_note_slot_read", real)
    after = run_all(mcp_server)
    n_before = sum(1 for r in before.values() if producer in r["parents"])
    n_after = sum(1 for r in after.values() if producer in r["parents"])
    print(f"slot-reading tools declaring their input run: before {n_before}/{len(CONSUMERS)}, after {n_after}/{len(CONSUMERS)}")
    assert n_before == 0 and n_after == len(CONSUMERS)


def test_trace_sees_the_new_edges(env, mcp_server):
    producer = seed(env)
    recs = run_all(mcp_server)
    err, body, _ = call(mcp_server, "trace_lineage", {"session_id": SID, "node": producer, "direction": "down"})
    assert {n["id"] for n in body["downstream"]["nodes"]} == {r["run_id"] for r in recs.values()}


def test_a_tool_reading_its_own_earlier_output_does_not_name_it_as_a_parent(env, mcp_server):
    """A cache check reads the tool's own previous result; that is not an input."""
    seed(env)
    err, first, _ = call(mcp_server, "check_record_length", {"session_id": SID})
    rid1 = first["_run_id"]
    s = HydroSession.load(SID)
    s.set("record_length_probe", {"data": {"x": 1}, "meta": {"tool": "check_record_length", "run_id": rid1}})
    tok = rr.begin_capture(tool="check_record_length")
    try:
        rr.note_slot_read(rid1, "check_record_length")
        capture = rr._CAPTURE.get()
        assert capture.slot_parents == []
        rr.note_slot_read(rid1, "separate_baseflow")
        assert capture.slot_parents == [rid1]
    finally:
        rr.end_capture(tok[1])


def test_an_unrecorded_status_tool_declares_nothing():
    cap, tok = rr.begin_capture(tool="get_session_summary")
    try:
        rr.note_slot_read("streamflow.abc", "fetch_streamflow_data")
        assert cap.slot_parents == []
    finally:
        rr.end_capture(tok)


def test_an_explicit_not_consumed_note_wins_over_the_slot_reads(env, mcp_server):
    """signatures says 'the slot did not match, I did not consume it': the generic read must not contradict that."""
    cap, tok = rr.begin_capture(tool="extract_hydrological_signatures")
    try:
        rr.note_slot_read("streamflow.abc", "fetch_streamflow_data")
        rr.declare_lineage(parent_unresolved="slot params differ")
        assert cap.slot_parents == ["streamflow.abc"] and cap.notes["parent_unresolved"]
    finally:
        rr.end_capture(tok)


def test_arbitrary_code_runs_get_no_lineage_basis():
    assert rr._slot_reads_cover("run_python") is False and rr._slot_reads_cover("separate_baseflow") is True


def test_a_claim_chain_is_still_traceable_after_the_change(env, mcp_server):
    producer = seed(env)
    recs = run_all(mcp_server)
    sid = recs["separate_baseflow"]["run_id"]
    rows = run_log()
    lin = L.trace_to_observed(rows, sid)
    # the producer here is a hand-seeded slot row with no sealed record, so the honest answer is unknown
    assert lin.state == L.UNKNOWN and producer in " ".join(lin.reasons)


def test_concurrent_reads_of_one_slot_name_the_producer_once(env, mcp_server):
    producer = seed(env)
    cap, tok = rr.begin_capture(tool="separate_baseflow")
    try:
        cap.slot_parents.extend([producer, producer])           # what two racing worker threads can leave behind
        parents = list(dict.fromkeys(cap.slot_parents))
        assert parents == [producer]
    finally:
        rr.end_capture(tok)
