"""Study recorder (T-B2): every file a tool writes and every layer it pushes is recorded in the owning
study's artifact manifest at one call site; study labels; study/list and study/by-artifact resources."""
from __future__ import annotations

import hashlib
import json
import time

import pytest

from binding_helpers import CHAT_A, call, ctx, make_study, read_resource

from ai_hydro.session import HydroSession
from ai_hydro.session.study_index import by_artifact, make_label, study_summaries

GEOJSON = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
           "geometry": {"type": "Polygon", "coordinates": [[[-69.5, 45.0], [-69.0, 45.0], [-69.0, 45.4], [-69.5, 45.0]]]}}]}


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "try"
    d.mkdir()
    return d


@pytest.fixture
def fixture_delineation(monkeypatch):
    """The real ``delineate_watershed`` tool with only the network backend replaced."""
    def fake(gauge_id=None, **kw):
        return {"data": {"area_km2": 774.2, "gauge_id": gauge_id, "gauge_name": "Piscataquis River Near Dover-Foxcroft, Maine",
                         "gauge_lat": 45.175, "gauge_lon": -69.31, "geometry_geojson": GEOJSON},
                "meta": {"tool": "delineate_watershed"}}

    monkeypatch.setattr("ai_hydro.analysis.watershed.delineate_watershed", fake)
    monkeypatch.setattr("ai_hydro.analysis.plots.plot_watershed_map", lambda **kw: None)


def _run_row(session_id: str, run_id: str) -> dict:
    return (HydroSession.load(session_id).get("_run_log") or {}).get(run_id)


# --------------------------------------------------------------------- recorder

def test_delineation_records_the_watershed_file_with_the_run_id_of_its_run_row(ws, fixture_delineation):
    res, body = call("delineate_watershed", {"gauge_id": "01031500", "workspace_dir": str(ws)}, meta=ctx(CHAT_A, ws))
    assert not res.is_error and not body.get("error"), body
    manifest = HydroSession.load("01031500").artifact_manifest
    entry = manifest["file:watershed_01031500.geojson"]
    assert entry["path"] == "watershed_01031500.geojson"
    assert entry["study_id"] == "01031500" and entry["tool"] == "delineate_watershed"
    assert entry["sha256"] == hashlib.sha256((ws / "watershed_01031500.geojson").read_bytes()).hexdigest()
    assert entry["run_id"] == body["_run_id"]
    assert _run_row("01031500", entry["run_id"]) is not None          # the run row it names exists


def test_a_pushed_layer_is_recorded_with_its_content_digest(ws, fixture_delineation):
    call("delineate_watershed", {"gauge_id": "01031500", "workspace_dir": str(ws)}, meta=ctx(CHAT_A, ws))
    layer = HydroSession.load("01031500").artifact_manifest["layer:watershed_01031500"]
    assert layer["type"] == "map_layer" and layer["layer_type"] == "polygon"
    assert layer["sha256"] == hashlib.sha256(json.dumps(GEOJSON).encode()).hexdigest()
    assert layer["tool"] == "delineate_watershed" and layer["run_id"]


def test_one_recorder_covers_any_tool_that_writes_through_the_helpers(ws):
    """No per-tool code: a tool nobody told about the recorder is recorded the same way."""
    import asyncio

    import mcp.types as mcp_types
    from fastmcp import FastMCP

    from ai_hydro.mcp import app
    from ai_hydro.mcp.enforcement import post_run
    from ai_hydro.mcp.helpers import _workspace_write

    srv = FastMCP(name="probe")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())

    @srv.tool()
    def export_something(session_id: str) -> dict:
        _workspace_write(session_id, "out/table.csv", "a,b\n1,2\n")
        _workspace_write(session_id, "report.md", "# r")
        return post_run("export_something", session_id, {"data": {"n": 2}}, inputs={})

    make_study("s1", workspace=str(ws))
    handler = srv._mcp_server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(method="tools/call", params=mcp_types.CallToolRequestParams(
        name="export_something", arguments={"session_id": "s1"}))
    asyncio.run(handler(req))
    entries = {e["path"]: e for e in HydroSession.load("s1").artifact_manifest.values()
               if e.get("kind") == "workspace_artifact"}
    assert set(entries) == {"out/table.csv", "report.md"}
    assert all(e["tool"] == "export_something" and e["study_id"] == "s1" and e["run_id"] for e in entries.values())
    assert entries["out/table.csv"]["sha256"] == hashlib.sha256(b"a,b\n1,2\n").hexdigest()


