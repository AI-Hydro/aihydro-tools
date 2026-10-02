"""
RunRecordMiddleware (ADR-001, slice 1a): every recorded tool call that resolves
a session gets a sealed ``aihydro.run/2`` record on its run-log row, and the
middleware never breaks a call.

Calls go through the real low-level MCP ``CallToolRequest`` handler, the same
path the extension hits, so argument validation and the middleware chain are
exercised. Synthetic tools run on a throwaway server built with the same two
middlewares in the same order as ``ai_hydro.mcp.app``.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import mcp.types as mcp_types
import pytest
from fastmcp import FastMCP

from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

import fastmcp

# aihydro-tools pins fastmcp<3 and its internal call shims need the 2.x API. On
# a 3.x interpreter the real server's tools fail before reaching our code, so
# the real-tool tests are skipped there; the synthetic-tool tests still run.
needs_fastmcp2 = pytest.mark.skipif(
    int(fastmcp.__version__.split(".")[0]) >= 3,
    reason="real ai_hydro tools need the pinned FastMCP 2.x API",
)

SID = "mw-session"
CHAT = "01KV1EPM88P9FV5CWNVJ9V60JY"


# --------------------------------------------------------------------- helpers
def call(server, name, arguments=None):
    """``(is_error, parsed_json_or_text)`` through the real request handler."""
    handler = server._mcp_server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments or {}),
    )
    result = asyncio.run(handler(req))
    root = result.root if hasattr(result, "root") else result
    text = root.content[0].text if root.content else ""
    try:
        return root.isError, json.loads(text), root
    except ValueError:
        return root.isError, text, root


def run_log(session_id=SID):
    return HydroSession.load(session_id).get("_run_log") or {}


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
    """A throwaway server wired like ai_hydro.mcp.app (injection, then recording)."""
    from ai_hydro.mcp import app
    from ai_hydro.mcp.enforcement import post_run
    from ai_hydro.mcp.helpers import _session_store

    srv = FastMCP(name="probe")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())

    @srv.tool()
    def ok_tool(x: int = 1, session_id: str | None = None) -> dict:
        return {"data": {"x": x}}

    @srv.tool()
    def post_run_tool(session_id: str) -> dict:
        return post_run("post_run_tool", session_id, {"data": {"nse": 0.8}}, inputs={"k": 1})

    @srv.tool()
    def both_writers_tool(session_id: str) -> dict:
        res = {"data": {"nse": 0.8, "series": [1, 2, 3]}, "meta": {"version": "7.1.0"}}
        _session_store(session_id, "model", res, tool_name="both_writers_tool")
        return post_run("both_writers_tool", session_id, {"data": {"nse": 0.8}, "meta": {"version": "7.1.0"}})

    @srv.tool()
    def failing_tool(session_id: str) -> dict:
        raise ValueError("boom")

    @srv.tool()
    def error_dict_tool(session_id: str) -> dict:
        return {"error": True, "code": "NOPE", "message": "refused for a reason"}

    @srv.tool()
    def lineage_tool(session_id: str) -> dict:
        from aihydro_core.records import digest, input_ref

        rr.declare_lineage(parents=["up.1"], input_refs=[input_ref("up.1", digest({"u": 1}), "upstream_output")],
                           note="hello")
        return {"data": {"y": 2}}

    @srv.tool()
    def get_session_summary(session_id: str | None = None) -> dict:      # exempt by name
        return {"data": {"ok": True}}

    @srv.tool()
    def list_tool(session_id: str) -> list:
        return [1, 2, 3]

    @srv.tool()
    def str_tool(session_id: str) -> str:
        return "plain text result"

    @srv.tool()
    def big_tool(session_id: str) -> dict:
        return {"data": {"blob": "z" * 200}}

    return srv


# ---------------------------------------------------------------- registration
def test_middleware_is_registered_inside_context_injection():
    from ai_hydro.mcp import app

    kinds = [type(m).__name__ for m in app.mcp.middleware]
    assert kinds.index("_ContextInjectionMiddleware") < kinds.index("RunRecordMiddleware")


# ------------------------------------------------------------------ real tools
@needs_fastmcp2
def test_real_tool_call_gets_a_verified_record(env):
    from ai_hydro.mcp.app import mcp

    is_error, body, _ = call(mcp, "add_note", {"session_id": SID, "note": "hello"})
    assert not is_error and "_record_error" not in body
    rows = run_log()
    assert len(rows) == 1
    (row,) = rows.values()
    rec = row["record"]
    assert rec["schema"] == "aihydro.run/2" and rec["tool"] == "add_note"
    assert rr.verify_run_log_entry(row) == {
        "has_record": True, "record_ok": True, "entry_ok": True, "record_error": None}
    assert rec["session_id"] == SID and rec["status"] == "ok"
    assert rec["input_digest"] and rec["output_digest"] and rec["env_digest"]
    assert rec["extra"]["writer"] == "middleware"


@needs_fastmcp2
def test_chat_binding_resolves_the_session_without_a_session_id(env):
    """_chat_id is stripped by the injection middleware and still visible to
    the recorder, which runs inside it."""
    from ai_hydro.mcp.app import mcp
    from ai_hydro.session.chat_binding import get_binding_store

    get_binding_store().bind(CHAT, SID)
    is_error, body, _ = call(mcp, "add_note", {"note": "via chat", "_chat_id": CHAT})
    assert not is_error
    (row,) = run_log().values()
    assert row["record"]["tool"] == "add_note"


@needs_fastmcp2
def test_exempt_tool_is_not_recorded_and_is_counted(env):
    from ai_hydro.mcp.app import mcp

    is_error, _, _ = call(mcp, "get_session_summary", {"session_id": SID})
    assert not is_error
    assert run_log() == {}
    assert rr.stats_snapshot().get("exempt") == 1


# ------------------------------------------------------------- synthetic tools
def test_no_session_is_counted_and_nothing_is_written(server, env):
    is_error, body, _ = call(server, "ok_tool", {"x": 5})
    assert not is_error and body["data"] == {"x": 5} and "_record_error" not in body
    assert rr.stats_snapshot().get("no_session") == 1
    assert not list(env.glob("*.runlog.sqlite3"))


def test_unknown_session_id_is_not_created_by_recording(server, env):
    call(server, "ok_tool", {"session_id": "does-not-exist"})
    assert rr.stats_snapshot().get("no_session") == 1
    assert not (env / "does-not-exist.runlog.sqlite3").exists()


def test_minimal_row_is_created_when_the_tool_wrote_none(server):
    is_error, body, _ = call(server, "ok_tool", {"x": 2, "session_id": SID})
    assert not is_error
    (row,) = run_log().values()
    assert row["minimal"] is True and row["tool_name"] == "ok_tool"
    assert row["record"]["extra"]["entry"] == "minimal"
    assert rr.verify_run_log_entry(row)["record_ok"] is True


def test_post_run_row_gets_the_record_and_matches_the_returned_run_id(server):
    is_error, body, _ = call(server, "post_run_tool", {"session_id": SID})
    assert not is_error
    rows = run_log()
    assert list(rows) == [body["_run_id"]]
    row = rows[body["_run_id"]]
    assert row["key_outputs"] == {"nse": 0.8} and row["inputs"] == {"k": 1}     # tool's own shape kept
    assert row["record"]["run_id"] == body["_run_id"]
    assert row["record"]["extra"]["writer"] == "post_run"
    assert rr.verify_run_log_entry(row)["entry_ok"] is True


def test_every_row_a_call_wrote_is_recorded_and_labelled(server):
    is_error, body, _ = call(server, "both_writers_tool", {"session_id": SID})
    assert not is_error
    rows = run_log()
    assert len(rows) == 2                                  # put_result row + post_run row
    primary = body["_run_id"]
    for run_id, row in rows.items():
        assert rr.verify_run_log_entry(row)["record_ok"] is True
        assert row["record"]["tool"] == "both_writers_tool"
        assert row["record"]["tool_version"] == "7.1.0"
        assert row["record"]["version_source"] == "result_meta"
        if run_id == primary:
            assert row["record"]["extra"]["writer"] == "post_run"
            assert "call_run_id" not in row["record"]["extra"]
        else:
            assert row["record"]["extra"]["writer"] == "put_result"
            assert row["record"]["extra"]["call_run_id"] == primary


def test_slot_result_carries_the_run_id_of_the_row_that_describes_it(server):
    call(server, "both_writers_tool", {"session_id": SID})
    slot = HydroSession.load(SID).get("model")
    stamped = slot["meta"]["run_id"]
    assert stamped in run_log() and run_log()[stamped]["slot"] == "model"


def test_failed_call_is_recorded_as_evidence_and_the_error_propagates(server):
    is_error, body, _ = call(server, "failing_tool", {"session_id": SID})
    assert is_error is True and "boom" in str(body)
    (row,) = run_log().values()
    assert row["record"]["status"] == "error" and row["error"] is True
    assert "boom" in row["error_summary"]


def test_error_dict_result_is_recorded_as_error_and_left_unchanged(server):
    is_error, body, _ = call(server, "error_dict_tool", {"session_id": SID})
    assert body == {"error": True, "code": "NOPE", "message": "refused for a reason"}
    (row,) = run_log().values()
    assert row["record"]["status"] == "error" and row["error_summary"] == "NOPE"


def test_declared_lineage_lands_in_the_record(server):
    call(server, "lineage_tool", {"session_id": SID})
    (row,) = run_log().values()
    rec = row["record"]
    assert rec["parents"] == ["up.1"]
    assert [r["ref"] for r in rec["input_refs"]] == ["up.1"]
    assert rec["input_refs"][0]["role"] == "upstream_output" and rec["input_refs"][0]["digest"].startswith("sha256:")
    assert rec["extra"]["note"] == "hello"


def test_declare_lineage_outside_a_call_is_a_noop():
    assert rr.declare_lineage(parents=["x"]) is False


def test_result_is_unchanged_except_for_record_error(server, env):
    from ai_hydro.mcp import app

    bare = FastMCP(name="bare")
    bare.add_middleware(app._ContextInjectionMiddleware())

    @bare.tool()
    def ok_tool(x: int = 1, session_id: str | None = None) -> dict:
        return {"data": {"x": x}}

    with_mw = call(server, "ok_tool", {"x": 3, "session_id": SID})
    without = call(bare, "ok_tool", {"x": 3, "session_id": SID})
    assert with_mw[1] == without[1]
    assert with_mw[2].structuredContent == without[2].structuredContent


# --------------------------------------------------------- failure visibility
def test_record_error_from_the_record_is_visible_in_the_result(server):
    with patch.object(rr, "MAX_DIGEST_BYTES", 10):
        is_error, body, root = call(server, "big_tool", {"session_id": SID})
    assert not is_error
    assert "skipped" in body["_record_error"]
    assert "_record_error" in (root.structuredContent or {})
    assert body["data"]["blob"] == "z" * 200                       # nothing else altered
    (row,) = run_log().values()
    assert "output_digest" not in row["record"] and row["record"]["record_error"]


def test_store_refusal_is_visible_and_does_not_fail_the_call(server):
    real = store._run_log_record

    def refuse_middleware(session_id, run_id, entry, *, writer=None, **kw):
        if writer == "middleware":
            return "refused"
        return real(session_id, run_id, entry, writer=writer, **kw)

    with patch.object(store, "_run_log_record", refuse_middleware):
        is_error, body, _ = call(server, "post_run_tool", {"session_id": SID})
    assert not is_error and body["data"] == {"nse": 0.8}
    assert "record_not_stored (refused)" in body["_record_error"]
    assert "record" not in run_log()[body["_run_id"]]


def test_a_crash_inside_recording_never_breaks_the_tool_call(server):
    with patch.object(rr, "record_call", side_effect=RuntimeError("recorder exploded")):
        is_error, body, _ = call(server, "ok_tool", {"x": 9, "session_id": SID})
    assert not is_error and body["data"] == {"x": 9}
    assert "recorder exploded" in body["_record_error"]


def test_recording_unavailable_degrades_to_a_plain_call(server, monkeypatch):
    """e.g. aihydro-core without aihydro_core.records: the import fails, the call goes through."""
    import sys

    import ai_hydro.session as session_pkg

    monkeypatch.delattr(session_pkg, "run_records")
    with patch.dict(sys.modules, {"ai_hydro.session.run_records": None}):
        is_error, body, _ = call(server, "ok_tool", {"x": 4, "session_id": SID})
    assert not is_error and body == {"data": {"x": 4}}
    assert run_log() == {}


@pytest.mark.parametrize("tool,expected", [("list_tool", [1, 2, 3]), ("str_tool", "plain text result")])
def test_list_and_string_results_are_digested(server, tool, expected):
    from aihydro_core.records import digest

    is_error, body, _ = call(server, tool, {"session_id": SID})
    assert not is_error
    (row,) = run_log().values()
    assert row["record"]["output_digest"] == digest(expected)
    assert "record_error" not in row["record"]


def test_map_cli_delineation_is_recorded(env, monkeypatch):
    import argparse

    import ai_hydro.mcp.tools_analysis as ta
    from ai_hydro import hydro_map_cli
    from ai_hydro.session.store import HydroSession as HS

    def fake(session_id=None, **kw):
        sid = "cli-session"
        HS(sid).save()
        HS.load(sid).put_result("watershed", "f", "k", {"data": {"area_km2": 12.0}, "meta": {"tool": "delineate_watershed_from_point"}})
        return {"data": {"area_km2": 12.0, "method_used": "stub"}, "_run_id": None}

    monkeypatch.setattr(ta, "delineate_watershed_from_point", fake)
    args = argparse.Namespace(session_id=None, method="3dep", lat=40.0, lon=-86.0, workspace_dir=None,
                              expected_area_km2=None, name=None)
    out = hydro_map_cli.cmd_delineate_point(args)
    assert out["ok"] is True
    rows = HydroSession.load("cli-session").get("_run_log")
    recs = [r["record"] for r in rows.values() if "record" in r]
    assert recs and all(r["tool"] == "delineate_watershed_from_point" for r in recs)
    assert recs[0]["extra"]["mcp_client"] == "direct_call"
    assert rr.verify_run_log_entry(next(iter(rows.values())))["record_ok"]
