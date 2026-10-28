"""Shared fixtures for the binding / study-recorder tests (chat ids, study files, tool calls)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastmcp import Client

# Real-shaped chat ids (Crockford base32, 26 chars): what the extension injects.
CHAT_A = "01KV1EPM88P9FV5CWNVJ9V60JY"
CHAT_B = "01KV1YSN57DFBQPPT3GH23WASF"
CHAT_C = "01KW7W799J9Z934NT9YB2BF42Y"
META = "aihydro/context"


def make_study(session_id: str, *, workspace: str | None = None, site_id: str = "", site_name: str = "",
               site_type: str = "", archived: bool = False, extra: dict | None = None,
               outlet: tuple[float, float] | None = None):
    """Write a study file under the (per-test) AIHYDRO_HOME and return the session."""
    from ai_hydro.session import HydroSession

    s = HydroSession.load(session_id)
    s.site_id, s.site_name, s.site_type = site_id, site_name, site_type
    s.workspace_dir = workspace
    s.archived = archived
    if extra:
        s.extra.update(extra)
    if outlet:
        s.put_result("watershed", "__legacy__", "", {"data": {"outlet_lat": outlet[0], "outlet_lon": outlet[1]}})
    s.save()
    return s


def call(tool: str, args: dict | None = None, meta: dict | None = None):
    """Call a tool on the real server, through the real middleware chain. Returns (result, body-dict-or-None)."""
    from ai_hydro.mcp import mcp

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(tool, args or {}, meta=meta, raise_on_error=False)

    res = asyncio.run(go())
    body = None
    if res.content:
        try:
            body = json.loads(res.content[0].text)
        except ValueError:
            body = None
    return res, body


def ctx(chat: str | None = None, workspace: str | Path | None = None, study: str | None = None) -> dict:
    """``_meta`` for a call: ``{"aihydro/context": {...}}``."""
    c: dict = {}
    if chat:
        c["chat_id"] = chat
    if workspace:
        c["workspace"] = str(workspace)
    if study:
        c["study_id"] = study
    return {META: c}


def read_resource(uri: str) -> dict:
    from ai_hydro.mcp import mcp

    async def go():
        async with Client(mcp) as c:
            return await c.read_resource(uri)

    return json.loads(asyncio.run(go())[0].text)
