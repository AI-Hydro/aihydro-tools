"""
Regression guard for the ``_chat_id`` / ``_workspace`` injection contract.

The VS Code extension injects ``_chat_id`` and ``_workspace`` into EVERY
ai-hydro MCP tool call.  FastMCP validates tool arguments against a Pydantic
model that rejects unknown keys, so those two fields must be stripped *before*
validation by the ``_ContextInjectionMiddleware`` registered in
``ai_hydro.mcp.app``.

History: a previous implementation monkeypatched ``mcp._call_tool_mcp`` after
construction.  FastMCP's low-level server binds that method during ``__init__``,
so the patch became dead code and EVERY tool call failed with
``Unexpected keyword argument _chat_id``.  No test exercised the real handler
path, so the total outage shipped silently.  These tests drive the actual MCP
``CallToolRequest`` handler — the same path the extension hits over the wire —
so any future regression of the injection contract fails CI immediately.
"""
from __future__ import annotations

import asyncio

import mcp.types as mcp_types
import pytest

# A real ULID-shaped chat id, matching what the extension injects.
INJECTED_CHAT_ID = "01KV1EPM88P9FV5CWNVJ9V60JY"
INJECTED_WORKSPACE = "/Users/researcher/Desktop/basin_demo"


def _call_via_lowlevel(tool_name: str, arguments: dict):
    """Invoke a tool through the real low-level MCP CallToolRequest handler.

    This is the exact path the extension exercises over stdio/SSE, so it
    includes argument validation and the middleware chain.
    """
    from ai_hydro.mcp.app import mcp

    handler = mcp._mcp_server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=tool_name, arguments=arguments),
    )
    result = asyncio.run(handler(req))
    root = result.root if hasattr(result, "root") else result
    text = root.content[0].text if root.content else ""
    return root.isError, text


def test_injected_params_do_not_break_tool_call():
    """A tool call carrying _chat_id + _workspace must NOT raise a validation error."""
    # list_available_tools is a cheap, side-effect-free Tier-3 tool present in
    # every build; it takes no required args, so any error here is the
    # injection contract failing, not the tool itself.
    is_error, text = _call_via_lowlevel(
        "list_available_tools",
        {"_chat_id": INJECTED_CHAT_ID, "_workspace": INJECTED_WORKSPACE},
    )
    assert is_error is not True, f"Injected params leaked into validation: {text}"
    assert "Unexpected keyword argument" not in text
    assert "_chat_id" not in text


def test_injected_params_are_exposed_via_contextvars():
    """The middleware must surface _chat_id / _workspace to the ContextVars
    that _resolve_session() reads."""
    from ai_hydro.mcp.app import ACTIVE_CHAT_ID, ACTIVE_WORKSPACE, mcp
    from fastmcp.server.middleware import Middleware, MiddlewareContext

    seen = {}

    class _Probe(Middleware):
        async def on_call_tool(self, context: MiddlewareContext, call_next):
            # Runs AFTER _ContextInjectionMiddleware in the chain, so the
            # ContextVars are already populated and args already stripped.
            seen["chat_id"] = ACTIVE_CHAT_ID.get()
            seen["workspace"] = ACTIVE_WORKSPACE.get()
            seen["args"] = dict(getattr(context.message, "arguments", {}) or {})
            return await call_next(context)

    mcp.add_middleware(_Probe())
    try:
        _call_via_lowlevel(
            "list_available_tools",
            {"_chat_id": INJECTED_CHAT_ID, "_workspace": INJECTED_WORKSPACE},
        )
    finally:
        # Best-effort removal so the probe does not leak into other tests.
        try:
            mcp.middleware[:] = [m for m in mcp.middleware if not isinstance(m, _Probe)]
        except Exception:
            pass

    assert seen.get("chat_id") == INJECTED_CHAT_ID
    assert seen.get("workspace") == INJECTED_WORKSPACE
    # The injected keys must have been stripped before the tool sees them.
    assert "_chat_id" not in seen.get("args", {})
    assert "_workspace" not in seen.get("args", {})


def test_call_without_injection_still_works():
    """Direct calls (tests/CLI) with no injected params remain a clean no-op."""
    is_error, text = _call_via_lowlevel("list_available_tools", {})
    assert is_error is not True, f"Unexpected error on plain call: {text}"


def test_context_injection_runs_before_arg_repair():
    """Ordering invariant: _chat_id / _workspace must be stripped BEFORE the
    arg-repair middleware runs, otherwise arg-repair misreads the injected
    keys as typos (e.g. suggesting _chat_id -> gauge_id) and the call fails.
    """
    from ai_hydro.mcp.app import mcp

    names = [type(m).__name__ for m in mcp.middleware]
    assert "_ContextInjectionMiddleware" in names, names
    ci = names.index("_ContextInjectionMiddleware")
    if "ArgRepairMiddleware" in names:
        assert ci < names.index("ArgRepairMiddleware"), (
            "ContextInjection must precede ArgRepair so injected identity "
            f"params are stripped first; got order {names}"
        )
