"""MCP error contract (Wave U, U4).

A failed tool call is an MCP error (``isError``) whose text is the JSON envelope:

* ``ArgRepairMiddleware`` raises ``StructuredToolError(help_payload)`` (no ``ToolResult``);
* ``ErrorContractMiddleware`` (innermost) turns any result dict with ``error is True`` into
  ``StructuredToolError(envelope)``: one rule for every tool;
* ``RunRecordMiddleware`` seals ``failure.envelope``, so a dict-error call has the same output
  digest as when it returned the dict (the golden below was produced at tag v2.3.7);
* no ``_run_id`` is injected on an error.
"""
from __future__ import annotations

import asyncio
import json

import mcp.types as mcp_types
import pytest
from fastmcp import Client, FastMCP

from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

SID = "ec-session"

# Output digest of {"error": True, "code": "NOPE", "message": "refused for a reason"}, sealed by
# RunRecordMiddleware at tag v2.3.7 (git-archive scratch tree, same synthetic tool and call).
V237_DICT_ERROR_DIGEST = "sha256:c920171390655dee184d966f29bb69440e28b337a79872f24eb396254f9f8df3"
ERROR_DICT = {"error": True, "code": "NOPE", "message": "refused for a reason"}


def call(server, name, arguments=None):
    handler = server._mcp_server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments or {}),
    )
    result = asyncio.run(handler(req))
    root = result.root if hasattr(result, "root") else result
    text = root.content[0].text if root.content else ""
    return root.isError, json.loads(text), root


def rows():
    return list((HydroSession.load(SID).get("_run_log") or {}).values())


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)
    from ai_hydro.session import chat_binding

    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    HydroSession(SID).save()
    rr.reset_stats()
    return tmp_path


@pytest.fixture
def server(env):
    from ai_hydro.mcp import app
    from ai_hydro.mcp.arg_repair import install_arg_repair
    from ai_hydro.mcp.error_contract import install_error_contract

    srv = FastMCP(name="probe")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())

    @srv.tool()
    def error_dict_tool(session_id: str) -> dict:
        return dict(ERROR_DICT)

    @srv.tool()
    def ok_tool(session_id: str) -> dict:
        return {"data": {"x": 1}}

    @srv.tool()
    def error_false_tool(session_id: str) -> dict:
        return {"error": False, "data": {"x": 1}}

    @srv.tool()
    def error_text_tool(session_id: str) -> dict:
        return {"error": "legacy string error", "data": {}}

    @srv.tool()
    def list_tool(session_id: str) -> list:
        return [1, 2]

    @srv.tool(name="needs_args")
    def needs_args(variable: str, session_id: str) -> dict:
        return {"data": {"v": variable}}

    async def _list_tools():            # app.py adds this facade to the real server (FastMCP 2.14 dropped it)
        return list((await srv.get_tools()).values())

    srv.list_tools = _list_tools
    install_arg_repair(srv)
    install_error_contract(srv)   # after ArgRepair: innermost
    return srv


def test_app_registers_error_contract_innermost():
    from ai_hydro.mcp import app  # noqa: F401  (registers the middleware chain)
    from ai_hydro.mcp import mcp

    kinds = [type(m).__name__ for m in mcp.middleware]
    assert kinds.index("RunRecordMiddleware") < kinds.index("ArgRepairMiddleware") < kinds.index("ErrorContractMiddleware")
    assert kinds[-1] == "ErrorContractMiddleware"


def test_dict_error_becomes_mcp_error_with_the_same_envelope(server):
    is_error, body, root = call(server, "error_dict_tool", {"session_id": SID})
    assert is_error is True
    assert body == ERROR_DICT
    assert "_run_id" not in body


def test_dict_error_sealed_digest_equals_v237_golden(server):
    call(server, "error_dict_tool", {"session_id": SID})
    (row,) = rows()
    assert row["record"]["status"] == "error"
    assert row["record"]["output_digest"] == V237_DICT_ERROR_DIGEST
    assert row["error_summary"] == "NOPE"
    from aihydro_core.records import digest
    assert row["record"]["output_digest"] == digest(ERROR_DICT)


def test_success_and_non_true_error_results_are_unchanged(server):
    is_error, body, _ = call(server, "ok_tool", {"session_id": SID})
    assert is_error is False and body["data"] == {"x": 1} and body["_run_id"]
    is_error, body, _ = call(server, "error_false_tool", {"session_id": SID})
    assert is_error is False and body["error"] is False
    is_error, body, _ = call(server, "error_text_tool", {"session_id": SID})
    assert is_error is False and body["error"] == "legacy string error"
    is_error, body, _ = call(server, "list_tool", {"session_id": SID})
    assert is_error is False and body == [1, 2]


def test_bad_arguments_give_is_error_help_and_sealed_envelope_digest(server):
    is_error, body, _ = call(server, "needs_args", {"bogus": 1, "session_id": SID})
    assert is_error is True
    assert body["error"] is True and body["tool"] == "needs_args"
    assert body["example_call"]["tool"] == "needs_args"
    assert "_run_id" not in body
    (row,) = rows()
    from aihydro_core.records import digest
    assert row["record"]["status"] == "error"
    assert row["record"]["output_digest"] == digest(body)


def test_real_server_data_fetch_bad_args(env):
    """The shipped chain end to end, through the in-memory client."""
    from ai_hydro.mcp import mcp

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool("data_fetch", {"bogus": 1, "session_id": SID}, raise_on_error=False)

    res = asyncio.run(go())
    assert res.is_error is True
    body = json.loads(res.content[0].text)          # clients read the error text, not structured_content
    assert body["error"] is True and "example_call" in body and body["example_call"]["tool"] == "data_fetch"
    assert res.structured_content is None            # no consumer may rely on it for an error
    (row,) = rows()
    from aihydro_core.records import digest
    assert row["record"]["status"] == "error"
    assert row["record"]["output_digest"] == digest(body)


def test_arg_repair_module_has_no_toolresult_import():
    import inspect
    from ai_hydro.mcp import arg_repair

    assert "import ToolResult" not in inspect.getsource(arg_repair)
    assert "fastmcp.tools.tool" not in inspect.getsource(arg_repair)


def test_c1_arm_still_strips_advisory_fields_from_a_dict_error(server, monkeypatch):
    """Eval arm C1 used to strip advisory fields from a returned error dict; the envelope must be stripped too."""
    from ai_hydro.mcp.eval_condition import _scrub_exception, hidden_name_pattern
    from ai_hydro.mcp.errors import StructuredToolError

    exc = StructuredToolError({"error": True, "code": "X", "quality_flags": ["f"], "skeptic_note": "n", "keep": 1})
    out = _scrub_exception(exc, hidden_name_pattern("C1", []), strip=True)
    assert out.envelope == {"error": True, "code": "X", "keep": 1}
    assert _scrub_exception(exc, hidden_name_pattern("C1", []), strip=False).envelope["quality_flags"] == ["f"]
