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
    # no-argument answer is one capped page (see tests/test_context_diet.py); the total is the registry
    assert out["n_tools"] > 100 and 0 < len(out["tools"]) < out["n_tools"]
    assert out["next_offset"] == len(out["tools"])

    async def one():
        async with Client(mcp) as c:
            return await c.call_tool("list_available_tools", {"name": "add_claim"}, raise_on_error=False)
    add_claim = asyncio.run(one()).structured_content["tool"]
    assert add_claim["name"] == "add_claim" and "session_id" in add_claim["parameters"]


def test_list_tools_sync_works_with_and_without_a_running_loop():
    plain = _list_tools_sync()
    assert plain

    async def inside():
        return _list_tools_sync()
    assert {t.name for t in asyncio.run(inside())} == {t.name for t in plain}
