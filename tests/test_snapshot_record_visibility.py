"""
Research snapshot visibility for run records (slice 1a, additive fields).

``record_coverage`` and ``record_errors`` ride in the snapshot without a schema
bump. Reading verifies records; it never writes.
"""
from __future__ import annotations

import json

import pytest

from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession, _run_log_record
from ai_hydro.session.surfaces import read_research_snapshot

SID = "visible"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)


def _row(run_id, **kw):
    return {"run_id": run_id, "tool_name": "demo", "session_id": SID,
            "timestamp": "2026-10-02T00:00:00+00:00", "key_outputs": {"v": 1}, **kw}


def _sealed(run_id, **kw):
    entry = _row(run_id)
    rec = rr.build_run_record(run_id=run_id, tool="demo", session_id=SID, entry=entry, **kw)
    return {**entry, "record": rec.to_dict()}


class _Unencodable:
    def __str__(self):
        return "<abbreviated>"


def test_snapshot_reports_coverage_and_record_errors():
    HydroSession(SID).save()
    _run_log_record(SID, "legacy", _row("legacy"))
    _run_log_record(SID, "good", _sealed("good", result={"data": {"v": 1}}))
    _run_log_record(SID, "flawed", _sealed("flawed", result={"data": {"obj": _Unencodable()}}))
    snapshot = read_research_snapshot(SID)

    coverage = snapshot["record_coverage"]
    assert coverage["run_log_rows"] == 3 and coverage["v2_records"] == 2
    assert coverage["legacy_unrecorded"] == 1 and coverage["record_errors"] == 1
    assert coverage["v2_verified"] == 2
    assert snapshot["record_errors"] == [{"run_id": "flawed", "record_error": snapshot["record_errors"][0]["record_error"]}]
    assert "output_digest" in snapshot["record_errors"][0]["record_error"]
    assert snapshot["schema_version"] == 1
    json.dumps(snapshot, allow_nan=False)


def test_snapshot_flags_a_row_edited_in_place_in_sqlite():
    HydroSession(SID).save()
    _run_log_record(SID, "good", _sealed("good"))
    import sqlite3

    path = store._run_log_db_path(SID)
    with sqlite3.connect(str(path)) as conn:
        entry = json.loads(conn.execute("SELECT entry_json FROM runs WHERE run_id='good'").fetchone()[0])
        entry["key_outputs"]["v"] = 2
        conn.execute("UPDATE runs SET entry_json=? WHERE run_id='good'", (json.dumps(entry),))
    coverage = read_research_snapshot(SID)["record_coverage"]
    assert coverage["v2_records"] == 1 and coverage["v2_verified"] == 0
    assert coverage["problems"] == [{"run_id": "good", "problem": "entry_modified_after_sealing"}]


def test_snapshot_without_any_run_log_has_null_coverage():
    HydroSession(SID).save()
    snapshot = read_research_snapshot(SID)
    assert snapshot["record_coverage"]["coverage"] is None and snapshot["record_errors"] == []
