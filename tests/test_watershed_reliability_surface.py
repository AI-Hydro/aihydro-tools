"""The delineation's reliability verdict (aihydro-watershed ``reliability`` / ``reliability_reason`` / ``remedy``) reaches the
human note, the map layer metadata and every downstream tool's result (a warning, never a refusal). Stubbed watershed results;
a watershed without the keys (older aihydro-watershed) changes nothing."""
from __future__ import annotations

import asyncio
import json

import fastmcp
import pytest
from fastmcp import Client

from ai_hydro.mcp import watershed_reliability as wr
from test_run_record_middleware import needs_fastmcp2
from test_train_289_fixes import _fn, point_router  # noqa: F401  (fixture)

UNRELIABLE = {
    "reliability": "unreliable",
    "reliability_reason": "outlet could not be snapped (offline_unsnapped)",
    "remedy": ["Sign in to Google Earth Engine.", "Or delineate with method='merit_hybrid'."],
    "estimated_area_km2_on_reach": 812.0,
    "reliability_checks": {"status": "unreliable"},
}


# ------------------------------------------------------------------ pure helpers
def test_helpers_are_noops_without_the_keys():
    for data in ({}, {"area_km2": 5}, {"reliability": None}, {"reliability": "weird"}, None):
        assert wr.reliability_of(data) is None
        assert wr.reliability_note(data) == "" and wr.layer_metadata(data) == {}


def test_note_and_metadata_state_reason_and_remedy():
    note = wr.reliability_note(UNRELIABLE)
    assert "UNRELIABLE" in note and "offline_unsnapped" in note and "Sign in to Google Earth Engine" in note
    md = wr.layer_metadata(UNRELIABLE)
    assert md["reliability"] == "unreliable" and "offline_unsnapped" in md["reliability_reason"] and "merit_hybrid" in md["remedy"]
    assert wr.layer_metadata({"reliability": "provisional", "reliability_reason": "x"}) == {
        "reliability": "provisional", "reliability_reason": "x"}


# ------------------------------------------------------------------ delineate_watershed_from_point
def _run(point_router, monkeypatch, sid, **extra):  # noqa: F811
    import ai_hydro.mcp.map_events as me
    from ai_hydro.mcp import tools_analysis as ta

    _, ex = point_router
    ex.update(extra)
    layers = []
    monkeypatch.setattr(me, "push_layer", lambda *a, **k: layers.append(k) or True)
    return _fn(ta.delineate_watershed_from_point)(lat=35.5, lon=-99.5, session_id=sid), layers


def test_unreliable_delineation_states_reliability_in_note_and_layer(point_router, monkeypatch):  # noqa: F811
    out, layers = _run(point_router, monkeypatch, "wr-unrel", **UNRELIABLE)
    assert not out.get("error"), out
    assert out["data"]["reliability"] == "unreliable"
    assert "RELIABILITY UNRELIABLE" in out["_note"] and "offline_unsnapped" in out["_note"] and "Remedy:" in out["_note"]
    md = layers[0]["metadata"]
    assert md["reliability"] == "unreliable" and "offline_unsnapped" in md["reliability_reason"]


def test_older_watershed_without_the_keys_is_unchanged(point_router, monkeypatch):  # noqa: F811
    out, layers = _run(point_router, monkeypatch, "wr-old")
    assert "RELIABILITY" not in out["_note"]
    assert not {"reliability", "reliability_reason", "remedy"} & set(layers[0]["metadata"])


def test_reliable_delineation_with_none_says_nothing(point_router, monkeypatch):  # noqa: F811
    out, layers = _run(point_router, monkeypatch, "wr-none", reliability=None, reliability_reason=None, remedy=None)
    assert "RELIABILITY" not in out["_note"] and "reliability" not in layers[0]["metadata"]


