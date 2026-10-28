"""A readOnlyHint tool resolves its session without side effects; the recent-study guess is loud.

Reviewer repro: a read-only call with an explicit session_id and a chat id used to REBIND the chat
to that study (re-routing later writes) and, through the one-time workspace_dir backfill, save the
session (bumping its mtime, which feeds the recent-study fallback).
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastmcp import Client

from ai_hydro.mcp import mcp
from ai_hydro.mcp.helpers import recent_fallback_enabled
from ai_hydro.session import chat_binding
from ai_hydro.session import store as _store


@pytest.fixture
def world(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions)
    bindings = chat_binding.ChatBindingStore(tmp_path / "bindings.json")
    monkeypatch.setattr(chat_binding, "get_binding_store", lambda: bindings)
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AIHYDRO_RECENT_SESSION_FALLBACK", raising=False)
    monkeypatch.delenv("AIHYDRO_EVAL_NONCE", raising=False)
    for sid in ("s_old", "s_new"):
        s = _store.HydroSession.load(sid)
        s.add_note("x")
        s.save()
    os.utime(sessions / "s_old.json", (1, 1))
    return {"sessions": sessions, "bindings": bindings, "workspace": str(ws)}


def _mtimes(w):
    return {p.name: p.stat().st_mtime_ns for p in w["sessions"].glob("*.json")}


def _call(tool, args, **meta):
    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(tool, args, raise_on_error=False, **meta)

    return asyncio.run(go())


def _body(res) -> dict:
    return json.loads(res.content[0].text)


def test_read_only_call_with_explicit_session_and_chat_does_not_rebind(world):
    before = _mtimes(world)
    res = _call("get_session_summary", {"session_id": "s_old", "_chat_id": "01HCHATA000000000000000000"})
    assert not res.is_error
    assert world["bindings"].lookup_study("01HCHATA000000000000000000") is None
    assert _mtimes(world) == before


def test_a_writer_with_explicit_session_and_chat_still_binds(world):
    res = _call("add_note", {"session_id": "s_old", "note": "n", "_chat_id": "01HCHATA000000000000000000"})
    assert not res.is_error, res.content
    assert world["bindings"].lookup_study("01HCHATA000000000000000000") == "s_old"


def test_read_only_call_does_not_backfill_workspace_or_touch_mtime(world):
    before = _mtimes(world)
    res = _call("get_session_summary",
                {"session_id": "s_old", "_chat_id": "01HCHATA000000000000000000", "_workspace": world["workspace"]})
    assert not res.is_error
    assert _store.HydroSession.load("s_old").workspace_dir in (None, "")
    assert _mtimes(world) == before


def test_read_only_call_via_chat_binding_does_not_backfill(world):
    world["bindings"].bind("01HCHATB000000000000000000", "s_old")
    before = _mtimes(world)
    res = _call("list_features", {"_chat_id": "01HCHATB000000000000000000", "_workspace": world["workspace"]})
    assert not res.is_error
    assert _body(res)["session_id"] == "s_old"
    assert _mtimes(world) == before


def test_get_model_results_and_list_features_echo_the_session_they_read(world):
    assert _body(_call("list_features", {"session_id": "s_new"}))["session_id"] == "s_new"
    assert _body(_call("get_model_results", {"session_id": "s_new"}))["session_id"] == "s_new"


def test_recent_fallback_result_carries_a_visible_session_warning(world, monkeypatch):
    monkeypatch.setenv("AIHYDRO_RECENT_SESSION_FALLBACK", "on")        # off by default since 2.8.2
    res = _call("list_features", {})
    body = _body(res)
    assert body["session_id"] == "s_new"
    assert body["session_warning"] == (
        "No study was named or bound; used most-recent study s_new \u2014 name a session to be sure")
    # no binding, no mtime change from the guess itself (read-only)
    assert world["bindings"].lookup_study("01HCHATZ000000000000000000") is None


def test_recent_fallback_warning_says_wrote_to_for_a_writer():
    from types import SimpleNamespace

    from ai_hydro.mcp.app import _inject_session_warning
    from ai_hydro.mcp.tool_surface import read_only_tools

    assert "compute_flow_duration_curve" not in read_only_tools() and "list_features" in read_only_tools()
    result = SimpleNamespace(structured_content={"ok": 1}, content=[SimpleNamespace(text=json.dumps({"ok": 1}))])
    _inject_session_warning(result, "s_new", read_only=False)
    expected = "No study was named or bound; wrote to most-recent study s_new \u2014 name a session to be sure"
    assert result.structured_content["session_warning"] == expected
    assert json.loads(result.content[0].text)["session_warning"] == expected


def test_named_session_has_no_warning(world):
    assert "session_warning" not in _body(_call("list_features", {"session_id": "s_new"}))


@pytest.mark.parametrize("value", ["", "off", "OFF", "0", "false", "no", " No ", "garbage"])
def test_fallback_is_off_unless_switched_on(world, monkeypatch, value):
    monkeypatch.setenv("AIHYDRO_RECENT_SESSION_FALLBACK", value)
    assert recent_fallback_enabled() is False
    res = _call("list_features", {})
    assert res.is_error or _body(res).get("code") == "SESSION_RESOLUTION_FAILED"


def test_fallback_is_off_when_the_variable_is_unset(world):
    assert recent_fallback_enabled() is False


@pytest.mark.parametrize("value", ["on", "ON", "1", "true", "yes"])
def test_fallback_switch_on_values_turn_it_on(world, monkeypatch, value):
    monkeypatch.setenv("AIHYDRO_RECENT_SESSION_FALLBACK", value)
    assert recent_fallback_enabled() is True


def test_fallback_is_off_in_an_evaluation_run(world, monkeypatch):
    monkeypatch.setenv("AIHYDRO_EVAL_NONCE", "nonce")
    assert recent_fallback_enabled() is False


def test_hot_surface_is_ignored_in_an_evaluation_run(monkeypatch):
    from ai_hydro.mcp.tool_surface import SURFACE_ENV, tool_surface_mode

    monkeypatch.setenv(SURFACE_ENV, "hot")
    monkeypatch.delenv("AIHYDRO_EVAL_NONCE", raising=False)
    assert tool_surface_mode() == "hot"
    monkeypatch.setenv("AIHYDRO_EVAL_NONCE", "nonce")
    assert tool_surface_mode() == "full"


def test_get_data_fetch_result_rejects_path_like_job_ids(world):
    for bad in ("../x", "a/b", "..", "a\\b", ""):
        body = _call("get_data_fetch_result", {"job_id": bad})
        text = body.content[0].text if body.content else ""
        assert "Invalid job_id" in text or body.is_error, (bad, text)
