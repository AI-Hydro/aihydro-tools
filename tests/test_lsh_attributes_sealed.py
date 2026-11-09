"""``lsh_attributes`` (aihydro-lsh >= 0.4.1, trailing optional ``session_id``) is sealed into the run record.

No per-tool rule is needed: ``resolve_call_session_rule`` reads ``arguments["session_id"]`` for every tool, so a
tool that takes the argument is sealed (see ``test_run_record_coverage``). These tests pin that for the lsh shape:
a stub with ``lsh_attributes``' signature and string return, driven through the real request handler and the same
middleware order as ``ai_hydro.mcp.app``. The release-gated checks on the installed plugin skip when it is older.
"""
from __future__ import annotations

import asyncio
import json

import fastmcp
import pytest
from fastmcp import FastMCP

from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

from test_run_record_middleware import call, run_log

needs_fastmcp2 = pytest.mark.skipif(
    int(fastmcp.__version__.split(".")[0]) >= 3,
    reason="real ai_hydro tools need the pinned FastMCP 2.x API",
)

SID = "lsh-sealed"


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)
    from ai_hydro.session import chat_binding

    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    HydroSession(SID).save()
    rr.reset_stats()
    from ai_hydro.mcp import app

    srv = FastMCP(name="lsh-probe")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())

    @srv.tool()
    def lsh_attributes(
        geometry_geojson: str, start: str = "1990-01-01", end: str = "2020-12-31", families: str = "",
        gauge_id: str = "", include_trends: bool = False, session_id: str = "",
    ) -> str:
        # aihydro-lsh returns a JSON string; the session id is accepted and unused by lsh itself.
        return json.dumps({"data": {"n_attributes": 107}})

    return srv


def test_lsh_attributes_with_session_id_is_sealed(server):
    err, _body, _raw = call(server, "lsh_attributes",
                            {"geometry_geojson": '{"lat": 39.27, "lon": -77.54}', "session_id": SID})
    assert not err
    rows = run_log(SID)
    assert len(rows) == 1
    (row,) = rows.values()
    assert row["record"]["tool"] == "lsh_attributes"
    assert row["record"]["schema"] == "aihydro.run/2"
    check = rr.verify_run_log_entry(row)
    assert check["has_record"] and check["record_ok"] and check["entry_ok"]
    assert rr.stats_snapshot().get("no_session", 0) == 0


def test_lsh_attributes_without_session_id_is_not_sealed(server):
    """The unbound call is the old behaviour (counted as no_session), not an error."""
    err, _body, _raw = call(server, "lsh_attributes", {"geometry_geojson": '{"lat": 39.27, "lon": -77.54}'})
    assert not err
    assert run_log(SID) == {}
    assert rr.stats_snapshot().get("no_session") == 1


@needs_fastmcp2
def test_installed_lsh_attributes_session_id_is_trailing_and_optional():
    """When the installed aihydro-lsh has the argument (>= 0.4.1) it must be optional, so older callers still work."""
    from ai_hydro.mcp import mcp as real

    tools = asyncio.run(real.get_tools())
    if "lsh_attributes" not in tools:
        pytest.skip("aihydro-lsh not installed")
    schema = tools["lsh_attributes"].parameters
    if "session_id" not in schema.get("properties", {}):
        pytest.skip("installed aihydro-lsh predates 0.4.1 (no session_id on lsh_attributes)")
    assert "session_id" not in schema.get("required", [])
