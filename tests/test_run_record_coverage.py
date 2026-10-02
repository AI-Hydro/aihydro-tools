"""
Coverage ratchet for run recording (ADR-001, slice 1a, acceptance criterion 3).

Every registered MCP tool must either be recorded by ``RunRecordMiddleware``
when called with a session, or be listed in ``RECORD_EXEMPT`` with a reason.
Adding a tool without deciding fails here; so does leaving an exemption behind
for a tool that no longer exists.

The middleware is driven directly per tool name (tool bodies are not run: many
need network or heavy data), so this proves the recording decision, not each
tool's own output.
"""
from __future__ import annotations

import asyncio

import fastmcp
import mcp.types as mcp_types
import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult

from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession

pytestmark = pytest.mark.skipif(
    int(fastmcp.__version__.split(".")[0]) >= 3,
    reason="tool enumeration uses the pinned FastMCP 2.x registry accessor",
)

SID = "coverage-session"


def _registered_tool_names() -> list:
    from ai_hydro.mcp.app import mcp

    return sorted(tool.name for tool in asyncio.run(mcp.list_tools()))


async def _call_through_middleware(name: str):
    from ai_hydro.mcp.app import RunRecordMiddleware

    context = MiddlewareContext(
        message=mcp_types.CallToolRequestParams(name=name, arguments={"session_id": SID}),
        method="tools/call",
    )

    async def call_next(_context):
        return ToolResult(structured_content={"data": {"value": 1}})

    return await RunRecordMiddleware().on_call_tool(context, call_next)


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)
    HydroSession(SID).save()
    rr.reset_stats()
    return tmp_path


def test_exempt_list_has_no_stale_entries_and_every_entry_has_a_reason():
    registered = set(_registered_tool_names())
    stale = sorted(set(rr.RECORD_EXEMPT) - registered)
    assert not stale, f"RECORD_EXEMPT names tools that are not registered: {stale}"
    for name, reason in rr.RECORD_EXEMPT.items():
        assert isinstance(reason, str) and len(reason) > 20, f"{name}: exemption needs a real reason"


def test_every_registered_tool_is_recorded_or_explicitly_exempt(session_dir):
    names = _registered_tool_names()
    assert len(names) > 100, "tool enumeration found suspiciously few tools"
    recorded, exempt, failures = [], [], []
    for name in names:
        before = set(HydroSession.load(SID).get("_run_log") or {})
        asyncio.run(_call_through_middleware(name))
        rows = HydroSession.load(SID).get("_run_log") or {}
        added = [rows[i] for i in set(rows) - before]
        if name in rr.RECORD_EXEMPT:
            exempt.append(name)
            if added:
                failures.append(f"{name}: exempt but wrote a row")
            continue
        recorded.append(name)
        if len(added) != 1:
            failures.append(f"{name}: expected 1 row, got {len(added)}")
            continue
        check = rr.verify_run_log_entry(added[0])
        if not (check["has_record"] and check["record_ok"] and check["entry_ok"]):
            failures.append(f"{name}: record missing or does not verify: {check}")
        elif added[0]["record"]["tool"] != name or added[0]["record"]["schema"] != "aihydro.run/2":
            failures.append(f"{name}: record has the wrong tool/schema")
    assert not failures, "\n".join(failures)
    assert len(recorded) + len(exempt) == len(names)
    coverage = len(recorded) / len(names)
    # Ratchet: tighten as exemptions are removed; never loosen without review.
    assert coverage >= 0.55, f"record coverage fell to {coverage:.3f} ({len(recorded)}/{len(names)})"
    print(f"\nrun-record coverage: {len(recorded)}/{len(names)} tools = {coverage:.4f} "
          f"({len(exempt)} exempt with reasons)")


def test_tier1_scientific_tools_are_never_exempt():
    from ai_hydro.mcp.app import TOOL_TIERS

    tier1 = {name for name, tier in TOOL_TIERS.items() if tier == 1}
    assert tier1 & set(rr.RECORD_EXEMPT) == set()
