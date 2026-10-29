"""Project registry v2 (T-P1): prj_ ids, host operations, resources, the confirmed-default rule (B4),
read-side duplicate grouping (D8), M3/M4 links, root_dir_aliases, and the atomic-write + revision check.

Every test runs under the per-test scratch AIHYDRO_HOME of tests/conftest.py; nothing reads ~/.aihydro.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from binding_helpers import CHAT_A, CHAT_B, call, ctx, make_study, read_resource

from ai_hydro.mcp.helpers import SessionResolutionError, _resolve_session_with_rule
from ai_hydro.registry.ids import PROJECT_ID_RE, STUDY_ID_RE, mint_project_id, mint_study_id
from ai_hydro.registry.paths import aihydro_home
from ai_hydro.session.chat_binding import get_binding_store
from ai_hydro.session.project_registry import ProjectUnreadable, dir_key, get_project_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
GAUGE = dict(site_id="01031500", site_name="Piscataquis River", site_type="usgs_gauge")


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "try"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _no_eval_or_fallback(monkeypatch):
    monkeypatch.delenv("AIHYDRO_RECENT_SESSION_FALLBACK", raising=False)
    monkeypatch.delenv("AIHYDRO_EVAL_NONCE", raising=False)


@pytest.fixture
def reg():
    return get_project_registry()


def _ensure(ws):
    res, body = call("ensure_project", {"root_dir": str(ws)})
    assert not res.is_error and not body.get("error"), body
    return body


# ---------------------------------------------------------------- ids and ensure_project

def test_ids_are_minted_prj_ulids_and_study_ids_are_st_ulids():
    a, b = mint_project_id(), mint_project_id()
    assert PROJECT_ID_RE.match(a) and a != b
    assert STUDY_ID_RE.match(mint_study_id())
    from ai_hydro.mcp.helpers import _normalize_session_id

    assert STUDY_ID_RE.match(_normalize_session_id(None))          # a study created with no id is minted
    assert _normalize_session_id("01031500") == "01031500"          # a named study keeps its name


def test_ensure_project_creates_once_and_is_idempotent(ws):
    first = _ensure(ws)
    again = _ensure(ws)
    assert first["status"] == "created" and again["status"] == "found"
    assert PROJECT_ID_RE.match(first["project_id"]) and first["project_id"] == again["project_id"]
    assert first["schema"] == "aihydro.project/2" and first["revision"] == 1 and again["revision"] == 1
    assert first["title"] == "try" and first["title_source"] == "fallback"       # the humanised folder name
    assert (aihydro_home() / "projects" / first["project_id"] / "project.json").is_file()


def test_ensure_project_refuses_a_relative_or_missing_directory(ws):
    for bad in ("relative/dir", str(ws / "nope"), ""):
        res, body = call("ensure_project", {"root_dir": bad})
        assert body["code"] == "ROOT_DIR_INVALID", bad
    assert get_project_registry().list_all() == []


def test_lookup_uses_the_realpath(ws, tmp_path):
    link = tmp_path / "alias_to_try"
    link.symlink_to(ws, target_is_directory=True)
    body = _ensure(ws)
    assert _ensure(link)["project_id"] == body["project_id"]
    assert get_project_registry().find_by_dir(link)["project_id"] == body["project_id"]


@pytest.mark.skipif(sys.platform not in ("darwin", "win32"), reason="case-insensitive filesystems only")
def test_lookup_is_case_normalised_where_the_filesystem_is(ws):
    body = _ensure(ws)
    flipped = str(ws.parent / ws.name.upper())
    assert dir_key(flipped) == dir_key(ws)
    assert get_project_registry().find_by_dir(flipped)["project_id"] == body["project_id"]


def test_legacy_slug_projects_are_neither_listed_nor_clobbered(ws):
    from ai_hydro.session.project import ProjectSession

    legacy = ProjectSession("old_slug")
    legacy.save()
    body = _ensure(ws)
    assert [r["project_id"] for r in get_project_registry().list_all()] == [body["project_id"]]
    assert ProjectSession.list_all() == ["old_slug"]                # prj_ directories are not slug projects
    with pytest.raises(ValueError, match="reserved"):
        ProjectSession.load(body["project_id"])


# ---------------------------------------------------------------- M3/M4 migration

def test_creation_links_the_studies_of_the_folder_with_a_suggested_unconfirmed_default(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    make_study("01031500", workspace=str(ws), **GAUGE)
    make_study("piscataquis-river-01031500", workspace=str(ws), **GAUGE)       # the duplicate of the live `try` folder
    make_study("basin_24p2_75p7", workspace=str(ws))
    make_study("india_basin", workspace=str(ws), outlet=(24.2, 75.75))
    make_study("elsewhere", workspace=str(other))                              # another folder: not linked
    make_study("test-promotion", workspace=str(ws), archived=True)             # hidden: not linked
    body = _ensure(ws)
    ids = {s["study_id"] for s in body["studies"]}
    assert ids == {"01031500", "piscataquis-river-01031500", "basin_24p2_75p7", "india_basin"}
    assert {s["added_by"] for s in body["studies"]} == {"migration"}
    assert body["default_study"]["confirmed"] is False                         # shown as suggested (D3)
    assert body["default_study"]["study_id"] in ids


def test_a_second_folder_gets_its_own_project(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    make_study("a", workspace=str(ws))
    make_study("b", workspace=str(other))
    pa, pb = _ensure(ws), _ensure(other)
    assert pa["project_id"] != pb["project_id"]
    assert [s["study_id"] for s in pb["studies"]] == ["b"]


# ---------------------------------------------------------------- resources

def test_resources_by_dir_and_by_id(ws):
    make_study("01031500", workspace=str(ws), **GAUGE)
    miss = read_resource(f"aihydro://project/by-dir?path={ws}")
    assert miss["found"] is False                                   # reading never creates
    assert get_project_registry().list_all() == []
    body = _ensure(ws)
    hit = read_resource(f"aihydro://project/by-dir?path={ws}")
    assert hit["found"] is True and hit["project_id"] == body["project_id"]
    rec = read_resource(f"aihydro://project/{body['project_id']}")
    assert rec["found"] is True and rec["revision"] == body["revision"]
    assert rec["studies"][0]["label"] == "Piscataquis River (USGS 01031500)"
    assert read_resource("aihydro://project/prj_00000000000000000000000000")["found"] is False


def test_resources_answer_fast_for_50_studies_without_loading_a_session(ws, monkeypatch):
    for i in range(50):
        make_study(f"basin_{i:02d}", workspace=str(ws), site_name=f"Gauge {i}", site_id=f"{i:08d}")
    from ai_hydro.session import study_index

    study_index._CACHE.clear()                                      # cold: every file parsed once
    body = _ensure(ws)

    def boom(*a, **k):
        raise AssertionError("a resource must not load a full session")

    monkeypatch.setattr("ai_hydro.session.store.HydroSession.load", boom)
    t0 = time.perf_counter()
    rec = read_resource(f"aihydro://project/{body['project_id']}")
    by_dir = read_resource(f"aihydro://project/by-dir?path={ws}")
    elapsed = time.perf_counter() - t0
    assert rec["n_studies"] == 50 and by_dir["n_studies"] == 50
    assert elapsed < 0.2 * 2, f"two resource reads took {elapsed:.3f}s"          # < 200 ms each


def test_labels_are_never_a_bare_id(ws):
    make_study("01031500", workspace=str(ws))                                   # nothing but an id
    make_study("basin_24p2_75p7", workspace=str(ws), outlet=(24.2, 75.75))
    body = _ensure(ws)
    for s in body["studies"]:
        assert s["label"] != s["study_id"]
    labels = {s["study_id"]: s["label"] for s in body["studies"]}
    assert labels["01031500"] == "01031500 (unnamed study)"
    assert labels["basin_24p2_75p7"].startswith("Basin at 24.20")
    assert body["default_study"]["label"] != body["default_study"]["study_id"]


def test_a_project_resource_for_an_unknown_project_is_not_found(reg):
    assert reg.get("prj_01M62NJQ6QJD650KZQH15SYM95") is None


# ---------------------------------------------------------------- D8 alias grouping (read side only)

def test_duplicate_studies_are_alias_grouped_never_merged(ws):
    make_study("01031500", workspace=str(ws), **GAUGE)
    make_study("piscataquis-river-01031500", workspace=str(ws), **GAUGE)
    make_study("basin_24p2_75p7", workspace=str(ws), outlet=(24.2, 75.75))
    files_before = {p.name: p.read_bytes() for p in (aihydro_home() / "sessions").glob("*.json")}
    body = _ensure(ws)
    (group,) = body["duplicate_groups"]
    assert set(group["study_ids"]) == {"01031500", "piscataquis-river-01031500"}
    assert group["n_studies"] == 2 and group["merged"] is False
    assert group["summary"] == "2 studies for 01031500"
    grouped = {s["study_id"] for s in body["studies"] if s.get("alias_group")}
    assert grouped == set(group["study_ids"])
    assert len(body["studies"]) == 3                                           # both studies are still listed
    files_after = {p.name: p.read_bytes() for p in (aihydro_home() / "sessions").glob("*.json")}
    assert files_before == files_after                                          # no study file was touched


def test_studies_without_a_site_id_are_never_grouped(ws):
    make_study("one", workspace=str(ws))
    make_study("two", workspace=str(ws))
    assert _ensure(ws)["duplicate_groups"] == []


# ---------------------------------------------------------------- host operations

def test_every_host_operation_is_audience_host_and_listed(ws):
    from ai_hydro.mcp.tool_surface import load_annotation_rows

    ops = {"ensure_project", "rename_project", "set_project_default_study", "add_project_study",
           "remove_project_study"}
    rows = load_annotation_rows()
    assert {n for n, r in rows.items() if r.get("audience") == "host"} == ops
    import asyncio
    from ai_hydro.mcp import mcp

    listed = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for n in ops:
        assert (listed[n].meta or {}).get("aihydro/audience") == "host", n


def test_rename_locks_the_title_and_refuses_a_bare_id(ws):
    body = _ensure(ws)
    pid = body["project_id"]
    res, out = call("rename_project", {"project_id": pid, "title": "  Maine and  Bay of Bengal "})
    assert out["title"] == "Maine and Bay of Bengal" and out["title_source"] == "user"
    assert out["revision"] == body["revision"] + 1
    for bad in ("", "   ", pid, "x" * 200):
        res, err = call("rename_project", {"project_id": pid, "title": bad})
        assert err["code"] == "INVALID_REQUEST", bad
    res, err = call("rename_project", {"project_id": "prj_01M62NJQ6QJD650KZQH15SYM95", "title": "x"})
    assert err["code"] == "PROJECT_NOT_FOUND"
    res, err = call("rename_project", {"project_id": "../etc", "title": "x"})
    assert err["code"] == "INVALID_REQUEST"                                    # not an id: never a path


def test_set_default_confirms_it_and_add_remove_study_edit_the_list(ws):
    make_study("s1", workspace=str(ws))
    make_study("s2", workspace=str(ws))
    make_study("s3")                                                           # no workspace: not linked at creation
    pid = _ensure(ws)["project_id"]
    res, out = call("add_project_study", {"project_id": pid, "study_id": "s3"})
    assert out["status"] == "added" and any(s["study_id"] == "s3" and s["added_by"] == "user" for s in out["studies"])
    res, out = call("add_project_study", {"project_id": pid, "study_id": "s3"})
    assert out["status"] == "already_member"
    res, err = call("add_project_study", {"project_id": pid, "study_id": "no_such_study"})
    assert err["code"] == "STUDY_NOT_FOUND"

    res, out = call("set_project_default_study", {"project_id": pid, "study_id": "s2"})
    assert out["default_study"] == {"study_id": "s2", "label": "s2 (unnamed study)", "confirmed": True, "confirmed_by": "user"}
    res, out = call("set_project_default_study", {"project_id": pid, "study_id": "s1", "confirmed_by": "host_launch"})
    assert out["default_study"]["confirmed_by"] == "host_launch"
    res, err = call("set_project_default_study", {"project_id": pid, "study_id": "s1", "confirmed_by": "agent"})
    assert err["code"] == "INVALID_REQUEST"
    res, err = call("set_project_default_study", {"project_id": pid, "study_id": "ghost"})
    assert err["code"] == "STUDY_NOT_FOUND"

    res, out = call("remove_project_study", {"project_id": pid, "study_id": "s1"})
    assert out["status"] == "removed" and out["default_study"] is None          # removing the default clears it
    res, out = call("remove_project_study", {"project_id": pid, "study_id": "s1"})
    assert out["status"] == "not_a_member"
    assert (aihydro_home() / "sessions" / "s1.json").exists()                   # the study itself is untouched

    res, out = call("set_project_default_study", {"project_id": pid, "study_id": "s3"})   # linked on the way
    assert out["default_study"]["study_id"] == "s3" and out["default_study"]["confirmed"]
    res, out = call("set_project_default_study", {"project_id": pid})
    assert out["status"] == "default_cleared" and out["default_study"] is None


def test_root_dir_aliases_and_reattach_after_a_move(ws, tmp_path):
    make_study("s1", workspace=str(ws))
    body = _ensure(ws)
    pid = body["project_id"]
    (tmp_path / "sub").mkdir()
    moved = tmp_path / "sub" / "try"
    ws.rename(moved)                                                           # the folder moved on disk
    res = read_resource(f"aihydro://project/by-dir?path={moved}")
    assert res["found"] is False
    assert [(c["project_id"], c["reason"]) for c in res["moved_candidates"]] == [(pid, "same_folder_name")]
    # an unrelated folder is not offered
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    assert read_resource(f"aihydro://project/by-dir?path={unrelated}")["moved_candidates"] == []
    res, out = call("ensure_project", {"root_dir": str(moved), "attach_to": pid})
    assert out["status"] == "reattached" and out["project_id"] == pid
    assert out["root_dir"] == str(moved.resolve()) and [os.path.dirname(a) for a in out["root_dir_aliases"]] == [str(tmp_path.resolve())]
    assert get_project_registry().find_by_dir(moved)["project_id"] == pid
    assert get_project_registry().find_by_dir(tmp_path / "try")["project_id"] == pid     # the old path still finds it
    # a directory another project owns cannot be taken over
    other = tmp_path / "other"
    other.mkdir()
    other_pid = _ensure(other)["project_id"]
    res, err = call("ensure_project", {"root_dir": str(other), "attach_to": pid})
    assert err["code"] == "INVALID_REQUEST" and other_pid != pid


# ---------------------------------------------------------------- B4: an unconfirmed default never resolves

def _two_studies_and_project(ws):
    make_study("s1", workspace=str(ws), **GAUGE)
    make_study("s2", workspace=str(ws))
    return _ensure(ws)["project_id"]


def test_an_unconfirmed_default_never_resolves_and_is_only_a_candidate(ws):
    pid = _two_studies_and_project(ws)
    rec = get_project_registry().get(pid)
    assert rec["default_study_id"] and rec["default_confirmed_by"] is None      # a suggestion exists
    assert get_project_registry().confirmed_default(workspace=str(ws)) is None
    assert get_project_registry().confirmed_default(project_id=pid) is None

    # a read-only call and a writer, with the workspace and with the project id: all fail
    for tool in ("list_features", "register_feature"):
        args = {} if tool == "list_features" else {"geojson": '{"type": "Point", "coordinates": [0, 0]}'}
        for meta in (ctx(CHAT_A, ws), {"aihydro/context": {"chat_id": CHAT_A, "project_id": pid}}):
            res, body = call(tool, args, meta=meta)
            assert body["code"] == "SESSION_RESOLUTION_FAILED", (tool, body)
    assert get_binding_store().lookup_study(CHAT_A) is None                    # nothing was bound
    res, body = call("list_features", {}, meta=ctx(CHAT_A, ws))
    first = body["candidates"][0]
    assert first["study_id"] == rec["default_study_id"] and first["suggested_default"] is True
    assert first["label"] and first["label"] != first["study_id"]
    assert "suggested default, not confirmed" in body["message"]
    assert len(body["candidates"]) <= 5 and len({c["study_id"] for c in body["candidates"]}) == len(body["candidates"])


def test_a_confirmed_default_resolves_as_project_default_and_binds_the_writer(ws):
    pid = _two_studies_and_project(ws)
    call("set_project_default_study", {"project_id": pid, "study_id": "s1"})
    # read-only: resolves, says so, does not bind
    res, body = call("list_features", {}, meta=ctx(CHAT_A, ws))
    assert body["session_id"] == "s1"
    assert body["session_note"] == "Used the project's confirmed default study: Piscataquis River (USGS 01031500) (s1)."
    assert get_binding_store().lookup_study(CHAT_A) is None
    # a writer binds the chat and the result says so
    poly = '{"type": "Polygon", "coordinates": [[[-69.5, 45.0], [-69.0, 45.0], [-69.0, 45.4], [-69.5, 45.0]]]}'
    res, body = call("register_feature", {"geojson": poly, "name": "roi"}, meta=ctx(CHAT_A, ws))
    assert not body.get("error"), body
    assert get_binding_store().lookup_study(CHAT_A) == "s1"
    assert body["session_binding"]["bound_by"] == "project_default"
    # the project is named by id alone (no workspace in the call)
    res, body = call("list_features", {}, meta={"aihydro/context": {"chat_id": CHAT_B, "project_id": pid}})
    assert body["session_id"] == "s1"


def test_the_confirmed_default_rule_is_closed_to_the_audit_eval_and_admin_tools(ws, monkeypatch):
    pid = _two_studies_and_project(ws)
    call("set_project_default_study", {"project_id": pid, "study_id": "s1"})
    with pytest.raises(SessionResolutionError):
        _resolve_session_with_rule(None, CHAT_A, allow_recent_fallback=False)  # no context either: nothing to find
    from ai_hydro.mcp.app import ACTIVE_WORKSPACE

    token = ACTIVE_WORKSPACE.set(str(ws))
    try:
        with pytest.raises(SessionResolutionError):
            _resolve_session_with_rule(None, CHAT_A, allow_recent_fallback=False)   # the audit's call
        res, body = call("clear_session", {}, meta=ctx(CHAT_A, ws))                 # admin, writes
        assert body["code"] == "SESSION_RESOLUTION_FAILED"
        monkeypatch.setattr("ai_hydro.mcp.helpers._in_eval", lambda: True)
        with pytest.raises(SessionResolutionError):
            _resolve_session_with_rule(None, CHAT_A)
    finally:
        ACTIVE_WORKSPACE.reset(token)


def test_a_chat_binding_beats_the_project_default_and_an_explicit_id_beats_both(ws):
    pid = _two_studies_and_project(ws)
    call("set_project_default_study", {"project_id": pid, "study_id": "s1"})
    get_binding_store().bind(CHAT_A, "s2")
    res, body = call("list_features", {}, meta=ctx(CHAT_A, ws))
    assert body["session_id"] == "s2"
    res, body = call("list_features", {"session_id": "s1"}, meta=ctx(CHAT_A, ws))
    assert body["session_id"] == "s1"


@pytest.mark.parametrize("breaker", ["archive", "remove_member", "delete_file"])
def test_a_confirmed_default_that_is_hidden_unlinked_or_gone_does_not_resolve(ws, breaker):
    pid = _two_studies_and_project(ws)
    call("set_project_default_study", {"project_id": pid, "study_id": "s1"})
    reg = get_project_registry()
    assert reg.confirmed_default(workspace=str(ws)) == ("s1", pid)
    if breaker == "archive":
        make_study("s1", workspace=str(ws), archived=True, **GAUGE)
    elif breaker == "remove_member":
        reg.mutate(pid, lambda r: r.__setitem__("studies", [s for s in r["studies"] if s["study_id"] != "s1"]))
    else:
        (aihydro_home() / "sessions" / "s1.json").unlink()
    assert reg.confirmed_default(workspace=str(ws)) is None


# ---------------------------------------------------------------- auto-create and bind add the study to the chat's project

def test_auto_create_adds_the_new_study_to_the_chats_project_as_an_unconfirmed_suggestion(ws):
    from ai_hydro.mcp.app import ACTIVE_CHAT_ID, ACTIVE_WORKSPACE

    t1, t2 = ACTIVE_CHAT_ID.set(CHAT_A), ACTIVE_WORKSPACE.set(str(ws))
    try:
        sid, rule = _resolve_session_with_rule(None, None, auto_create_hint="basin_26p9_78p1")
        assert (sid, rule) == ("basin_26p9_78p1", "auto_create")
    finally:
        ACTIVE_WORKSPACE.reset(t2)
        ACTIVE_CHAT_ID.reset(t1)
    assert get_binding_store().lookup_study(CHAT_A) == "basin_26p9_78p1"
    rec = get_project_registry().find_by_dir(ws)
    assert rec is not None                                                      # created for the chat's workspace
    assert [(s["study_id"], s["added_by"]) for s in rec["studies"]] == [("basin_26p9_78p1", "auto")]
    assert rec["default_study_id"] == "basin_26p9_78p1" and rec["default_confirmed_by"] is None


def test_a_later_bind_adds_a_study_and_changes_the_default_only_when_there_was_none(ws):
    make_study("s1", workspace=str(ws))
    make_study("s2", workspace=str(ws))
    pid = _ensure(ws)["project_id"]
    call("set_project_default_study", {"project_id": pid, "study_id": "s1"})
    make_study("s3", workspace=str(ws))
    res, body = call("aihydro_rebind_chat", {"study_id": "s3"}, meta=ctx(CHAT_A, ws))
    assert body["bound_to"] == "s3", body
    rec = get_project_registry().get(pid)
    assert {s["study_id"] for s in rec["studies"]} == {"s1", "s2", "s3"}
    s3 = next(s for s in rec["studies"] if s["study_id"] == "s3")
    assert s3["added_by"] == "auto"
    assert (rec["default_study_id"], rec["default_confirmed_by"]) == ("s1", "user")        # untouched


def test_binding_without_a_call_workspace_creates_no_project(ws):
    make_study("s1", workspace=str(ws))
    res, body = call("aihydro_rebind_chat", {"study_id": "s1"}, meta=ctx(CHAT_A))          # no workspace
    assert body["bound_to"] == "s1"
    assert get_project_registry().list_all() == []


# ---------------------------------------------------------------- atomic write + revision check

def test_every_write_bumps_the_revision_and_a_noop_does_not(ws):
    make_study("s1", workspace=str(ws))
    pid = _ensure(ws)["project_id"]
    reg = get_project_registry()
    r1 = reg.get(pid)["revision"]
    reg.rename(pid, "A")
    reg.rename(pid, "A")                                                        # same title: no write
    assert reg.get(pid)["revision"] == r1 + 1
    reg.add_study(pid, "s1")                                                    # already a member: no write
    assert reg.get(pid)["revision"] == r1 + 1


def test_a_stale_expected_revision_is_refused_with_the_current_project(ws):
    pid = _ensure(ws)["project_id"]
    res, out = call("rename_project", {"project_id": pid, "title": "first", "expected_revision": 1})
    assert out["revision"] == 2
    res, err = call("rename_project", {"project_id": pid, "title": "second", "expected_revision": 1})
    assert err["code"] == "REVISION_CONFLICT"
    assert err["current"]["title"] == "first" and err["current"]["revision"] == 2
    assert get_project_registry().get(pid)["title"] == "first"                  # the stale write changed nothing


def test_an_unreadable_project_file_is_never_overwritten(ws, reg):
    pid = _ensure(ws)["project_id"]
    path = reg.project_file(pid)
    path.write_text("{ not json")
    assert reg.get(pid) is None and reg.list_all() == []
    with pytest.raises(ProjectUnreadable):
        reg.rename(pid, "x")
    assert path.read_text() == "{ not json"
    res, err = call("rename_project", {"project_id": pid, "title": "x"})
    assert err["code"] == "PROJECT_UNREADABLE"


def test_a_project_file_for_another_id_or_schema_is_not_a_project(ws, reg):
    pid = _ensure(ws)["project_id"]
    rec = json.loads(reg.project_file(pid).read_text())
    rec["schema"] = "aihydro.project/1"
    reg.project_file(pid).write_text(json.dumps(rec))
    assert reg.get(pid) is None


_WRITER = r"""
import json, os, sys, time
from pathlib import Path
from ai_hydro.session.project_registry import ProjectRegistry, RevisionConflict

