"""find_studies_for_gauge: which studies are bound to a gauge, and which runs name it (synthetic studies)."""
from __future__ import annotations

import pytest

from test_series_tools import SID, call, env, mcp_server  # noqa: F401  (fixtures)
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from ai_hydro.session import study_index as si


def study(sid, site_id=None, rows=()):
    s = HydroSession(sid)
    s.site_id = site_id or ""
    s.save()
    for rid, entry in rows:
        store._run_log_record(sid, rid, {"run_id": rid, "session_id": sid, "timestamp": entry.pop("timestamp", "2026-10-01T00:00:00+00:00"), **entry})


def test_gauges_are_read_from_what_a_row_recorded():
    row = {"key_outputs": {"gauge_id": "USGS-01013500", "site_id": "not a gauge"},
           "inputs": {"resolved_place": {"gauge": "01031500"}, "gauge_id": "01013500"}}
    assert si.gauges_of_row(row) == {"01013500": "key_outputs.gauge_id", "01031500": "inputs.resolved_place.gauge"}
    assert si.gauges_of_row({"key_outputs": {"basin_gauge_id": "01013500"}}) == {"01013500": "key_outputs.basin_gauge_id"}
    assert si.gauges_of_row({"key_outputs": {"note": "gauge 01013500"}}) == {}          # prose is not read


def test_studies_bound_to_or_holding_a_gauge(env):
    study("s-fish", "01013500", [("q.1", {"tool_name": "data_fetch", "slot": "streamflow", "key_outputs": {"gauge_id": "01013500"}}),
                                 ("w.1", {"tool_name": "delineate_watershed", "slot": "watershed", "key_outputs": {"area_km2": 2250.0}})])
    study("s-other", "01031500", [("q.2", {"tool_name": "data_fetch", "inputs": {"gauge_id": "USGS-01013500"}, "timestamp": "2026-10-02T00:00:00+00:00"}),
                                  ("q.3", {"tool_name": "data_fetch", "key_outputs": {"gauge_id": "01031500"}})])
    study("s-none", None, [("x.1", {"tool_name": "run_python", "key_outputs": {"a": 1.0}})])
    out = si.runs_holding_gauge("USGS-01013500", include_hidden=True)
    ids = [s["study_id"] for s in out["studies"]]
    assert ids[0] == "s-fish" and "s-other" in ids and "s-none" not in ids
    fish, other = out["studies"][0], next(s for s in out["studies"] if s["study_id"] == "s-other")
    assert fish["bound_to_gauge"] is True and [r["run_id"] for r in fish["runs"]] == ["q.1"]
    assert other["bound_to_gauge"] is False and [r["run_id"] for r in other["runs"]] == ["q.2"] and other["runs"][0]["named_in"] == "inputs.gauge_id"
    assert si.runs_holding_gauge("01013500", include_hidden=True)["gauge"] == "01013500"
    assert si.runs_holding_gauge("nope")["studies"] == []


def test_a_bound_study_with_no_runs_is_listed_and_the_run_cap_is_reported(env):
    study("s-empty", "01013500")
    study("s-many", "01013500", [(f"q.{i}", {"tool_name": "data_fetch", "key_outputs": {"gauge_id": "01013500"},
                                           "timestamp": f"2026-10-{i + 1:02d}T00:00:00+00:00"}) for i in range(5)])
    out = si.runs_holding_gauge("01013500", include_hidden=True, max_runs_per_study=2)
    by = {s["study_id"]: s for s in out["studies"]}
    assert by["s-empty"]["n_runs_naming_gauge"] == 0 and by["s-empty"]["bound_to_gauge"]
    assert by["s-many"]["n_runs_naming_gauge"] == 5 and len(by["s-many"]["runs"]) == 2 and by["s-many"]["runs_truncated"]
    assert by["s-many"]["runs"][0]["run_id"] == "q.4"                                      # newest first


def test_an_unreadable_run_log_is_unknown_not_empty(env, monkeypatch):
    study("s-fish", "01013500")
    monkeypatch.setattr(si, "_read_run_rows", lambda sid: (_ for _ in ()).throw(OSError("unreadable")))
    (s,) = si.runs_holding_gauge("01013500", include_hidden=True)["studies"]
    assert s["run_log_readable"] is False


def test_the_tool_through_the_handler_and_its_annotation(env, mcp_server):
    study("s-fish", "01013500", [("q.1", {"tool_name": "data_fetch", "key_outputs": {"gauge_id": "01013500"}})])
    err, body, _ = call(mcp_server, "find_studies_for_gauge", {"gauge_id": "01013500", "include_hidden": True})
    assert not err and any(s["study_id"] == "s-fish" and s["bound_to_gauge"] for s in body["studies"])
    err, bad, _ = call(mcp_server, "find_studies_for_gauge", {"gauge_id": "GB-1"})
    assert bad["code"] == "INVALID_PARAMETER"
    from ai_hydro.mcp.tool_surface import load_annotation_rows

    assert load_annotation_rows()["find_studies_for_gauge"]["readOnlyHint"] is True


def test_reading_run_rows_leaves_the_store_unchanged_and_makes_no_directory(env):
    study("s-fish", "01013500", [("q.1", {"tool_name": "data_fetch", "key_outputs": {"gauge_id": "01013500"}})])
    db = store._run_log_db_path("s-fish")
    sidecar = ("-wal", "-shm")                  # SQLite may create these when it opens a WAL store, even read-only

    def snapshot():
        return {p.name: p.read_bytes() for p in db.parent.iterdir() if p.is_file() and not p.name.endswith(sidecar)}
    before = snapshot()
    assert list(si._read_run_rows("s-fish")) == ["q.1"] and si._read_run_rows("never-existed") == {}
    assert snapshot() == before and not any(p.is_dir() for p in db.parent.iterdir())
