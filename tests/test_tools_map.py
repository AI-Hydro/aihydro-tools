"""Tests for map orchestration MCP tools and ROI resolution."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


_POLYGON = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    from ai_hydro.session import store

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(store, "_SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)
    return sessions_dir


def _save_session(session_id: str, workspace: Path | None, *, watershed: dict | None = None):
    from ai_hydro.session import HydroSession

    session = HydroSession(session_id)
    session.workspace_dir = str(workspace) if workspace else None
    if watershed is not None:
        session.watershed = {"data": {"geometry_geojson": watershed}}
    session.save()
    return session


def _write_host_session(home: Path, payload: dict) -> Path:
    state_dir = home / ".aihydro"
    state_dir.mkdir(exist_ok=True)
    path = state_dir / "map_session.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_write_map_command_creates_json(tmp_path, monkeypatch):
    from ai_hydro.mcp import map_commands

    cmd_dir = tmp_path / "map_commands"
    monkeypatch.setattr(map_commands, "_MAP_COMMANDS_DIR", cmd_dir)

    ok = map_commands.push_set_roi(
        geojson={"type": "FeatureCollection", "features": []},
        name="Test ROI",
    )
    assert ok is True
    files = list(cmd_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["type"] == "set_roi"
    assert payload["roi"]["name"] == "Test ROI"


def test_resolve_active_roi_priority_workspace(tmp_path, monkeypatch):
    from unittest.mock import patch

    from ai_hydro.mcp.helpers import _resolve_active_roi_geojson
    from ai_hydro.session import HydroSession

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    ws = tmp_path / "workspace"
    ws.mkdir()
    roi_dir = ws / "roi"
    roi_dir.mkdir()
    rel = "roi/user_basin.geojson"
    (ws / rel).write_text(
        json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}),
        encoding="utf-8",
    )
    (roi_dir / "active.json").write_text(
        json.dumps({"path": rel, "name": "User basin"}),
        encoding="utf-8",
    )

    aihydro = tmp_path / ".aihydro"
    aihydro.mkdir()
    (aihydro / "map_session.json").write_text(
        json.dumps(
            {
                "activeRoi": {
                    "geojson": json.dumps({"type": "Point", "coordinates": [9, 9]}),
                    "name": "Map session ROI",
                }
            }
        ),
        encoding="utf-8",
    )

    session_id = "test-map-roi-priority"
    with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path / "sessions"), \
         patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
        (tmp_path / "sessions").mkdir(exist_ok=True)
        session = HydroSession(session_id)
        session.workspace_dir = str(ws)
        session.save()
        geojson, source = _resolve_active_roi_geojson(session_id)
    assert source == "workspace_roi"
    assert geojson["type"] == "Polygon"


def test_resolve_host_roi_for_matching_normalized_workspace(tmp_path, monkeypatch, isolated_sessions):
    from ai_hydro.mcp.helpers import _resolve_active_roi_geojson

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    _save_session("matching-host", workspace, watershed=_POLYGON)
    _write_host_session(
        tmp_path,
        {
            "workspaceRoot": str(alias),
            "activeRoi": {
                "geojson": json.dumps({"type": "Point", "coordinates": [9, 9]}),
            },
        },
    )

    geojson, source = _resolve_active_roi_geojson("matching-host")

    assert source == "map_session"
    assert geojson == {"type": "Point", "coordinates": [9, 9]}


@pytest.mark.parametrize("host_workspace", [None, "relative/workspace"])
def test_resolve_host_roi_with_unknown_ownership_falls_back(
    tmp_path,
    monkeypatch,
    isolated_sessions,
    host_workspace,
):
    from ai_hydro.mcp.helpers import _resolve_active_roi_geojson

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _save_session("unowned-host", workspace, watershed=_POLYGON)
    payload = {
        "activeRoi": {
            "geojson": json.dumps({"type": "Point", "coordinates": [9, 9]}),
        }
    }
    if host_workspace is not None:
        payload["workspaceRoot"] = host_workspace
    _write_host_session(tmp_path, payload)

    geojson, source = _resolve_active_roi_geojson("unowned-host")

    assert source == "session_watershed"
    assert geojson == _POLYGON


def test_resolve_foreign_host_roi_falls_back_to_requested_session(tmp_path, monkeypatch, isolated_sessions):
    from ai_hydro.mcp.helpers import _resolve_active_roi_geojson

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = tmp_path / "requested-workspace"
    foreign_workspace = tmp_path / "foreign-workspace"
    workspace.mkdir()
    foreign_workspace.mkdir()
    _save_session("foreign-host", workspace, watershed=_POLYGON)
    _write_host_session(
        tmp_path,
        {
            "workspaceRoot": str(foreign_workspace),
            "activeRoi": {
                "geojson": json.dumps({"type": "Point", "coordinates": [9, 9]}),
            },
        },
    )

    geojson, source = _resolve_active_roi_geojson("foreign-host")

    assert source == "session_watershed"
    assert geojson == _POLYGON


def test_resolve_missing_selected_working_geometry_fails(tmp_path, monkeypatch, isolated_sessions):
    from ai_hydro.mcp.helpers import ActiveRoiResolutionError, _resolve_active_roi_geojson

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _save_session("missing-working", workspace, watershed=_POLYGON)
    session.working_geometry_path = str(tmp_path / "outside" / "missing.geojson")
    session.save()

    with pytest.raises(ActiveRoiResolutionError, match="working geometry does not exist"):
        _resolve_active_roi_geojson("missing-working")


def test_resolve_malformed_selected_workspace_roi_fails(tmp_path, monkeypatch, isolated_sessions):
    from ai_hydro.mcp.helpers import ActiveRoiResolutionError, _resolve_active_roi_geojson

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    roi_dir = workspace / "roi"
    roi_dir.mkdir(parents=True)
    selected = tmp_path / "outside-selected.geojson"
    selected.write_text(json.dumps({"kind": "not-geojson"}), encoding="utf-8")
    (roi_dir / "active.json").write_text(json.dumps({"path": str(selected)}), encoding="utf-8")
    _save_session("malformed-pointer-target", workspace, watershed=_POLYGON)

    with pytest.raises(ActiveRoiResolutionError, match="is not a GeoJSON"):
        _resolve_active_roi_geojson("malformed-pointer-target")


def test_resolve_missing_selected_workspace_roi_fails(tmp_path, monkeypatch, isolated_sessions):
    from ai_hydro.mcp.helpers import ActiveRoiResolutionError, _resolve_active_roi_geojson

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    roi_dir = workspace / "roi"
    roi_dir.mkdir(parents=True)
    (roi_dir / "active.json").write_text(
        json.dumps({"path": str(tmp_path / "missing-outside.geojson")}),
        encoding="utf-8",
    )
    _save_session("missing-pointer-target", workspace, watershed=_POLYGON)

    with pytest.raises(ActiveRoiResolutionError, match="workspace ROI does not exist"):
        _resolve_active_roi_geojson("missing-pointer-target")


def test_resolve_unreadable_selected_workspace_roi_fails(tmp_path, monkeypatch, isolated_sessions):
    from ai_hydro.mcp.helpers import ActiveRoiResolutionError, _resolve_active_roi_geojson

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    roi_dir = workspace / "roi"
    roi_dir.mkdir(parents=True)
    selected = tmp_path / "selected.geojson"
    selected.write_text(json.dumps(_POLYGON), encoding="utf-8")
    (roi_dir / "active.json").write_text(json.dumps({"path": str(selected)}), encoding="utf-8")
    _save_session("unreadable-pointer-target", workspace, watershed=_POLYGON)
    original_open = Path.open

    def deny_selected(path, *args, **kwargs):
        if path == selected:
            raise PermissionError("denied by test")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_selected)

    with pytest.raises(ActiveRoiResolutionError, match="workspace ROI could not be read"):
        _resolve_active_roi_geojson("unreadable-pointer-target")


def test_resolve_matching_malformed_host_roi_fails(tmp_path, monkeypatch, isolated_sessions):
    from ai_hydro.mcp.helpers import ActiveRoiResolutionError, _resolve_active_roi_geojson

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _save_session("malformed-host", workspace, watershed=_POLYGON)
    _write_host_session(
        tmp_path,
        {"workspaceRoot": str(workspace), "activeRoi": "not-an-roi-record"},
    )

    with pytest.raises(ActiveRoiResolutionError, match="host ROI record is malformed"):
        _resolve_active_roi_geojson("malformed-host")


def test_resolve_absent_explicit_selection_uses_session_watershed(tmp_path, monkeypatch, isolated_sessions):
    from ai_hydro.mcp.helpers import _resolve_active_roi_geojson

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _save_session("watershed-fallback", workspace, watershed=_POLYGON)

    geojson, source = _resolve_active_roi_geojson("watershed-fallback")

    assert source == "session_watershed"
    assert geojson == _POLYGON


def test_map_get_state_reads_session_file(tmp_path, monkeypatch):
    from ai_hydro.mcp import map_layer_catalog, tools_map

    session_file = tmp_path / "map_session.json"
    session_file.write_text(
        json.dumps(
            {
                "activeRoi": {
                    "id": "r1",
                    "name": "Basin A",
                    "source": "agent",
                    "geojson": "{}",
                    "areaHa": 500,
                },
                "workspaceRoot": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tools_map, "_MAP_SESSION_FILE", session_file)
    monkeypatch.setattr(tools_map, "_MAP_EVENTS_OUTBOUND", tmp_path / "events")
    monkeypatch.setattr(map_layer_catalog, "MAP_LAYER_CATALOG_FILE", tmp_path / "catalog.json")

    state = tools_map.map_get_state(session_id=None, event_limit=5)
    assert state["active_roi"]["name"] == "Basin A"
    assert state["active_roi"]["area_ha"] == 500
    assert state["display_state_scope"] == "global_host"
    assert state["active_roi_scope"] == "global_host"
    assert state["host_workspace_ownership"]["status"] == "not_requested"


def test_map_get_state_marks_foreign_host_state_global(tmp_path, monkeypatch, isolated_sessions):
    from ai_hydro.mcp import map_layer_catalog, tools_map

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    requested_workspace = tmp_path / "requested"
    foreign_workspace = tmp_path / "foreign"
    requested_workspace.mkdir()
    foreign_workspace.mkdir()
    _save_session("state-foreign", requested_workspace, watershed=_POLYGON)
    session_file = _write_host_session(
        tmp_path,
        {
            "workspaceRoot": str(foreign_workspace),
            "activeRoi": {
                "name": "Foreign display ROI",
                "geojson": json.dumps({"type": "Point", "coordinates": [9, 9]}),
            },
        },
    )
    monkeypatch.setattr(tools_map, "_MAP_SESSION_FILE", session_file)
    monkeypatch.setattr(tools_map, "_MAP_EVENTS_OUTBOUND", tmp_path / "events")
    monkeypatch.setattr(map_layer_catalog, "MAP_LAYER_CATALOG_FILE", tmp_path / "catalog.json")

    state = tools_map.map_get_state(session_id="state-foreign")

    assert state["active_roi"]["name"] == "Foreign display ROI"
    assert state["active_roi_scope"] == "global_host"
    assert state["host_workspace_ownership"]["status"] == "conflicting"
    assert state["host_workspace_ownership"]["active_roi_eligible_for_session"] is False
    assert state["resolved_roi_for_session"] == {
        "scope": "requested_session",
        "session_id": "state-foreign",
        "source": "session_watershed",
        "geometry_type": "Polygon",
    }


def test_map_get_state_marks_requested_session_without_workspace_unknown(
    tmp_path,
    monkeypatch,
    isolated_sessions,
):
    from ai_hydro.mcp import map_layer_catalog, tools_map

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _save_session("state-unowned", None, watershed=_POLYGON)
    session_file = _write_host_session(
        tmp_path,
        {
            "workspaceRoot": str(tmp_path / "host-workspace"),
            "activeRoi": {
                "name": "Unowned display ROI",
                "geojson": json.dumps({"type": "Point", "coordinates": [9, 9]}),
            },
        },
    )
    monkeypatch.setattr(tools_map, "_MAP_SESSION_FILE", session_file)
    monkeypatch.setattr(tools_map, "_MAP_EVENTS_OUTBOUND", tmp_path / "events")
    monkeypatch.setattr(map_layer_catalog, "MAP_LAYER_CATALOG_FILE", tmp_path / "catalog.json")

    state = tools_map.map_get_state(session_id="state-unowned")

    assert state["host_workspace_ownership"]["status"] == "unknown"
    assert state["host_workspace_ownership"]["active_roi_eligible_for_session"] is False
    assert state["resolved_roi_for_session"]["source"] == "session_watershed"


def test_push_update_layer_command(tmp_path, monkeypatch):
    from ai_hydro.mcp import map_commands

    cmd_dir = tmp_path / "map_commands"
    monkeypatch.setattr(map_commands, "_MAP_COMMANDS_DIR", cmd_dir)

    ok = map_commands.push_update_layer(
        layer_id="file_vectors_basin",
        style={"fillColor": "#FF5733", "fillOpacity": 0.5},
        metadata={"display_name": "Styled basin"},
    )
    assert ok is True
    payload = json.loads(list(cmd_dir.glob("*.json"))[0].read_text())
    assert payload["type"] == "update_layer"
    assert payload["layer_id"] == "file_vectors_basin"
    assert payload["style"]["fillColor"] == "#FF5733"


def test_map_get_state_includes_layer_catalog(tmp_path, monkeypatch):
    from ai_hydro.mcp import map_layer_catalog, tools_map

    monkeypatch.setattr(tools_map, "_MAP_SESSION_FILE", tmp_path / "map_session.json")
    (tmp_path / "map_session.json").write_text("{}", encoding="utf-8")
    catalog_file = tmp_path / "map_layer_catalog.json"
    catalog_file.write_text(
        json.dumps(
            {
                "layer_order": ["l1"],
                "layers": [
                    {
                        "id": "l1",
                        "name": "Test",
                        "layer_type": "polygon",
                        "visible": True,
                        "symbology_mode": "basic",
                        "numeric_attributes": [],
                        "feature_count": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(map_layer_catalog, "MAP_LAYER_CATALOG_FILE", catalog_file)
    monkeypatch.setattr(tools_map, "_MAP_EVENTS_OUTBOUND", tmp_path / "events")

    state = tools_map.map_get_state()
    assert state["layers"][0]["id"] == "l1"
    assert state["layer_catalog_scope"] == "global_host"
    assert state["recent_events_scope"] == "global_host"


def test_compute_graduated_metadata():
    from ai_hydro.mcp.map_layer_catalog import compute_graduated_metadata

    geojson = {
        "type": "FeatureCollection",
        "features": [
            {"properties": {"twi": 1.0}},
            {"properties": {"twi": 5.0}},
            {"properties": {"twi": 10.0}},
        ],
    }
    meta = compute_graduated_metadata(geojson, attribute="twi", num_classes=3)
    assert meta["graduated_attr"] == "twi"
    breaks = json.loads(meta["graduated_breaks"])
    assert len(breaks) >= 2
    colors = json.loads(meta["graduated_colors"])
    assert len(colors) >= 2
