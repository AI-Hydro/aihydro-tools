"""``list_claims`` / ``list_assumptions`` must not report a missing or unreadable
session as an empty ledger; a genuinely empty session still lists ``[]``."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import Client

import ai_hydro.mcp  # noqa: F401  (registers every tool)
from ai_hydro.mcp.app import mcp
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession

TOOLS = [("list_claims", {}), ("list_assumptions", {})]


@pytest.fixture(autouse=True)
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    return tmp_path / "sessions"


def call(name, args):
    async def run():
        async with Client(mcp) as c:
            return await c.call_tool(name, args, raise_on_error=False)
    return asyncio.run(run())


def envelope(res):
    assert res.is_error
    text = "".join(getattr(b, "text", "") for b in res.content)
    assert "Output validation error" not in text
    return json.loads(text)


@pytest.mark.parametrize("name,extra", TOOLS)
def test_missing_session_is_a_structured_error(name, extra):
    env = envelope(call(name, {"session_id": "no-such-session", **extra}))
    assert env["error"] is True and env["code"] == "SESSION_NOT_FOUND"
    assert env["recovery"] and env["next_tools"]


@pytest.mark.parametrize("name,extra", TOOLS)
def test_corrupt_session_is_a_structured_error(name, extra, sessions):
    sessions.mkdir(parents=True)
    (sessions / "broken.json").write_text("{not json")
    env = envelope(call(name, {"session_id": "broken", **extra}))
    assert env["error"] is True and "corrupted" in env["message"]


@pytest.mark.parametrize("name,extra", TOOLS)
def test_genuinely_empty_session_lists_nothing(name, extra):
    HydroSession("empty-one").save()
    res = call(name, {"session_id": "empty-one", **extra})
    assert not res.is_error
    assert res.structured_content == {"result": []}


def test_populated_session_still_lists(sessions):
    s = HydroSession("with-rows")
    s.claims = {"C-1": {"id": "C-1", "status": "draft"}, "C-2": {"id": "C-2", "status": "supported"}}
    s.assumptions = {"A-1": {"id": "A-1", "validated": False}}
    s.save()
    assert [c["id"] for c in call("list_claims", {"session_id": "with-rows"}).structured_content["result"]] == ["C-1", "C-2"]
    only = call("list_claims", {"session_id": "with-rows", "status": "supported"}).structured_content["result"]
    assert [c["id"] for c in only] == ["C-2"]
    assert call("list_assumptions", {"session_id": "with-rows", "validated": False}).structured_content["result"][0]["id"] == "A-1"
