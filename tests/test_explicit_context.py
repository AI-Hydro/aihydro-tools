"""
Explicit request context (ADR-004, slice 2 packet P2).

Clients send ``_meta["aihydro/context"] = {study_id, workspace, chat_id,
client}`` on ``tools/call``; the legacy hidden ``_chat_id`` / ``_workspace``
arguments are read second.  Calls go through a real in-process fastmcp
``Client`` (which puts ``_meta`` on the wire request) against a throwaway
server wired like ``ai_hydro.mcp.app``.  Runs record ``extra.context_source``
and ``extra.session_resolution``.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from fastmcp import Client, FastMCP

from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore, get_binding_store
from ai_hydro.session.store import HydroSession

SID = "ctx-session"
OTHER = "ctx-other"
CHAT = "01KV1EPM88P9FV5CWNVJ9V60JY"
META = "aihydro/context"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)
    from ai_hydro.session import chat_binding

    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    HydroSession(SID).save()
    HydroSession(OTHER).save()
    rr.reset_stats()
    return tmp_path


@pytest.fixture
def server(env):
    from ai_hydro.mcp import app
    from ai_hydro.mcp.helpers import _resolve_session

    srv = FastMCP(name="ctx-probe")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())

    @srv.tool()
    def resolving_tool(session_id: str | None = None) -> dict:
        sid = _resolve_session(session_id, allow_auto_create=False)
        return {"data": {"resolved": sid, "chat": app.ACTIVE_CHAT_ID.get(),
                         "workspace": app.ACTIVE_WORKSPACE.get(),
                         "source": app.ACTIVE_CONTEXT_SOURCE.get()}}

    @srv.tool()
    def passive_tool(x: int = 1, session_id: str | None = None) -> dict:
        return {"data": {"x": x}}

    return srv


def call(srv, name, arguments=None, meta=None):
    async def run():
        async with Client(srv) as c:
            return await c.call_tool(name, arguments or {}, meta=meta, raise_on_error=False)

    return asyncio.run(run())


def latest_record(session_id=SID):
    rows = HydroSession.load(session_id).get("_run_log") or {}
    assert rows, "no run-log rows"
    row = list(rows.values())[-1]
    return row["record"]


def test_meta_only_resolves_session_and_records_meta(server):
    res = call(server, "resolving_tool", meta={META: {"study_id": SID, "chat_id": CHAT,
                                                      "workspace": "/w/ws", "client": "vscode/1"}})
    assert not res.is_error
    data = res.data["data"]
    assert data["resolved"] == SID
    assert (data["chat"], data["workspace"], data["source"]) == (CHAT, "/w/ws", "meta")
    extra = latest_record()["extra"]
    assert extra["context_source"] == "meta"
    assert extra["session_resolution"] == "meta"
    assert extra["context_client"] == "vscode/1"
    # study_id also rebinds the chat, like an explicit session_id.
    assert get_binding_store().lookup_study(CHAT) == SID


def test_meta_study_id_resolves_for_a_tool_that_never_resolves(server):
    res = call(server, "passive_tool", meta={META: {"study_id": SID}})
    assert not res.is_error
    extra = latest_record()["extra"]
    assert extra["context_source"] == "meta"
    assert extra["session_resolution"] == "meta"


def test_explicit_arg_beats_meta_study_id(server):
    call(server, "resolving_tool", {"session_id": OTHER}, meta={META: {"study_id": SID}})
    extra = latest_record(OTHER)["extra"]
    assert extra["session_resolution"] == "explicit_arg"
    assert extra["context_source"] == "meta"


def test_legacy_hidden_args_still_work_and_are_recorded(server, caplog):
    from ai_hydro.mcp import app

    get_binding_store().bind(CHAT, SID)
    app._legacy_context_warned = False
    with caplog.at_level(logging.WARNING, logger="ai_hydro.mcp.app"):
        r1 = call(server, "resolving_tool", {"_chat_id": CHAT, "_workspace": "/w/legacy"})
        r2 = call(server, "resolving_tool", {"_chat_id": CHAT, "_workspace": "/w/legacy"})
    assert not r1.is_error and not r2.is_error
    assert r1.data["data"]["resolved"] == SID
    assert r1.data["data"]["workspace"] == "/w/legacy"
    extra = latest_record()["extra"]
    assert extra["context_source"] == "legacy_args"
    assert extra["session_resolution"] == "chat_binding"
    deprecations = [r for r in caplog.records if "Deprecated" in r.getMessage()]
    assert len(deprecations) == 1   # logged once per process


def test_meta_wins_over_legacy_per_field(server):
    get_binding_store().bind(CHAT, SID)
    res = call(server, "resolving_tool",
               {"_chat_id": "legacy-chat", "_workspace": "/w/legacy"},
               meta={META: {"chat_id": CHAT}})
    data = res.data["data"]
    assert data["chat"] == CHAT                 # meta
    assert data["workspace"] == "/w/legacy"     # legacy fills the gap
    assert data["resolved"] == SID
    assert latest_record()["extra"]["context_source"] == "meta"


def test_no_context_records_none(server):
    call(server, "resolving_tool", {"session_id": SID})
    extra = latest_record()["extra"]
    assert extra["context_source"] == "none"
    assert extra["session_resolution"] == "explicit_arg"


def test_malformed_meta_is_ignored(server):
    res = call(server, "resolving_tool", {"session_id": SID}, meta={META: ["not", "a", "dict"]})
    assert not res.is_error
    assert latest_record()["extra"]["context_source"] == "none"


def test_context_vars_reset_after_call(server):
    from ai_hydro.mcp import app

    call(server, "resolving_tool", meta={META: {"study_id": SID, "chat_id": CHAT}})
    assert app.ACTIVE_STUDY_ID.get() is None
    assert app.ACTIVE_CONTEXT_SOURCE.get() == "none"


def test_real_server_accepts_meta_through_client(env):
    from ai_hydro.mcp.app import mcp

    res = call(mcp, "list_available_tools", meta={META: {"study_id": SID, "chat_id": CHAT}})
    assert not res.is_error


def test_chat_binding_follows_aihydro_home(tmp_path, monkeypatch):
    from ai_hydro.session import chat_binding

    monkeypatch.setattr(chat_binding, "_store", None)
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("AIHYDRO_HOME", str(home_a))
    get_binding_store().bind("c1", "s1")
    assert (home_a / "chat_studies.json").exists()
    monkeypatch.setenv("AIHYDRO_HOME", str(home_b))
    assert get_binding_store().lookup_study("c1") is None
    get_binding_store().bind("c2", "s2")
    assert (home_b / "chat_studies.json").exists()


def test_writer_row_in_other_session_is_flagged_mismatch(server):
    from ai_hydro.mcp.helpers import _session_store

    @server.tool()
    def cross_writer(session_id: str | None = None) -> dict:
        _session_store(OTHER, "model", {"data": {"v": 1}}, tool_name="cross_writer")
        return {"data": {"ok": True}}

    call(server, "cross_writer", meta={META: {"study_id": SID}})
    extra = latest_record(OTHER)["extra"]
    assert extra["session_resolution"] == "writer_row"
    assert extra["context_study_id"] == SID
    assert extra["context_mismatch"] is True


def test_no_mismatch_flag_when_resolved_matches(server):
    call(server, "resolving_tool", meta={META: {"study_id": SID}})
    extra = latest_record()["extra"]
    assert extra["context_study_id"] == SID
    assert "context_mismatch" not in extra