def test_a_read_only_call_records_nothing_and_does_not_touch_the_study(ws):
    make_study("s1", workspace=str(ws))
    path = HydroSession._path("s1", None)
    before = path.stat().st_mtime_ns
    time.sleep(0.01)
    call("list_features", {"session_id": "s1"}, meta=ctx(CHAT_A, ws))
    assert path.stat().st_mtime_ns == before


def test_a_file_outside_the_study_workspace_is_not_recorded(tmp_path, ws):
    from ai_hydro.session import run_records
    from ai_hydro.session.artifact_recorder import record_artifacts

    make_study("s1", workspace=str(ws))
    outside = tmp_path / "elsewhere.json"
    outside.write_text("{}")
    capture, token = run_records.begin_capture()
    try:
        run_records.note_file_written(outside, "s1")
    finally:
        run_records.end_capture(token)
    assert record_artifacts(capture, tool="t", call_session_id="s1", run_id="r") == 0


def test_the_recorder_never_resurrects_a_cleared_study(ws):
    from ai_hydro.session import run_records
    from ai_hydro.session.artifact_recorder import record_artifacts
    from ai_hydro.session.study_index import study_exists

    (ws / "f.json").write_text("{}")
    capture, token = run_records.begin_capture()
    try:
        run_records.note_file_written(ws / "f.json", "ghost")
    finally:
        run_records.end_capture(token)
    assert record_artifacts(capture, tool="t", call_session_id="ghost", run_id="r") == 0
    assert not study_exists("ghost")


# --------------------------------------------------------------------- by-artifact

def test_by_artifact_resolves_a_recorded_file_with_rule_manifest(ws, fixture_delineation):
    call("delineate_watershed", {"gauge_id": "01031500", "workspace_dir": str(ws)}, meta=ctx(CHAT_A, ws))
    out = read_resource(f"aihydro://study/by-artifact?path={ws / 'watershed_01031500.geojson'}")
    assert out["rule"] == "manifest" and out["resolved"] is True and out["study_id"] == "01031500"
    # a workspace-relative path with the workspace named resolves the same way
    out = read_resource(f"aihydro://study/by-artifact?path=watershed_01031500.geojson&workspace={ws}")
    assert out["resolved"] is True and out["study_id"] == "01031500"


def test_a_rewritten_file_no_longer_matches_the_old_manifest_entry(ws, fixture_delineation):
    call("delineate_watershed", {"gauge_id": "01031500", "workspace_dir": str(ws)}, meta=ctx(CHAT_A, ws))
    (ws / "watershed_01031500.geojson").write_text('{"type": "FeatureCollection", "features": []}')
    out = read_resource(f"aihydro://study/by-artifact?path={ws / 'watershed_01031500.geojson'}")
    assert out["resolved"] is False and out.get("stale_manifest") is True


def test_a_legacy_file_in_a_two_study_folder_returns_candidates_never_a_bind(ws):
    make_study("01031500", workspace=str(ws), site_id="01031500", site_name="Piscataquis River", site_type="usgs_gauge")
    make_study("piscataquis-river-dover-foxcroft-01031500", workspace=str(ws))     # no site_id, id carries the gauge
    make_study("basin_24p203_75p746", workspace=str(ws))
    (ws / "watershed_01031500.geojson").write_text("{}")                           # written before the recorder
    out = read_resource(f"aihydro://study/by-artifact?path={ws / 'watershed_01031500.geojson'}")
    assert out["rule"] == "artifact_name_heuristic" and out["resolved"] is False and out["study_id"] is None
    assert {c["study_id"] for c in out["candidates"]} == {"01031500", "piscataquis-river-dover-foxcroft-01031500"}
    assert all(c["label"] for c in out["candidates"])


def test_a_single_heuristic_match_is_still_only_a_candidate(ws):
    make_study("01031500", workspace=str(ws), site_id="01031500")
    (ws / "watershed_01031500.geojson").write_text("{}")
    out = by_artifact(str(ws / "watershed_01031500.geojson"))
    assert out["resolved"] is False and out["study_id"] is None and len(out["candidates"]) == 1


def test_a_file_in_another_folder_or_with_no_identifier_matches_nothing(ws, tmp_path):
    make_study("01031500", workspace=str(ws), site_id="01031500")
    (ws / "notes.txt").write_text("x")
    other = tmp_path / "other"
    other.mkdir()
    (other / "watershed_01031500.geojson").write_text("{}")
    assert by_artifact(str(ws / "notes.txt"))["candidates"] == []
    assert by_artifact(str(other / "watershed_01031500.geojson"))["candidates"] == []


