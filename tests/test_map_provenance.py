"""T-M1 (MP0, tools half): the map provenance envelope, ``map_add_layer_from_run`` through the run
store and the recorded artifact, ``bind_claim_to_item`` as a ledger link only, ``map_get_state(scope)``,
and the background job's captured ``chat_id``.

Every test runs under its own AIHYDRO_HOME (tests/conftest.py); map events and commands are redirected
to the test's tmp_path as well.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from binding_helpers import CHAT_A, CHAT_B, call, ctx, make_study

from ai_hydro.session import HydroSession

GEOJSON = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
           "geometry": {"type": "Polygon", "coordinates": [[[-69.5, 45.0], [-69.0, 45.0], [-69.0, 45.4], [-69.5, 45.0]]]}}]}


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "try"
    d.mkdir()
    return d


@pytest.fixture
def map_dirs(tmp_path, monkeypatch):
    events, commands = tmp_path / "map_events", tmp_path / "map_commands"
    monkeypatch.setattr("ai_hydro.mcp.map_events._MAP_EVENTS_DIR", events)
    monkeypatch.setattr("ai_hydro.mcp.map_commands._MAP_COMMANDS_DIR", commands)
    return events, commands


@pytest.fixture
def fixture_delineation(monkeypatch):
    """The real ``delineate_watershed`` tool with only the network backend replaced."""
    def fake(gauge_id=None, **kw):
        return {"data": {"area_km2": 774.2, "gauge_id": gauge_id, "gauge_name": "Piscataquis River Near Dover-Foxcroft, Maine",
                         "gauge_lat": 45.175, "gauge_lon": -69.31, "geometry_geojson": GEOJSON},
                "meta": {"tool": "delineate_watershed"}}

    monkeypatch.setattr("ai_hydro.analysis.watershed.delineate_watershed", fake)
    monkeypatch.setattr("ai_hydro.analysis.plots.plot_watershed_map", lambda **kw: None)


def _events(events_dir: Path) -> dict[str, dict]:
    return {e["id"]: e for e in (json.loads(f.read_text()) for f in events_dir.glob("*.json"))}


def _commands(commands_dir: Path) -> list[dict]:
    return [json.loads(f.read_text()) for f in commands_dir.glob("*.json")]


def _delineate(ws, chat=CHAT_A):
    res, body = call("delineate_watershed", {"gauge_id": "01031500", "workspace_dir": str(ws)}, meta=ctx(chat, ws))
    assert not res.is_error and not body.get("error"), body
    return body


# ------------------------------------------------------ MP0 1: the envelope on pushed layers

def test_watershed_and_gauge_layers_carry_the_run_id_of_their_run_row(ws, map_dirs, fixture_delineation):
    body = _delineate(ws)
    layers = _events(map_dirs[0])
    run_id = body["_run_id"]
    assert run_id
    for lid in ("watershed_01031500", "gauge_01031500"):
        meta = layers[lid]["metadata"]
        assert meta["_run_id"] == run_id                                   # non-empty and the call's id
        assert meta["study_id"] == "01031500" and meta["tool"] == "delineate_watershed"
        assert meta["chat_id"] == CHAT_A and meta["retrieved_at"]
    assert (HydroSession.load("01031500").get("_run_log") or {}).get(run_id) is not None   # the run row exists
    assert layers["watershed_01031500"]["metadata"]["source"] == "USGS NLDI"             # a tool's own source is kept


def test_the_manifest_keeps_the_envelope_of_each_layer(ws, map_dirs, fixture_delineation):
    body = _delineate(ws)
    entry = HydroSession.load("01031500").artifact_manifest["layer:watershed_01031500"]
    assert entry["run_id"] == body["_run_id"] and entry["chat_id"] == CHAT_A
    assert entry["source"] == "USGS NLDI" and entry["retrieved_at"]


def test_a_layer_pushed_before_any_row_exists_names_the_row_that_is_then_stored(ws, map_dirs):
    """The reserved id: no slot row was written, yet the layer's _run_id is the stored row's id."""
    from fastmcp import FastMCP
    import mcp.types as mcp_types

    from ai_hydro.mcp import app
    from ai_hydro.mcp.enforcement import post_run
    from ai_hydro.mcp.map_events import push_layer

    srv = FastMCP(name="probe")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())

    @srv.tool()
    def pushes_then_post_run(session_id: str) -> dict:
        push_layer("L_post", "L", GEOJSON)
        return post_run("pushes_then_post_run", session_id, {"data": {"n": 1}}, inputs={})

    @srv.tool()
    def pushes_only(session_id: str) -> dict:
        push_layer("L_plain", "L", GEOJSON)
        return {"data": {"n": 1}}

    make_study("s1", workspace=str(ws))
    handler = srv._mcp_server.request_handlers[mcp_types.CallToolRequest]
    for name in ("pushes_then_post_run", "pushes_only"):
        req = mcp_types.CallToolRequest(method="tools/call", params=mcp_types.CallToolRequestParams(
            name=name, arguments={"session_id": "s1"}))
        asyncio.run(handler(req))
    layers = _events(map_dirs[0])
    rows = HydroSession.load("s1").get("_run_log") or {}
    for lid in ("L_post", "L_plain"):
        rid = layers[lid]["metadata"]["_run_id"]
        assert rid and rid in rows, (lid, rid, list(rows))
    assert HydroSession.load("s1").artifact_manifest["layer:L_post"]["run_id"] == layers["L_post"]["metadata"]["_run_id"]


def test_a_caller_cannot_spoof_the_envelope_keys(map_dirs):
    from ai_hydro.mcp.map_events import push_layer, push_raster_layer

    push_layer("L1", "L", GEOJSON, metadata={"_run_id": "forged", "chat_id": "forged", "note": "kept"})
    push_raster_layer("R1", "R", "/tmp/x.png", [0, 0, 1, 1], metadata={"_run_id": "forged", "tool": "forged"})
    layers = _events(map_dirs[0])
    for lid in ("L1", "R1"):
        assert "forged" not in layers[lid]["metadata"].values()
    assert layers["L1"]["metadata"]["note"] == "kept"
    assert layers["R1"]["metadata"]["retrieved_at"]


def test_outside_a_recorded_call_unknown_envelope_keys_are_omitted_not_empty(map_dirs):
    from ai_hydro.mcp.map_events import push_layer

    push_layer("L1", "L", GEOJSON)
    meta = _events(map_dirs[0])["L1"]["metadata"]
    assert "_run_id" not in meta and "study_id" not in meta       # the host shows "no run record"
    assert all(v != "" for v in meta.values())


# ------------------------------------------------------ MP0 2: map_add_layer_from_run

def test_add_layer_from_run_uses_the_run_store_and_the_recorded_artifact(ws, map_dirs, fixture_delineation):
    body = _delineate(ws)
    run_id = body["_run_id"]
    # The run row lives in the SQLite store; the study JSON no longer carries the run log.
    assert "_run_log" not in json.loads((HydroSession._path("01031500", None)).read_text())
    res, out = call("map_add_layer_from_run", {"session_id": "01031500", "run_id": run_id}, meta=ctx(CHAT_B, ws))
    assert out["ok"] is True, out
    assert out["artifact_id"] == "file:watershed_01031500.geojson"
    cmd = next(c for c in _commands(map_dirs[1]) if c["type"] == "add_layer_from_run")
    assert json.loads(cmd["geojson"]) == GEOJSON
    assert cmd["provenance"]["_run_id"] == run_id and cmd["provenance"]["study_id"] == "01031500"
    assert cmd["provenance"]["tool"] == "delineate_watershed" and cmd["provenance"]["chat_id"] == CHAT_B


def test_add_layer_from_run_without_an_artifact_is_a_structured_error(ws, map_dirs):
    from ai_hydro.session import store

    make_study("s1", workspace=str(ws))
    # key_outputs hold geometry, but no artifact was recorded: nothing is read from the summary values.
    store._run_log_record("s1", "r-noart", {"tool_name": "x", "key_outputs": {"geom": GEOJSON}})
    res, out = call("map_add_layer_from_run", {"session_id": "s1", "run_id": "r-noart"}, meta=ctx(CHAT_A, ws))
    assert out["error"] is True and out["code"] == "NO_GEOMETRY_ARTIFACT"
    assert out["recovery"] and out["run_id"] == "r-noart"
    assert not _commands(map_dirs[1])


def test_add_layer_from_run_unknown_run_and_unknown_study(ws, map_dirs):
    make_study("s1", workspace=str(ws))
    _, out = call("map_add_layer_from_run", {"session_id": "s1", "run_id": "nope"}, meta=ctx(CHAT_A, ws))
    assert out["code"] == "RUN_NOT_FOUND"
    _, out = call("map_add_layer_from_run", {"session_id": "ghost", "run_id": "nope"}, meta=ctx(CHAT_A, ws))
    assert out["code"] == "STUDY_NOT_FOUND"


def test_add_layer_from_run_refuses_an_artifact_edited_after_the_run(ws, map_dirs, fixture_delineation):
    body = _delineate(ws)
    (ws / "watershed_01031500.geojson").write_text(json.dumps({**GEOJSON, "edited": True}))
    _, out = call("map_add_layer_from_run", {"session_id": "01031500", "run_id": body["_run_id"]}, meta=ctx(CHAT_A, ws))
    assert out["code"] == "ARTIFACT_MODIFIED"
    assert not _commands(map_dirs[1])


# ------------------------------------------------------ MP0 3: bind_claim_to_item and map_get_state

def _study_with_layer_and_claim(ws, map_dirs, fixture_delineation):
    _delineate(ws)
    s = HydroSession.load("01031500")
    s.claims["CLM-1"] = {"id": "CLM-1", "claim": "area is 774 km2", "status": "proposed", "confidence": 0.5}
    s.save()
    return "01031500"


def test_bind_claim_to_item_makes_claim_ids_appear_on_the_item(ws, map_dirs, fixture_delineation):
    sid = _study_with_layer_and_claim(ws, map_dirs, fixture_delineation)
    _, out = call("bind_claim_to_item", {"claim_id": "CLM-1", "item_id": "watershed_01031500", "session_id": sid},
                  meta=ctx(CHAT_A, ws))
    assert out["linked"] is True and out["already_linked"] is False
    _, state = call("map_get_state", {"scope": "chat"}, meta=ctx(CHAT_A, ws))
    items = {i["item_id"]: i for i in state["items"]}
    assert items["watershed_01031500"]["claim_ids"] == ["CLM-1"]
    assert items["gauge_01031500"]["claim_ids"] == []


def test_bind_claim_to_item_unknown_claim_and_unknown_item(ws, map_dirs, fixture_delineation):
    sid = _study_with_layer_and_claim(ws, map_dirs, fixture_delineation)
    _, out = call("bind_claim_to_item", {"claim_id": "CLM-9", "item_id": "watershed_01031500", "session_id": sid},
                  meta=ctx(CHAT_A, ws))
    assert out["error"] is True and out["code"] == "CLAIM_NOT_FOUND"
    _, out = call("bind_claim_to_item", {"claim_id": "CLM-1", "item_id": "no_such_layer", "session_id": sid},
                  meta=ctx(CHAT_A, ws))
    assert out["code"] == "ITEM_NOT_FOUND"
    assert "map_item_claim_links" not in HydroSession.load(sid).extra


def test_claim_status_is_never_stored_on_the_item(ws, map_dirs, fixture_delineation):
    sid = _study_with_layer_and_claim(ws, map_dirs, fixture_delineation)
    call("bind_claim_to_item", {"claim_id": "CLM-1", "item_id": "watershed_01031500", "session_id": sid},
         meta=ctx(CHAT_A, ws))
    s = HydroSession.load(sid)
    # What was stored is the link and nothing else.
    assert s.extra["map_item_claim_links"] == {"watershed_01031500": ["CLM-1"]}
    entry = s.artifact_manifest["layer:watershed_01031500"]
    assert "claim_ids" not in entry and "status" not in entry and "claim_status" not in entry
    # The claim's status changes later; the item, its manifest entry and the link do not move.
    s.claims["CLM-1"]["status"] = "promoted"
    s.save()
    _, state = call("map_get_state", {"scope": "chat"}, meta=ctx(CHAT_A, ws))
    item = next(i for i in state["items"] if i["item_id"] == "watershed_01031500")
    assert item["claim_ids"] == ["CLM-1"]
    assert not any("status" in k for k in item), item
    assert "promoted" not in json.dumps(item) and "proposed" not in json.dumps(item)
    s2 = HydroSession.load(sid)
    assert s2.extra["map_item_claim_links"] == {"watershed_01031500": ["CLM-1"]}
    assert "promoted" not in json.dumps(s2.artifact_manifest["layer:watershed_01031500"])


def test_binding_twice_is_idempotent(ws, map_dirs, fixture_delineation):
    sid = _study_with_layer_and_claim(ws, map_dirs, fixture_delineation)
    args = {"claim_id": "CLM-1", "item_id": "watershed_01031500", "session_id": sid}
    call("bind_claim_to_item", args, meta=ctx(CHAT_A, ws))
    _, again = call("bind_claim_to_item", args, meta=ctx(CHAT_A, ws))
    assert again["already_linked"] is True
    assert HydroSession.load(sid).extra["map_item_claim_links"] == {"watershed_01031500": ["CLM-1"]}


def test_map_get_state_scopes(tmp_path, map_dirs, fixture_delineation):
    ws1, ws2 = tmp_path / "p1", tmp_path / "p2"
    ws1.mkdir()
    ws2.mkdir()
    _delineate(ws1, CHAT_A)                                        # chat A, project p1
    # a second study in project p1 by chat B, and one in p2 by chat A
    s = make_study("other", workspace=str(ws1))
    s.artifact_manifest["layer:foreign"] = {
        "artifact_id": "layer:foreign", "kind": "workspace_artifact", "type": "map_layer", "layer_id": "foreign",
        "name": "F", "layer_type": "polygon", "study_id": "other", "run_id": "r9", "tool": "t", "chat_id": CHAT_B}
    s.save()
    s = make_study("elsewhere", workspace=str(ws2))
    s.artifact_manifest["layer:far"] = {
        "artifact_id": "layer:far", "kind": "workspace_artifact", "type": "map_layer", "layer_id": "far",
        "name": "Far", "layer_type": "polygon", "study_id": "elsewhere", "run_id": "r8", "tool": "t", "chat_id": CHAT_A}
    s.save()

    _, chat = call("map_get_state", {"scope": "chat"}, meta=ctx(CHAT_A, ws1))
    assert chat["scope"] == "chat" and chat["resolved"] is True
    assert {i["item_id"] for i in chat["items"]} == {"watershed_01031500", "gauge_01031500", "far"}
    _, proj = call("map_get_state", {"scope": "project"}, meta=ctx(CHAT_A, ws1))
    assert proj["project_key"].startswith("dir:")
    assert {i["item_id"] for i in proj["items"]} == {"watershed_01031500", "gauge_01031500", "foreign"}
    item = next(i for i in proj["items"] if i["item_id"] == "foreign")
    assert item["run_id"] == "r9" and item["chat_id"] == CHAT_B and item["tool"] == "t"


def test_map_get_state_scope_errors_and_missing_context(ws, map_dirs):
    _, bad = call("map_get_state", {"scope": "session"}, meta=ctx(CHAT_A, ws))
    assert bad["error"] is True and bad["code"] == "INVALID_SCOPE"
    _, none = call("map_get_state", {"scope": "chat"})
    assert none["resolved"] is False and none["items"] == [] and none["note"]
    assert "display_state_scope" in none                            # the host fields are still there


def test_project_key_is_dir_sha256_of_the_realpath(tmp_path):
    import hashlib
    import os

    from ai_hydro.session.project_key import project_key

    d = tmp_path / "w"
    d.mkdir()
    link = tmp_path / "link"
    link.symlink_to(d)
    assert project_key(d) == "dir:" + hashlib.sha256(os.path.realpath(d).encode()).hexdigest()
    assert project_key(link) == project_key(d)
    assert project_key(None) is None


# ------------------------------------------------------ background job: chat_id

def test_a_background_job_captures_the_chat_id_at_submit_time(ws, monkeypatch):
    """The job is a detached process: the config handed to it must carry the chat."""
    seen = {}

    def fake_start_job(**kw):
        seen["config"] = kw["config"]
        seen["seed"] = kw["status_seed"]
        return {"job_id": kw["config"]["job_id"], "artifact_dir": str(kw["artifact_dir"]),
                "log_path": "x", "started_at": "now"}

    monkeypatch.setattr("ai_hydro.mcp.tools_data_async.jobs.start_job", fake_start_job)
    res, body = call("data_fetch_background", {"variable": "precipitation", "geometry": {"lat": 45.0, "lon": -69.0},
                                               "start": "2000-01-01", "end": "2000-01-31"},
                     meta=ctx(CHAT_A, ws, study="s1"))
    assert not res.is_error and not body.get("error"), body
    from ai_hydro.session.project_key import project_key
    assert seen["config"]["context"] == {"chat_id": CHAT_A, "study_id": "s1", "project_key": project_key(ws)}
    assert str(ws) not in json.dumps(seen["config"]["context"])
    assert seen["seed"]["context"]["chat_id"] == CHAT_A


def test_the_job_runner_echoes_the_captured_context_into_every_status(tmp_path):
    from ai_hydro.mcp.runners.base import BaseJobRunner

    class Ok(BaseJobRunner):
        def run_job(self, cfg, artifact_dir):
            return {}

    class Boom(BaseJobRunner):
        def run_job(self, cfg, artifact_dir):
            raise RuntimeError("no")

    for cls in (Ok, Boom):
        d = tmp_path / cls.__name__
        d.mkdir()
        (d / "job_config.json").write_text(json.dumps({"context": {"chat_id": CHAT_A}}))
        cls().execute(d)
        status = json.loads((d / "status.json").read_text())
        assert status["context"] == {"chat_id": CHAT_A}, (cls.__name__, status)


def test_a_contextvar_does_not_reach_a_detached_job_process():
    """Why the capture is needed: ``start_job`` Popens a new interpreter, which starts with every
    ContextVar at its default."""
    from ai_hydro.mcp.app import ACTIVE_CHAT_ID

    token = ACTIVE_CHAT_ID.set(CHAT_A)
    try:
        out = subprocess.run(
            [sys.executable, "-c", "from ai_hydro.mcp.app import ACTIVE_CHAT_ID; print(ACTIVE_CHAT_ID.get())"],
            capture_output=True, text=True, timeout=120)
    finally:
        ACTIVE_CHAT_ID.reset(token)
    assert out.stdout.strip().splitlines()[-1] == "None", out


# ------------------------------------------------------ review fixes

def test_a_layer_reserved_under_one_study_is_stored_even_when_post_run_writes_another(ws, map_dirs):
    """Cross-study probe: session_id="A" pushes a layer, then post_run stores the result under "B"."""
    import mcp.types as mcp_types
    from fastmcp import FastMCP

    from ai_hydro.mcp import app
    from ai_hydro.mcp.enforcement import post_run
    from ai_hydro.mcp.map_events import push_layer

    srv = FastMCP(name="probe")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())

    @srv.tool()
    def crosses(session_id: str) -> dict:
        push_layer("L_x", "L", GEOJSON)
        return post_run("crosses", "B", {"data": {"n": 1}}, inputs={})

    make_study("A", workspace=str(ws))
    make_study("B", workspace=str(ws))
    handler = srv._mcp_server.request_handlers[mcp_types.CallToolRequest]
    asyncio.run(handler(mcp_types.CallToolRequest(method="tools/call", params=mcp_types.CallToolRequestParams(
        name="crosses", arguments={"session_id": "A"}))))
    meta = _events(map_dirs[0])["L_x"]["metadata"]
    rid, sid = meta["_run_id"], meta["study_id"]
    assert rid and (HydroSession.load(sid).get("_run_log") or {}).get(rid) is not None, (sid, rid)


def _study_with_recorded_path(ws, path_value):
    make_study("s1", workspace=str(ws))
    s = HydroSession.load("s1")
    s.artifact_manifest["file:x"] = {"artifact_id": "file:x", "kind": "workspace_artifact", "type": "workspace_file",
                                     "path": path_value, "run_id": "r1", "sha256": None}
    s.save()
    from ai_hydro.session import store
    store._run_log_record("s1", "r1", {"tool_name": "x"})


def test_add_layer_from_run_refuses_a_path_that_escapes_the_workspace(tmp_path, ws, map_dirs):
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps(GEOJSON))
    for value in ("../secret.json", str(outside)):
        _study_with_recorded_path(ws, value)
        _, out = call("map_add_layer_from_run", {"session_id": "s1", "run_id": "r1"}, meta=ctx(CHAT_A, ws))
        assert out["code"] == "ARTIFACT_OUTSIDE_WORKSPACE", (value, out)
    assert not _commands(map_dirs[1])


def test_add_layer_from_run_refuses_a_symlink_that_leaves_the_workspace(tmp_path, ws, map_dirs):
    outside = tmp_path / "secret.geojson"
    outside.write_text(json.dumps(GEOJSON))
    (ws / "link.geojson").symlink_to(outside)
    _study_with_recorded_path(ws, "link.geojson")
    _, out = call("map_add_layer_from_run", {"session_id": "s1", "run_id": "r1"}, meta=ctx(CHAT_A, ws))
    assert out["code"] == "ARTIFACT_OUTSIDE_WORKSPACE"


def test_add_layer_from_run_reports_when_no_digest_was_checked(ws, map_dirs):
    (ws / "g.geojson").write_text(json.dumps(GEOJSON))
    _study_with_recorded_path(ws, "g.geojson")
    _, out = call("map_add_layer_from_run", {"session_id": "s1", "run_id": "r1"}, meta=ctx(CHAT_A, ws))
    assert out["ok"] is True and out["hash_checked"] is False


def test_pusher_owned_metadata_keys_cannot_be_overridden(map_dirs):
    from ai_hydro.mcp.map_events import push_inundation_3d_manifest, push_raster_layer, push_tile_layer

    bad = {"raster_colormap": "forged", "raster_bounds": "forged", "tile_pyramid": "forged",
           "tile_pyramid_manifest": "forged", "presentation_tier": "forged"}
    push_raster_layer("R", "R", "/tmp/x.png", [0, 0, 1, 1], metadata=bad)
    push_tile_layer("T", "T", "/tmp/o.png", "/tmp/m.json", [0, 0, 1, 1], metadata=bad)
    push_inundation_3d_manifest("D", "D", "/tmp/m.json", [0, 0, 1, 1], metadata=bad)
    ev = _events(map_dirs[0])
    owned = {"R": ("raster_colormap", "raster_bounds"),
             "T": ("raster_colormap", "raster_bounds", "tile_pyramid", "tile_pyramid_manifest"),
             "D": ("presentation_tier",)}
    for lid, keys in owned.items():
        for k in keys:
            assert ev[lid]["metadata"][k] != "forged", (lid, k)