mode, pid, tag, go = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4])
reg = ProjectRegistry()
while not go.exists():
    time.sleep(0.001)
try:
    if mode == "expect":                       # every writer read revision 1 and tries to write on it
        reg.rename(pid, "writer-" + tag, expected_revision=1)
    elif mode == "add":                        # no expectation: serialised by the lock, none may be lost
        reg.add_study(pid, "study_" + tag, added_by="user", require_exists=False)
    print("ok")
except RevisionConflict as exc:
    print("conflict", exc.current["revision"])
"""


def _run_writers(mode, pid, n, tmp_path):
    go = tmp_path / "go"
    env = {**os.environ}
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, mode, pid, str(i), str(go)], cwd=str(REPO_ROOT), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(n)]
    time.sleep(0.3)                                                             # all writers are waiting on the gate
    go.write_text("go")
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        outs.append(out.strip())
    return outs


def test_two_writers_on_the_same_revision_exactly_one_wins(ws, tmp_path):
    """The two-writer race: both read revision 1; the file lock serialises them; the loser is refused."""
    pid = _ensure(ws)["project_id"]
    assert get_project_registry().get(pid)["revision"] == 1
    outs = _run_writers("expect", pid, 2, tmp_path)
    assert sorted(o.split()[0] for o in outs) == ["conflict", "ok"], outs
    assert [o for o in outs if o.startswith("conflict")][0].endswith(" 2")      # the loser saw revision 2
    final = get_project_registry().get(pid)
    assert final["revision"] == 2                                               # one write landed, not two
    assert final["title"] in {"writer-0", "writer-1"}
    assert json.loads(get_project_registry().project_file(pid).read_text())["revision"] == 2   # whole, parseable file


def test_many_unconditioned_writers_lose_no_update(ws, tmp_path):
    pid = _ensure(ws)["project_id"]
    n = 8
    outs = _run_writers("add", pid, n, tmp_path)
    assert outs == ["ok"] * n
    rec = get_project_registry().get(pid)
    assert {s["study_id"] for s in rec["studies"]} == {f"study_{i}" for i in range(n)}
    assert rec["revision"] == 1 + n


def test_a_reader_never_sees_a_torn_file_while_writers_write(ws, tmp_path):
    pid = _ensure(ws)["project_id"]
    path = get_project_registry().project_file(pid)
    go = tmp_path / "go"
    env = {**os.environ}
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, "add", pid, str(i), str(go)], cwd=str(REPO_ROOT), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(6)]
    go.write_text("go")
    seen = 0
    while any(p.poll() is None for p in procs):
        json.loads(path.read_text())                                            # raises if the file were half-written
        seen += 1
    for p in procs:
        out, err = p.communicate()
        assert p.returncode == 0, err
    assert seen > 0 and json.loads(path.read_text())["revision"] == 7


def test_concurrent_ensure_of_one_directory_makes_one_project(ws, tmp_path):
    code = ("import sys, time\nfrom pathlib import Path\nfrom ai_hydro.session.project_registry import get_project_registry\n"
            "go = Path(sys.argv[2])\nwhile not go.exists():\n    time.sleep(0.001)\n"
            "print(get_project_registry().ensure(sys.argv[1])[0]['project_id'])\n")
    go = tmp_path / "go"
    procs = [subprocess.Popen([sys.executable, "-c", code, str(ws), str(go)], cwd=str(REPO_ROOT), env={**os.environ},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(5)]
    time.sleep(0.3)
    go.write_text("go")
    ids = set()
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        ids.add(out.strip())
    assert len(ids) == 1 and len(get_project_registry().list_all()) == 1


# ---------------------------------------------------------------- misc contracts

def test_capabilities_advertise_the_registry():
    from ai_hydro.mcp.capabilities import CAPABILITIES

    assert {"project.registry_v2", "project.host_ops", "project.revision_check", "binding.project_default",
            "binding.meta_project_id", "study.minted_ids"} <= set(CAPABILITIES)
    assert len(CAPABILITIES) == len(set(CAPABILITIES))


def test_the_registry_never_touches_a_path_outside_the_scratch_home(ws, reg, tmp_path):
    _ensure(ws)
    assert str(reg.projects_dir).startswith(str(aihydro_home()))
    assert str(aihydro_home()) != str(Path.home() / ".aihydro")


# ---------------------------------------------------------------- review fixes

def test_a_case_variant_of_the_reserved_prefix_cannot_overwrite_a_registry_project(ws):
    from ai_hydro.session.project import ProjectSession

    pid = _ensure(ws)["project_id"]
    path = get_project_registry().project_file(pid)
    before = path.read_bytes()
    for variant in ("PRJ_" + pid[4:], "Prj_" + pid[4:], pid.upper(), "pRj_x"):
        with pytest.raises(ValueError, match="reserved"):
            ProjectSession(variant).save()
        with pytest.raises(ValueError, match="reserved"):
            ProjectSession.load(variant)
    assert path.read_bytes() == before
    assert get_project_registry().get(pid) is not None
    assert ProjectSession.list_all() == []


def test_host_audience_tools_are_not_named_to_the_agent(ws):
    import asyncio

    from ai_hydro.mcp.tools_discovery import aihydro_describe_capability
    from ai_hydro.mcp.tools_session import list_available_tools

    ops = {"ensure_project", "rename_project", "set_project_default_study", "add_project_study",
           "remove_project_study"}
    seen = set()
    out = asyncio.run(list_available_tools())
    pages = 0
    while True:
        seen |= {t["name"] if isinstance(t, dict) else t for t in out["tools"]}
        pages += 1
        if out.get("next_offset") is None or pages > 200:
            break
        out = asyncio.run(list_available_tools(offset=out["next_offset"]))
    assert seen and not (seen & ops)
    assert not (ops & {t["name"] for t in asyncio.run(list_available_tools(name="ensure_project")).get("tools", [])})
    for domain in (None, "project", "session"):
        res = asyncio.run(aihydro_describe_capability(domain=domain))
        assert not (ops & {t["name"] for t in res.get("tools", [])}), domain
    # still callable by name (the host calls them) and still registered
    res, body = call("ensure_project", {"root_dir": str(ws)})
    assert not body.get("error")


def test_project_default_ignores_the_machine_global_workspace_file(ws):
    pid = _two_studies_and_project(ws)
    call("set_project_default_study", {"project_id": pid, "study_id": "s1"})
    f = aihydro_home() / "active_workspace.json"
    f.write_text(json.dumps({"workspace": str(ws)}))                 # whichever window was focused last
    res, body = call("list_features", {})                            # read-only, no context of its own
    assert body["code"] == "SESSION_RESOLUTION_FAILED"
    res, body = call("list_features", {}, meta=ctx(CHAT_A, ws))      # the call's own workspace does resolve
    assert body["session_id"] == "s1"