# ------------------------------------------------------------------ downstream warning (through the MCP client)
@pytest.fixture
def server(tmp_path, monkeypatch):
    from ai_hydro.mcp import app
    from ai_hydro.session import chat_binding, store
    from ai_hydro.session.chat_binding import ChatBindingStore

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    (tmp_path / "sessions").mkdir()
    srv = fastmcp.FastMCP(name="wr")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())
    srv.add_middleware(app.WatershedReliabilityMiddleware())

    for name in ("compute_twi", "fetch_forcing_data", "lsh_attributes", "compute_flow_duration_curve"):
        def make(n):
            def tool(session_id: str) -> dict:
                return {"data": {"tool": n, "value": 1.0}, "quality_flags": []}
            tool.__name__ = n
            return tool
        srv.tool(name=name)(make(name))

    @srv.tool()
    def failing(session_id: str) -> dict:
        return {"error": True, "code": "X", "message": "m"}

    return srv


def _session(sid, **watershed_data):
    from ai_hydro.session.store import HydroSession

    s = HydroSession(sid)
    if watershed_data is not None:
        s.set("watershed", {"data": {"area_km2": 5.0, **watershed_data}, "meta": {}})
    s.save()


def _call(srv, tool, sid):
    async def go():
        async with Client(srv) as c:
            return await c.call_tool(tool, {"session_id": sid}, raise_on_error=False)

    return json.loads(asyncio.run(go()).content[0].text)


@needs_fastmcp2
@pytest.mark.parametrize("tool", ["compute_twi", "fetch_forcing_data", "lsh_attributes"])
def test_downstream_tools_warn_on_an_unreliable_watershed_and_still_run(server, tool):
    _session("wr-s1", **UNRELIABLE)
    r = _call(server, tool, "wr-s1")
    assert not r.get("error") and r["data"]["value"] == 1.0                      # warned, not refused
    flag = next(f for f in r["quality_flags"] if f["validator"] == "watershed_reliability")
    assert flag["status"] == "warning" and flag["severity"] == "high" and "offline_unsnapped" in flag["message"]
    assert "Sign in to Google Earth Engine" in flag["recommendation"]
    assert "UNRELIABLE" in r["watershed_reliability_warning"]


@needs_fastmcp2
def test_the_warning_is_in_the_sealed_run_record(server):
    from aihydro_core.records import RunRecord
    from ai_hydro.session import store

    _session("wr-s2", **UNRELIABLE)
    _call(server, "compute_twi", "wr-s2")
    recs = [r["record"] for r in store._run_log_read_all("wr-s2").values()
            if isinstance(r, dict) and (r.get("record") or {}).get("tool") == "compute_twi"]
    assert len(recs) == 1 and recs[0]["extra"]["watershed_reliability"] == "unreliable"
    assert RunRecord.from_dict(recs[0]).verify()
    _session("wr-s6")
    _call(server, "compute_twi", "wr-s6")
    clean = [r["record"] for r in store._run_log_read_all("wr-s6").values()
             if isinstance(r, dict) and (r.get("record") or {}).get("tool") == "compute_twi"]
    assert "watershed_reliability" not in clean[0]["extra"]


@needs_fastmcp2
@pytest.mark.parametrize("data", [{}, {"reliability": None}, {"reliability": "provisional", "reliability_reason": "x"}])
def test_no_warning_without_an_unreliable_mark(server, data):
    _session("wr-s3", **data)
    r = _call(server, "compute_twi", "wr-s3")
    assert r["quality_flags"] == [] and "watershed_reliability_warning" not in r


@needs_fastmcp2
def test_no_warning_for_other_tools_errors_or_a_session_without_a_watershed(server):
    _session("wr-s4", **UNRELIABLE)
    assert "watershed_reliability_warning" not in _call(server, "compute_flow_duration_curve", "wr-s4")
    assert "watershed_reliability_warning" not in _call(server, "failing", "wr-s4")
    from ai_hydro.session.store import HydroSession
    HydroSession("wr-s5").save()
    assert "watershed_reliability_warning" not in _call(server, "compute_twi", "wr-s5")