# --------------------------------------------------------------------------- labels

def test_label_priorities():
    assert make_label("x", {"extra": {"study_label": "My basin"}, "site_name": "Other"}) == "My basin"
    assert make_label("01031500", {"site_id": "01031500", "site_name": "Piscataquis River near Dover-Foxcroft, ME",
                                   "site_type": "usgs_gauge"}) == "Piscataquis River near Dover-Foxcroft, ME (USGS 01031500)"
    assert make_label("a", {"watershed": {"__legacy__": {"": {"data": {"outlet_lat": 24.203, "outlet_lon": 75.7455}}}}}) \
        == "Basin at 24.20°N, 75.75°E"
    assert make_label("a", {"watershed": {"__legacy__": {"": {"data": {"gauge_lat": 45.1, "gauge_lon": -69.3}}}}}) \
        == "Basin at 45.10°N, 69.30°W"
    assert make_label("mystery", {}) == "mystery (unnamed study)"


def test_a_name_that_is_only_the_study_id_is_not_a_name():
    assert make_label("Nepal_flash_outlet", {"site_name": "Nepal_flash_outlet", "site_type": "pour_point"}) \
        == "Nepal_flash_outlet (unnamed study)"


@pytest.mark.parametrize("raw", [
    {}, {"site_id": "01031500"}, {"site_name": "slug"}, {"site_name": "slug", "site_id": "slug"},
    {"site_id": "01031500", "site_type": "usgs_gauge"}, {"extra": {"study_label": ""}},
    {"watershed": {"__legacy__": {"": {"data": {"outlet_lat": 1.0, "outlet_lon": 2.0}}}}},
])
def test_no_label_is_a_bare_id(raw):
    for sid in ("slug", "01031500", "basin_1p0_2p0", "x"):
        label = make_label(sid, raw)
        assert label and label != sid and label != raw.get("site_id")


# ----------------------------------------------------------------- study list

def test_test_studies_are_hidden_by_default_and_present_with_include_hidden(ws):
    make_study("01031500", workspace=str(ws), site_id="01031500")
    make_study("test-promotion")
    make_study("e2e-proof-1-20261002T095800Z")
    make_study("test-but-in-a-project", workspace=str(ws))                 # has a workspace: a real study
    make_study("old", workspace=str(ws), archived=True)
    default = read_resource("aihydro://study/list")
    assert {s["study_id"] for s in default["studies"]} == {"01031500", "test-but-in-a-project"}
    everything = read_resource("aihydro://study/list?include_hidden=true")
    by_id = {s["study_id"]: s for s in everything["studies"]}
    assert set(by_id) == {"01031500", "test-promotion", "e2e-proof-1-20261002T095800Z", "test-but-in-a-project", "old"}
    assert by_id["test-promotion"]["hidden"] == "test_study" and by_id["old"]["hidden"] == "archived"
    assert "hidden" not in by_id["01031500"]
    from ai_hydro.session.study_index import study_exists

    assert study_exists("test-promotion")                                  # hidden, never deleted


def test_study_list_filters_by_workspace_and_every_row_has_a_label(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    make_study("a", workspace=str(ws))
    make_study("b", workspace=str(other))
    rows = read_resource(f"aihydro://study/list?workspace={ws}")["studies"]
    assert [r["study_id"] for r in rows] == ["a"] and rows[0]["label"] == "a (unnamed study)"


def test_study_list_answers_fast_for_fifty_studies_without_loading_sessions(ws, monkeypatch):
    for i in range(50):
        make_study(f"basin_{i}", workspace=str(ws), site_id=f"{i:08d}", site_name=f"River {i}", site_type="usgs_gauge")
    monkeypatch.setattr(HydroSession, "load", classmethod(lambda cls, *a, **k: pytest.fail("full session load")))
    study_summaries()                                                      # warm: first read parses each file once
    t0 = time.perf_counter()
    rows = study_summaries(workspace=str(ws))
    assert len(rows) == 50 and time.perf_counter() - t0 < 0.2
    t0 = time.perf_counter()
    from ai_hydro.mcp.resources import study_list

    json.loads(study_list(workspace=str(ws)))
    assert time.perf_counter() - t0 < 0.2
