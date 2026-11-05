"""Map layer ids follow the host catalog's ``layer_aliases``; claim links keep the id they were recorded with;
``map_get_state`` / ``map_list_layers`` show the model no absolute path."""
from __future__ import annotations

import copy
import json

import pytest

from binding_helpers import CHAT_A, call, ctx, make_study

from ai_hydro.mcp import map_items, map_layer_catalog
from ai_hydro.session import HydroSession


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A host catalog and map session under tmp_path; ``write(catalog, session)`` fills them."""
    cat, sess = tmp_path / "map_layer_catalog.json", tmp_path / "map_session.json"
    cmds = tmp_path / "map_commands"
    monkeypatch.setattr(map_layer_catalog, "MAP_LAYER_CATALOG_FILE", cat)
    monkeypatch.setattr(map_items, "MAP_LAYER_CATALOG_FILE", cat)
    monkeypatch.setattr("ai_hydro.mcp.tools_map._MAP_SESSION_FILE", sess)
    monkeypatch.setattr("ai_hydro.mcp.map_commands._MAP_COMMANDS_DIR", cmds)

    class H:
        catalog, session, commands = cat, sess, cmds

        @staticmethod
        def write(catalog, session=None):
            cat.write_text(json.dumps(catalog))
            if session is not None:
                sess.write_text(json.dumps(session))

        @staticmethod
        def sent():
            return [json.loads(f.read_text()) for f in cmds.glob("*.json")] if cmds.exists() else []

    return H


def _cat(ids, aliases=None, **extra):
    out = {"updated_at_ms": 1, "layer_order": list(ids), "layers": [{"id": i, "name": i} for i in ids], **extra}
    if aliases is not None:
        out["layer_aliases"] = aliases
    return out


# ------------------------------------------------------------------ resolver

def test_alias_is_followed(host):
    host.write(_cat(["keep"], {"old": "keep"}))
    hit = map_layer_catalog.resolve_catalog_layer("old")
    assert hit["canonical_id"] == "keep" and hit["alias_followed"] is True and hit["entry"]["id"] == "keep"
    assert map_layer_catalog.find_catalog_layer("old")["id"] == "keep"


def test_own_id_is_not_an_alias_follow(host):
    host.write(_cat(["keep"], {"old": "keep"}))
    hit = map_layer_catalog.resolve_catalog_layer("keep")
    assert hit["canonical_id"] == "keep" and hit["alias_followed"] is False


def test_chain_is_followed(host):
    host.write(_cat(["c"], {"a": "b", "b": "c"}))
    hit = map_layer_catalog.resolve_catalog_layer("a")
    assert hit["canonical_id"] == "c" and hit["alias_followed"] is True


def test_cycle_resolves_to_nothing(host):
    host.write(_cat(["z"], {"a": "b", "b": "a", "s": "s"}))
    for lid in ("a", "b", "s"):
        hit = map_layer_catalog.resolve_catalog_layer(lid)
        assert hit["entry"] is None and hit["canonical_id"] is None and hit["alias_followed"] is False


def test_depth_cap(host):
    chain = {f"n{i}": f"n{i + 1}" for i in range(20)}
    host.write(_cat(["n20"], chain))
    assert map_layer_catalog.resolve_catalog_layer("n0")["entry"] is None            # 20 hops: over the cap
    near = {f"n{i}": f"n{i + 1}" for i in range(map_layer_catalog.MAX_ALIAS_DEPTH)}
    host.write(_cat([f"n{map_layer_catalog.MAX_ALIAS_DEPTH}"], near))
    assert map_layer_catalog.resolve_catalog_layer("n0")["canonical_id"] == f"n{map_layer_catalog.MAX_ALIAS_DEPTH}"


def test_missing_target_resolves_to_nothing(host):
    host.write(_cat(["other"], {"old": "gone"}))
    assert map_layer_catalog.find_catalog_layer("old") is None
    out = call("map_update_layer", {"layer_id": "old", "visible": False})[1]
    assert out["ok"] is False and "Unknown layer_id 'old'" in out["message"]
    assert host.sent() == []


def test_malformed_aliases_are_ignored(host):
    host.write(_cat(["a"], {"x": 5, "y": None}))
    host.catalog.write_text(json.dumps({**_cat(["a"]), "layer_aliases": ["not", "a", "map"]}))
    assert map_layer_catalog.find_catalog_layer("a")["id"] == "a"
    assert map_layer_catalog.find_catalog_layer("x") is None


# ------------------------------------------------------------------ tools resolve through it

def test_update_and_fit_use_the_canonical_id(host):
    host.write(_cat(["keep"], {"old": "keep"}))
    _, up = call("map_update_layer", {"layer_id": "old", "visible": False})
    assert up["ok"] is True and up["layer_id"] == "keep"
    assert up["alias_followed"] is True and up["requested_layer_id"] == "old"
    _, fit = call("map_fit_layer", {"layer_id": "old"})
    assert fit["layer_id"] == "keep" and fit["alias_followed"] is True
    sent = host.sent()
    assert sent and all(json.dumps(c).count("old") == 0 for c in sent), sent


def test_remove_through_an_alias_is_refused(host):
    host.write(_cat(["keep"], {"old": "keep"}))
    _, rm = call("map_remove_layer", {"layer_id": "old"})
    assert rm["ok"] is False and rm["layer_id"] == "keep" and rm["alias_followed"] is True
    assert "'keep'" in rm["message"] and "nothing was removed" in rm["message"]
    assert host.sent() == []
    _, rm = call("map_remove_layer", {"layer_id": "keep"})
    assert rm["ok"] is True and [c.get("layer_id") for c in host.sent()] == ["keep"]


def test_plain_call_has_no_alias_fields(host):
    host.write(_cat(["keep"]))
    _, up = call("map_update_layer", {"layer_id": "keep", "visible": True})
    assert set(up) == {"ok", "layer_id", "message"}
    _, rm = call("map_remove_layer", {"layer_id": "keep"})
    assert set(rm) == {"ok", "layer_id"}


@pytest.fixture
def study(tmp_path):
    ws = tmp_path / "w"
    ws.mkdir()
    s = make_study("s1", workspace=str(ws))
    s.claims["CLM-1"] = {"id": "CLM-1", "claim": "c", "status": "proposed", "confidence": 0.5}
    s.save()
    return s, ws


def test_bind_to_alias_records_the_surviving_id(host, study):
    host.write(_cat(["keep"], {"old": "keep"}))
    _, out = call("bind_claim_to_item", {"claim_id": "CLM-1", "item_id": "old", "session_id": "s1"})
    assert out["linked"] is True and out["item_id"] == "keep"
    assert out["alias_followed"] is True and out["requested_item_id"] == "old"
    extra = HydroSession.load("s1").extra
    assert extra["map_item_claim_links"] == {"keep": ["CLM-1"]}
    assert extra["map_item_claim_link_via"] == {"keep": {"CLM-1": "old"}}          # the audit survives in the study


def test_bind_plain_response_unchanged(host, study):
    host.write(_cat(["keep"]))
    _, out = call("bind_claim_to_item", {"claim_id": "CLM-1", "item_id": "keep", "session_id": "s1"})
    assert set(out) - {"_run_id"} == {"session_id", "claim_id", "item_id", "linked", "already_linked", "message"}


def test_bind_unknown_through_missing_alias_target(host, study):
    host.write(_cat(["keep"], {"old": "gone"}))
    _, out = call("bind_claim_to_item", {"claim_id": "CLM-1", "item_id": "old", "session_id": "s1"})
    assert out["code"] == "ITEM_NOT_FOUND"


# ------------------------------------------------------------------ claim links

def test_claim_bound_to_alias_shows_on_survivor_and_stored_link_is_unchanged(host, study):
    s, ws = study
    s.extra["map_item_claim_links"] = {"old": ["CLM-1"], "keep": ["CLM-2"]}
    s.artifact_manifest["layer:keep"] = {
        "artifact_id": "layer:keep", "kind": "workspace_artifact", "type": "map_layer", "layer_id": "keep",
        "name": "K", "layer_type": "polygon", "study_id": "s1", "run_id": "r1", "tool": "t", "chat_id": CHAT_A}
    s.save()
    host.write(_cat(["keep"], {"old": "keep"}))
    before = copy.deepcopy(HydroSession.load("s1").extra["map_item_claim_links"])
    _, state = call("map_get_state", {"scope": "chat"}, meta=ctx(CHAT_A, ws))
    item = next(i for i in state["items"] if i["item_id"] == "keep")
    assert item["claim_ids"] == ["CLM-2"]                                              # direct links only
    assert item["claim_ids_via_alias"] == [{"claim_id": "CLM-1", "bound_to": "old"}]
    assert "layer_alias_of" not in item
    assert state["layer_aliases"] == {"old": "keep"}
    assert HydroSession.load("s1").extra["map_item_claim_links"] == before            # never rewritten


def test_claim_links_direct_vs_alias_and_siblings(host):
    cat = _cat(["B"], {"Y": "B", "Z": "B", "X": "Y"})
    extra = {"map_item_claim_links": {"B": ["C0"], "Y": ["CY", "C0"], "Z": ["CZ"], "X": ["CX"]}}
    assert map_items.claim_ids_for(extra, "B") == ["C0"]
    via = map_items.claim_links_via_alias(extra, "B", cat)
    assert sorted((v["claim_id"], v["bound_to"]) for v in via) == [("CX", "X"), ("CY", "Y"), ("CZ", "Z")]   # C0 is direct
    for sibling in ("Y", "Z", "X"):                                                  # merged ids never see each other
        assert map_items.claim_links_via_alias(extra, sibling, cat) == []
    assert map_items.claim_ids_for(extra, "Y") == ["CY", "C0"]
    assert map_items.claim_links_via_alias(extra, "B", _cat(["B"])) == []            # no aliases: nothing
    assert map_items.claim_links_via_alias(extra, "B", None) == []
    assert map_items.claim_links_via_alias(None, "B", cat) == []


def test_a_forged_alias_cannot_put_a_claim_in_claim_ids(host, study):
    s, ws = study
    s.extra["map_item_claim_links"] = {"victim_old": ["CLM-1"]}
    s.artifact_manifest["layer:target"] = {
        "artifact_id": "layer:target", "kind": "workspace_artifact", "type": "map_layer", "layer_id": "target",
        "name": "T", "layer_type": "polygon", "study_id": "s1", "run_id": "r1", "tool": "t", "chat_id": CHAT_A}
    s.save()
    host.write(_cat(["target"], {"victim_old": "target"}))                            # host data says so; the link is not direct
    _, state = call("map_get_state", {"scope": "chat"}, meta=ctx(CHAT_A, ws))
    item = state["items"][0]
    assert item["claim_ids"] == [] and item["claim_ids_via_alias"] == [{"claim_id": "CLM-1", "bound_to": "victim_old"}]


def test_item_recorded_under_a_merged_id_is_marked(host, study):
    s, ws = study
    s.artifact_manifest["layer:old"] = {
        "artifact_id": "layer:old", "kind": "workspace_artifact", "type": "map_layer", "layer_id": "old",
        "name": "O", "layer_type": "polygon", "study_id": "s1", "run_id": "r1", "tool": "t", "chat_id": CHAT_A}
    s.save()
    host.write(_cat(["keep"], {"old": "keep"}))
    _, state = call("map_get_state", {"scope": "chat"}, meta=ctx(CHAT_A, ws))
    item = next(i for i in state["items"] if i["item_id"] == "old")
    assert item["in_host_catalog"] is True and item["layer_alias_of"] == "keep"


def test_no_aliases_catalog_items_unchanged(host, study):
    s, ws = study
    s.artifact_manifest["layer:keep"] = {
        "artifact_id": "layer:keep", "kind": "workspace_artifact", "type": "map_layer", "layer_id": "keep",
        "name": "K", "layer_type": "polygon", "study_id": "s1", "run_id": "r1", "tool": "t", "chat_id": CHAT_A}
    s.extra["map_item_claim_links"] = {"keep": ["CLM-1"]}
    s.save()
    host.write(_cat(["keep"]))
    _, state = call("map_get_state", {"scope": "chat"}, meta=ctx(CHAT_A, ws))
    assert "layer_aliases" not in state
    assert state["items"][0]["claim_ids"] == ["CLM-1"] and "layer_alias_of" not in state["items"][0]
    assert "claim_ids_via_alias" not in state["items"][0]
    _, lst = call("map_list_layers")
    assert "layer_aliases" not in lst


# ------------------------------------------------------------------ paths

def _entry(root, outside):
    return {
        "id": "L", "name": "L",
        "workspace_path": "vectors/L.geojson",
        "geojson_cache_path": f"{outside}/cache/L.geojson",
        "raster_path": f"{root}/rasters/L.png",
        "inundation_3d_manifest_path": f"{root}/m.json",
        "source_path": f"{root}/src/L.geojson",
        "workspace_folder": root,
        "provenance_path": f"{outside}/prov.json",
        "display_geojson_path": f"{root}/d.geojson",
        "source_display_path": f"file://{root}/s.geojson",
        "source_uri": "https://example.org/data.geojson",
        "metadata_snapshot": {
            "workspace_path": "vectors/L.geojson", "raster_path": f"{root}/rasters/L.png",
            "source_uri": f"{outside}/x.geojson", "workspace_folder": root, "note": f"{root}/kept-verbatim",
        },
    }


def test_catalog_paths_relative_or_marked(host, tmp_path):
    root, outside = str(tmp_path / "ws"), "/Users/someone/private"
    cat = _cat([], {})
    cat["layers"] = [_entry(root, outside)]
    cat["layer_order"] = ["L"]
    host.write(cat, {"workspaceRoot": root,
                     "activeRoi": {"id": "r", "name": "n", "workspacePath": f"{root}/roi.geojson", "geojson": {}}})
    for tool in ("map_list_layers", "map_get_state"):
        _, out = call(tool)
        layer = out["layers"][0]
        assert layer["workspace_path"] == "vectors/L.geojson"
        assert layer["geojson_cache_path"] == "<outside-workspace>"
        assert layer["raster_path"] == "rasters/L.png"
        assert layer["inundation_3d_manifest_path"] == "m.json"
        assert layer["source_path"] == "src/L.geojson"
        assert layer["workspace_folder"] == "."
        assert layer["provenance_path"] == "<outside-workspace>"
        assert layer["display_geojson_path"] == "d.geojson"
        assert layer["source_display_path"] == "s.geojson"
        assert layer["source_uri"] == "https://example.org/data.geojson"
        snap = layer["metadata_snapshot"]
        assert snap["raster_path"] == "rasters/L.png" and snap["source_uri"] == "<outside-workspace>"
        assert snap["workspace_folder"] == "." and snap["note"].endswith("kept-verbatim")
        snap.pop("note")                                                  # not a path field: kept as given
        assert root not in json.dumps(out) and outside not in json.dumps(out)
    _, state = call("map_get_state")
    assert state["workspace_root"] == "." and state["active_roi"]["workspace_path"] == "roi.geojson"


def test_no_workspace_known_marks_every_absolute_path(host):
    cat = _cat([])
    cat["layers"] = [{"id": "L", "geojson_cache_path": "/Users/x/c.geojson", "workspace_path": "rel.geojson"}]
    host.write(cat)
    _, out = call("map_list_layers")
    assert out["layers"][0]["geojson_cache_path"] == "<outside-workspace>"
    assert out["layers"][0]["workspace_path"] == "rel.geojson"


def test_internal_readers_still_see_absolute_paths(host, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    cache = tmp_path / "cache.geojson"
    cache.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    cat = _cat([])
    cat["layers"] = [{"id": "L", "geojson_cache_path": str(cache)}]
    host.write(cat, {"workspaceRoot": str(root)})
    call("map_list_layers")                                                   # the model view is a copy
    entry = map_layer_catalog.find_catalog_layer("L")
    assert entry["geojson_cache_path"] == str(cache)
    assert map_layer_catalog.load_geojson_for_layer(entry, str(root))["type"] == "FeatureCollection"
    assert json.loads(host.catalog.read_text())["layers"][0]["geojson_cache_path"] == str(cache)   # file untouched


def test_graduated_symbology_still_reads_the_layer_after_the_model_view(host, tmp_path):
    root = tmp_path / "ws"
    (root / "v").mkdir(parents=True)
    (root / "v" / "L.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"q": float(i)}, "geometry": None} for i in range(6)]}))
    cat = _cat([])
    cat["layers"] = [{"id": "L", "workspace_path": "v/L.geojson"}]
    host.write(cat, {"workspaceRoot": str(root)})
    _, out = call("map_apply_symbology", {"layer_id": "L", "attribute": "q"})
    assert out["ok"] is True, out


def test_safe_path_unit():
    from ai_hydro.mcp.map_model_view import OUTSIDE_WORKSPACE, safe_path

    assert safe_path("/w/a/b.json", "/w") == "a/b.json"
    assert safe_path("/w", "/w") == "."
    assert safe_path("/wx/a", "/w") == OUTSIDE_WORKSPACE                     # a prefix of the name is not inside
    assert safe_path("/w/../etc/passwd", "/w") == OUTSIDE_WORKSPACE
    assert safe_path("rel/p", "/w") == "rel/p" and safe_path("https://h/x", "/w") == "https://h/x"
    assert safe_path("", "/w") == "" and safe_path(None, "/w") is None and safe_path(3, "/w") == 3
    assert safe_path("C:\\Users\\me\\ws\\sub\\a.json", "C:\\Users\\me\\ws") == "sub/a.json"
    assert safe_path("C:\\Users\\other\\a.json", "C:\\Users\\me\\ws") == OUTSIDE_WORKSPACE
    assert safe_path("/w/a", None) == OUTSIDE_WORKSPACE


def test_recent_events_show_no_absolute_paths(host, tmp_path, monkeypatch):
    root = str(tmp_path / "ws")
    out_dir = tmp_path / "outbound"
    out_dir.mkdir()
    (out_dir / "1.json").write_text(json.dumps({
        "type": "layer_added", "id": "L", "geojson_cache_path": "/Users/someone/cache.geojson",
        "metadata": {"raster_path": f"{root}/r/L.png", "workspace_path": "v/L.geojson", "note": "kept"},
        "layers": [{"source_path": f"{root}/s.geojson", "name": "n"}],
        "geojson": {"type": "Point", "coordinates": [1, 2]},
    }))
    monkeypatch.setattr("ai_hydro.mcp.tools_map._MAP_EVENTS_OUTBOUND", out_dir)
    host.write(_cat([]), {"workspaceRoot": root})
    _, state = call("map_get_state")
    ev = state["recent_events"][0]
    assert ev["geojson_cache_path"] == "<outside-workspace>"
    assert ev["metadata"] == {"raster_path": "r/L.png", "workspace_path": "v/L.geojson", "note": "kept"}
    assert ev["layers"][0] == {"source_path": "s.geojson", "name": "n"}
    assert ev["geojson"] == {"type": "Point", "coordinates": [1, 2]}
    assert "/Users/someone" not in json.dumps(state) and root not in json.dumps(
        {k: v for k, v in state.items() if k != "host_workspace_ownership"})


def test_save_roi_does_not_return_an_absolute_path(host, tmp_path):
    ws = tmp_path / "w2"
    ws.mkdir()
    make_study("s2", workspace=str(ws))
    host.write(_cat([]), {"workspaceRoot": str(ws), "activeRoi": {"name": "n", "geojson": {
        "type": "Point", "coordinates": [0, 0]}}})
    _, out = call("map_save_roi", {"session_id": "s2", "name": "basin"})
    assert out["ok"] is True and not str(out["saved_to"]).startswith("/"), out
    assert out["saved_to"] == "roi/basin.geojson" and (ws / "roi" / "basin.geojson").exists()


# ------------------------------------------------------------------ scrubbing probes

def test_safe_tree_probes():
    from ai_hydro.mcp.map_model_view import OUTSIDE_WORKSPACE as OUT, safe_tree

    root = "/Users/me/ws"
    probe = {
        "workspacePath": f"{root}/a.geojson",                                         # camelCase
        "geojson_cache_path": ["/Users/me/other/c.json", {"deep": "/opt/x/y/z"}],       # list and dict under a path key
        "uri": "file:///Users/me/ws/v/L.geojson",
        "url": "vscode-file://vscode-app/Users/me/secret/L.geojson",
        "source_uri": "https://example.org/d.geojson",
        "source_path": "\\Users\\me\\x",                                            # backslash-rooted
        "raster_path": "..\\..\\etc\\passwd",
        "workspace_folder": "../../outside",
        "file": "C:\\Users\\me\\ws\\q.json",
        "nested": {"note": "see /Users/me/secret/notes.txt for details", "list": ["/home/bob/a/b"]},
    }
    out = safe_tree(probe, root)
    assert out["workspacePath"] == "a.geojson"
    assert out["geojson_cache_path"] == [OUT, {"deep": OUT}]
    assert out["uri"] == "v/L.geojson" and out["url"] == OUT
    assert out["source_uri"] == "https://example.org/d.geojson"
    assert out["source_path"] == OUT and out["raster_path"] == OUT and out["workspace_folder"] == OUT
    assert out["file"] == OUT
    assert "/Users/me/secret" not in json.dumps(out) and "/home/bob" not in json.dumps(out)    # free text scrubbed
    assert probe["nested"]["note"].startswith("see /Users")                              # input untouched


def test_error_and_message_text_is_scrubbed(host, tmp_path):
    from ai_hydro.mcp.tools_map import _scrub_roots

    msg = _scrub_roots("Selected ROI does not exist: /Users/me/ws/roi/a.json (and /home/bob/q/r.txt)", "/Users/me/ws")
    assert "/Users/me" not in msg and "/home/bob" not in msg and "<workspace>" in msg
    host.write(_cat(["a"]))
    ws = tmp_path / "w3"
    ws.mkdir()
    make_study("s3", workspace=str(ws))
    _, out = call("map_set_working_geometry", {"session_id": "s3", "workspace_path": "missing/x.geojson"})
    assert out["ok"] is False and str(tmp_path) not in out["message"]
