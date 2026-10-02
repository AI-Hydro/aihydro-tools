"""``list_available_tools`` must list the real registry when called through the app.

FastMCP runs sync tools on the server's event loop, so ``_list_tools_sync`` is invoked
with a loop already running; it used to return ``[]`` there (``n_tools: 0``)."""
from __future__ import annotations

import asyncio

from fastmcp import Client

import ai_hydro.mcp  # noqa: F401  (registers every tool)
from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.tools_docs import _list_tools_sync


def test_list_available_tools_through_the_real_app_is_not_empty():
    async def run():
        async with Client(mcp) as c:
            return await c.call_tool("list_available_tools", {}, raise_on_error=False)
    res = asyncio.run(run())
    out = res.structured_content
    assert not res.is_error
    assert out["n_tools"] > 100 and out["n_tools"] == len(out["tools"])
    names = {t["name"] for t in out["tools"]}
    assert {"add_claim", "list_available_tools"} <= names
    add_claim = next(t for t in out["tools"] if t["name"] == "add_claim")
    assert "session_id" in add_claim["parameters"]


def test_list_tools_sync_works_with_and_without_a_running_loop():
    plain = _list_tools_sync()
    assert plain

    async def inside():
        return _list_tools_sync()
    assert {t.name for t in asyncio.run(inside())} == {t.name for t in plain}
