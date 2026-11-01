"""find_studies_for_gauge: which studies are bound to a gauge, and which runs name it (synthetic studies)."""
from __future__ import annotations

import json

import pytest

from test_series_tools import SID, call, env, mcp_server  # noqa: F401  (fixtures)
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from ai_hydro.session import study_index as si


def study(sid, site_id=None, rows=(), workspace=None):
    s = HydroSession(sid)
    s.site_id = site_id or ""
    if workspace:
        s.workspace_dir = str(workspace)
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
    monkeypatch.setattr(si, "_read_run_rows", lambda sid, needle=None: (_ for _ in ()).throw(OSError("unreadable")))
    (s,) = si.runs_holding_gauge("01013500", include_hidden=True)["studies"]
    assert s["run_log_readable"] is False


def test_the_tool_through_the_handler_and_its_annotation(env, mcp_server):
    study("s-fish", "01013500", [("q.1", {"tool_name": "data_fetch", "key_outputs": {"gauge_id": "01013500"}})])
    err, body, _ = call(mcp_server, "find_studies_for_gauge", {"gauge_id": "01013500", "scope": "all", "include_hidden": True})
    assert not err and any(s["study_id"] == "s-fish" and s["bound_to_gauge"] for s in body["studies"]) and body["scope"] == "all"
    err, bad, _ = call(mcp_server, "find_studies_for_gauge", {"gauge_id": "GB-1"})
    assert bad["code"] == "INVALID_PARAMETER"
    from ai_hydro.mcp.tool_surface import load_annotation_rows

    assert load_annotation_rows()["find_studies_for_gauge"]["readOnlyHint"] is True


def _files(db):
    return sorted(p.name for p in db.parent.iterdir())


def test_reading_run_rows_creates_nothing_beside_the_database(env):
    """An idle WAL store: a read-only open would leave -shm / -wal next to every study. This one leaves the directory as it was."""
    study("s-fish", "01013500", [("q.1", {"tool_name": "data_fetch", "key_outputs": {"gauge_id": "01013500"}})])
    db = store._run_log_db_path("s-fish")
    before = _files(db)
    assert list(si._read_run_rows("s-fish")) == ["q.1"] and si._read_run_rows("never-existed") == {}
    assert _files(db) == before and not any(p.is_dir() for p in db.parent.iterdir())
    assert not [n for n in _files(db) if n.endswith(("-shm", "-wal"))]
    si.runs_holding_gauge("01013500", include_hidden=True)
    assert _files(db) == before


def test_an_uncheckpointed_wal_creates_nothing_and_is_reported_not_hidden(env):
    """A live writer keeps rows in the -wal file. They are not read (reading would create sidecars or copies); the study says so."""
    study("s-fish", "01013500")
    db = store._run_log_db_path("s-fish")
    writer = store._run_log_connect("s-fish")                  # keeps the WAL open
    try:
        writer.execute("INSERT INTO runs (run_id, timestamp, entry_json) VALUES (?,?,?)",
                       ("w.1", "2026-10-03T00:00:00+00:00", json.dumps({"key_outputs": {"gauge_id": "01013500"}})))
        writer.commit()
        wal = db.parent / (db.name + "-wal")
        assert wal.exists() and wal.stat().st_size > 0                 # the premise
        before = _files(db)
        assert si.wal_pending("s-fish") is True
        (s,) = si.runs_holding_gauge("01013500", include_hidden=True)["studies"]
        assert s["run_log_recent_rows_may_be_missing"] is True and s["bound_to_gauge"] is True
        assert _files(db) == before
    finally:
        writer.close()
    assert si.wal_pending("s-fish") is False
    assert [r["run_id"] for r in si.runs_holding_gauge("01013500", include_hidden=True)["studies"][0]["runs"]] == ["w.1"]


def test_an_integer_gauge_id_is_found_and_the_prefilter_does_not_hide_it(env):
    study("s-int", "01013500", [("q.1", {"tool_name": "data_fetch", "key_outputs": {"gauge_id": 1013500}}),
                                ("q.2", {"tool_name": "data_fetch", "key_outputs": {"gauge_id": "01013500"}}),
                                ("q.3", {"tool_name": "data_fetch", "key_outputs": {"gauge_id": 1013501}})])
    (s,) = si.runs_holding_gauge("01013500", include_hidden=True)["studies"]
    assert sorted(r["run_id"] for r in s["runs"]) == ["q.1", "q.2"]
    assert si._norm_gauge(1013500) == "01013500" and si._norm_gauge(True) is None and si._norm_gauge(12) is None
    assert [r for r in si._read_run_rows("s-int", "1013500")] == ["q.1", "q.2"]        # SQL prefilter: q.3 never parsed


def test_the_study_cap_is_reported(env):
    for i in range(5):
        study(f"s-{i}", "01013500")
    out = si.runs_holding_gauge("01013500", include_hidden=True, max_studies=3)
    assert out["n_studies"] == 5 and len(out["studies"]) == 3 and out["studies_truncated"] is True
    assert si.runs_holding_gauge("01013500", include_hidden=True)["studies_truncated"] is False


def test_default_scope_is_the_current_workspace_and_paths_are_never_absolute(env, mcp_server, tmp_path):
    ws, other = tmp_path / "proj-a", tmp_path / "proj-b"
    ws.mkdir(); other.mkdir()
    cur = HydroSession.load(SID)
    cur.workspace_dir = str(ws)
    cur.save()
    study("s-here", "01013500", workspace=ws)
    study("s-there", "01013500", workspace=other)
    err, body, _ = call(mcp_server, "find_studies_for_gauge", {"gauge_id": "01013500", "include_hidden": True, "session_id": SID})
    assert not err and body["scope"] == "workspace"
    assert [s["study_id"] for s in body["studies"]] == ["s-here"]
    assert body["studies"][0]["workspace"] == "proj-a" and str(tmp_path) not in json.dumps(body)
    err, body, _ = call(mcp_server, "find_studies_for_gauge", {"gauge_id": "01013500", "scope": "all", "include_hidden": True})
    assert {s["study_id"] for s in body["studies"]} >= {"s-here", "s-there"} and str(tmp_path) not in json.dumps(body)
    err, bad, _ = call(mcp_server, "find_studies_for_gauge", {"gauge_id": "01013500", "scope": "galaxy"})
    assert bad["code"] == "INVALID_PARAMETER"


def test_workspace_scope_without_a_workspace_says_how_to_widen(env, mcp_server):
    err, body, _ = call(mcp_server, "find_studies_for_gauge", {"gauge_id": "01013500", "session_id": SID})
    assert body["code"] == "NO_WORKSPACE_SCOPE" and "scope='all'" in body["recovery"]


def test_a_read_that_hits_a_moving_checkpoint_is_retried_once(env, monkeypatch):
    import sqlite3

    study("s-fish", "01013500", [("q.1", {"tool_name": "data_fetch", "key_outputs": {"gauge_id": "01013500"}})])
    real, calls = sqlite3.connect, []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real(*a, **k)
    monkeypatch.setattr(sqlite3, "connect", flaky)
    assert list(si._read_run_rows("s-fish")) == ["q.1"] and len(calls) == 2
    calls.clear()
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(sqlite3.DatabaseError("still bad")))
    (s,) = si.runs_holding_gauge("01013500", include_hidden=True)["studies"]
    assert s["run_log_readable"] is False                                  # twice is enough: unknown, not empty
