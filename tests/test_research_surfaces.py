"""Real persisted fixtures, no live user sessions or backend migrations."""
import asyncio
import base64
import json
import sqlite3

import pytest

from ai_hydro.session import store
from ai_hydro.session.surfaces import SnapshotError, read_research_snapshot, snapshot_resource


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)


@pytest.fixture
def session():
    s = store.HydroSession("surface")
    for rid, timestamp, nse in [("first", "2026-01-01", 0.5), ("second", "2026-01-02", 0.8)]:
        s.put_result("evaluation", "basin", "same-params", {
            "run_id": rid, "data": {"nse": nse},
            "meta": {"tool": "evaluate", "computed_at": timestamp}})
    s.set("_experiments", {"exp": {"defn": {"name": "Synthetic experiment"}, "results": {"status": "complete"}}})
    s.claims["c1"] = {"id": "c1", "claim": "Fixture", "evidence_spans": [{"source_type": "run", "source_id": "first"}]}
    s.save()
    return s


def test_sqlite_runs_survive_repeated_tool_and_slot_writes(session):
    snapshot = read_research_snapshot("surface")
    assert snapshot["run_log_source"] == "sqlite"
    evaluations = [r for r in snapshot["runs"] if r["tool_name"] == "evaluate"]
    assert [r["run_id"] for r in evaluations] == ["first", "second"]
    assert [r["key_outputs"]["nse"] for r in evaluations] == [0.5, 0.8]
    assert evaluations[1]["evidence"]["data"]["nse"] == 0.8
    assert snapshot["experiments"]["exp"]["defn"]["name"] == "Synthetic experiment"
    assert snapshot["claims"]["c1"]["evidence_spans"][0]["source_id"] == "first"


def test_read_does_not_modify_session_or_database(session):
    files = [store._SESSIONS_DIR / "surface.json", store._run_log_db_path("surface")]
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
    read_research_snapshot("surface")
    assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in files] == before


@pytest.mark.parametrize("wrapper", ["bare", "data", "v2", "list"])
def test_legacy_run_logs_read_without_migration(tmp_path, wrapper):
    entry = {"run_id": "old", "tool_name": "evaluate", "key_outputs": {"nse": 0.8}}
    log = {"old": entry}
    if wrapper == "data":
        log = {"data": log}
    elif wrapper == "v2":
        log = {"__legacy__": {"": log}}
    elif wrapper == "list":
        log = [entry]
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"session_id": "old-session", "_run_log": log}))
    before = path.read_bytes()
    snapshot = read_research_snapshot(str(path))
    assert snapshot["runs"][0]["run_id"] == "old"
    assert snapshot["run_log_source"] == "legacy_json"
    assert path.read_bytes() == before
    assert not path.with_suffix(".runlog.sqlite3").exists()


def test_missing_history_does_not_invent_runs_from_current_results(tmp_path):
    path = tmp_path / "no-history.json"
    path.write_text(json.dumps({"session_id": "empty", "evaluation": {
        "data": {"nse": 0.8}, "meta": {"tool": "evaluate", "computed_at": "2026-01-01"}}}))
    snapshot = read_research_snapshot(str(path))
    assert snapshot["runs"] == []
    assert snapshot["run_log_source"] == "absent"
    assert snapshot["warnings"]


def test_capsule_uses_own_run_file_not_live_same_id(session, tmp_path):
    directory = tmp_path / "capsule with spaces"
    directory.mkdir()
    (directory / "session.json").write_text(json.dumps({"session_id": "surface"}))
    (directory / "run_log.json").write_text(json.dumps({"capsule-only": {"nse": 0.1}}))
    snapshot = read_research_snapshot(str(directory))
    assert snapshot["source"] == "capsule"
    assert snapshot["run_log_source"] == "capsule_json"
    assert [r["run_id"] for r in snapshot["runs"]] == ["capsule-only"]
    assert snapshot["runs"][0]["key_outputs"] == {"nse": 0.1}


def test_empty_sqlite_log_wins_over_old_embedded_json(session):
    with sqlite3.connect(store._run_log_db_path("surface")) as conn:
        conn.execute("DELETE FROM runs")
    path = store._SESSIONS_DIR / "surface.json"
    raw = json.loads(path.read_text())
    raw["_run_log"] = {"stale": {"nse": 0.7}}
    path.write_text(json.dumps(raw))
    assert read_research_snapshot("surface")["runs"] == []


@pytest.mark.parametrize("corruption", ["database", "row", "identity"])
def test_corrupt_logs_and_conflicting_identity_fail_explicitly(session, corruption):
    path = store._run_log_db_path("surface")
    if corruption == "database":
        path.write_bytes(b"not sqlite")
    else:
        with sqlite3.connect(path) as conn:
            payload = "not json" if corruption == "row" else json.dumps({"session_id": "foreign"})
            conn.execute("UPDATE runs SET entry_json = ? WHERE run_id = ?", (payload, "first"))
    with pytest.raises(SnapshotError):
        read_research_snapshot("surface")


def test_nonfinite_values_become_strict_json_null(session):
    session.set("_run_log", {"nonfinite": {"key_outputs": {"nan": float("nan"), "inf": float("inf")}}})
    snapshot = read_research_snapshot("surface")
    record = next(r for r in snapshot["runs"] if r["run_id"] == "nonfinite")
    assert record["key_outputs"] == {"nan": None, "inf": None}
    json.dumps(snapshot, allow_nan=False)


def test_resource_reference_preserves_explicit_path_and_unicode(session):
    path = str(store._SESSIONS_DIR / "surface.json")
    ref = base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")
    assert json.loads(snapshot_resource(ref))["session_id"] == "surface"
    assert json.loads(snapshot_resource("!invalid"))["code"] == "INVALID_REFERENCE"


def test_registered_mcp_resource_returns_same_snapshot(session):
    from ai_hydro.mcp.app import mcp
    ref = base64.urlsafe_b64encode(b"surface").decode().rstrip("=")
    response = asyncio.run(mcp.read_resource("aihydro://research/snapshot/" + ref))
    assert json.loads(response.contents[0].content) == read_research_snapshot("surface")


def test_missing_session_resource_is_explicit_error():
    ref = base64.urlsafe_b64encode(b"missing").decode().rstrip("=")
    assert json.loads(snapshot_resource(ref))["code"] == "SESSION_NOT_FOUND"


def test_active_feature_experiment_slot_uses_backend_selection(session):
    session.active_feature_id = "basin"
    session.put_result("_experiments", "basin", "older", {"data": {"old": {}}, "meta": {"computed_at": "2025"}})
    session.put_result("_experiments", "basin", "newer", {"data": {"new": {}}, "meta": {"computed_at": "2026"}})
    session.save()
    assert read_research_snapshot("surface")["experiments"] == {"new": {}}


def test_canonical_session_named_session_uses_sqlite():
    s = store.HydroSession("session")
    s.put_result("evaluation", "basin", "params", {
        "run_id": "retained", "data": {"nse": 0.4},
        "meta": {"tool": "evaluate", "computed_at": "2026-01-01"}})
    s.save()
    snapshot = read_research_snapshot("session")
    assert snapshot["source"] != "capsule"
    assert snapshot["run_log_source"] == "sqlite"
    assert any(run["run_id"] == "retained" for run in snapshot["runs"])
