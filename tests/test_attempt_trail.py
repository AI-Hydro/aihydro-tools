"""The attempt trail (AGU claim C10): which sources a call tried, which served, the limitations.

Offline. A call that reports a ``fallback_history`` gets ``extra.attempt_trail`` in its sealed record, on
success and on failure, and the trail survives the capsule export. A call that reports none gets a record
exactly as before. Fixtures drive the real middleware and the real exporter; the forced fallback runs the
real aihydro-data pipeline with its single network seam (``_fetch_one``) replaced.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import fastmcp
import mcp.types as mcp_types
import pytest
from fastmcp import FastMCP

from aihydro_core import ToolError
from aihydro_core.records import RunRecord

from ai_hydro.capsule.manifest import MANIFEST_FILE, build_manifest
from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

needs_fastmcp2 = pytest.mark.skipif(
    int(fastmcp.__version__.split(".")[0]) >= 3,
    reason="uses the real middleware chain on the pinned FastMCP 2.x API",
)

SID = "attempt-trail"


def _call(server, name, arguments):
    handler = server._mcp_server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call", params=mcp_types.CallToolRequestParams(name=name, arguments=arguments))
    result = asyncio.run(handler(req))
    root = result.root if hasattr(result, "root") else result
    return json.loads(root.content[0].text)


def _history_of_failure():
    """What aihydro-watershed (with its attempt-trail change) attaches to a total delineation failure."""
    return [
        {"method": "merit_gee", "outcome": "failed", "reason": "GEE_MEMORY_LIMIT"},
        {"method": "local_merit_flowdir_pyflwdir", "outcome": "failed",
         "reason": "raster not staged at /Users/someone/data/merit/n30e070_dir.tif"},
        {"method": "merit_basins_hybrid", "outcome": "failed",
         "reason": "MERIT_BASINS_NOT_STAGED token=abc123secret"},
    ]


@pytest.fixture
def world(tmp_path, monkeypatch):
    from ai_hydro.mcp import app
    from ai_hydro.mcp.helpers import _tool_error_to_dict
    from ai_hydro.session import chat_binding

    monkeypatch.setenv("HOME", str(tmp_path / "home"))           # aihydro-data cache is isolated
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    (tmp_path / "sessions").mkdir()
    HydroSession(SID).save()

    server = FastMCP(name="trail")
    server.add_middleware(app._ContextInjectionMiddleware())
    server.add_middleware(app.RunRecordMiddleware())

    @server.tool()
    def data_fetch(session_id: str, variable: str = "precipitation") -> dict:
        """The real unified fetch, so the real FetchResult.fallback_history reaches the middleware."""
        from ai_hydro.mcp.tools_data_fetch import data_fetch as real
        return real(variable, "2020-06-01", "2020-06-03", geometry="POINT(-90 40)",
                    cache=False, session_id=session_id)

    @server.tool()
    def delineate_watershed_from_point(session_id: str, lat: float = 28.4, lon: float = 77.2,
                                       mode: str = "total_failure") -> dict:
        """Same shape as the real tool: a ToolError becomes an error dict, a success a data dict."""
        try:
            if mode == "total_failure":
                raise ToolError(
                    code="DELINEATION_FAILED",
                    message="All tiers failed. Fast: skipped. MERIT-Basins: MERIT_BASINS_NOT_STAGED",
                    tool="aihydro_watershed.delineation.router.delineate_from_point",
                    details={"fallback_history": _history_of_failure()},
                )
            from ai_hydro.mcp.enforcement import post_run
            data = {
                "area_km2": 12.5, "method_used": "merit_basins_hybrid",
                "quality_flags": ["MERIT_BASINS_NOT_STAGED", "RAW_DEM_EXPERIMENTAL_FALLBACK"],
                "fallback_history": [
                    {"method": "merit_gee", "outcome": "failed", "reason": "GEE_MEMORY_LIMIT"},
                    {"method": "merit_basins_hybrid", "outcome": "succeeded", "reason": "gee_failed"},
                ],
            }
            return post_run("delineate_watershed_from_point", session_id, {"data": data, "meta": {}})
        except Exception as exc:
            return _tool_error_to_dict(exc)

    @server.tool()
    def extract_hydrological_signatures(session_id: str, q_mean: float = 2.5) -> dict:
        from ai_hydro.mcp.enforcement import post_run
        return post_run("extract_hydrological_signatures", session_id,
                        {"data": {"q_mean": q_mean, "baseflow_index": 0.41}, "meta": {}})

    return server, tmp_path


def _records(session_id: str = SID) -> list[dict]:
    log = store._run_log_read_all(session_id)
    return [row["record"] for row in log.values() if isinstance(row, dict) and row.get("record")]


def _fake_fetch_one_factory(fail_first: int, fail_all: bool = False):
    import pandas as pd

    from aihydro_data.contracts import FetchResult

    calls = {"n": 0}

    def _fake(spec, geom, start, end, agg, req, **kw):
        calls["n"] += 1
        if fail_all or calls["n"] <= fail_first:
            raise RuntimeError(f"{spec.id} boom at /Users/someone/cache/x.nc")
        df = pd.DataFrame({"date": pd.date_range("2020-06-01", periods=3), "precipitation": [1.0, 2.0, 3.0]})
        return FetchResult(variable=spec.variable, product=spec.id, source=spec.source, request=req, data=df,
                           license=spec.license, citation=spec.citation)

    return _fake


def _export(tmp_path: Path) -> Path:
    import ai_hydro.mcp.tools_session as ts

    result = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "capsule"))
    assert "error" not in result, result
    return Path(result["capsule_dir"])


def _replay(capsule: Path, *args: str):
    proc = subprocess.run([sys.executable, str(capsule / "replay.py"), *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def _rebuild_manifest(capsule: Path):
    (capsule / MANIFEST_FILE).write_text(json.dumps(build_manifest(capsule), indent=2))


# ------------------------------------------------------------------ the helper
def test_trail_is_none_without_history():
    assert rr.attempt_trail({"data": {"q_mean": 1.0}, "quality_flags": []}) is None
    assert rr.attempt_trail({"error": True, "code": "X", "message": "m"}) is None
    assert rr.attempt_trail(None) is None


def test_trail_normalises_bounds_and_scrubs():
    history = [{"product": f"P{i}", "source": "prov", "outcome": "failed",
                "reason": "x" * 900 + " /Users/someone/secret/file.nc api_key=hunter2"} for i in range(40)]
    history.append({"product": "SERVED", "source": "prov", "outcome": "served", "reason": ""})
    t = rr.attempt_trail({"fallback_history": history, "data": {"quality_flags": ["FLAG_A", "FLAG_A", "FLAG_B"]}})
    assert len(t["attempts"]) == rr.MAX_TRAIL_ATTEMPTS and t["attempts_omitted"] == 41 - rr.MAX_TRAIL_ATTEMPTS
    assert all(set(a) <= {"source", "outcome", "reason", "provider"} for a in t["attempts"])
    assert all(len(a["reason"]) <= 200 and "/Users/someone" not in a["reason"] for a in t["attempts"])
    assert t["limitations"] == ["FLAG_A", "FLAG_B"]
    t2 = rr.attempt_trail({"fallback_history": [
        {"method": "m1", "outcome": "failed", "reason": "r"},
        {"method": "m2", "outcome": "succeeded_after_auto_stage", "reason": ""}]})
    assert t2["source_used"] == "m2"


def test_unchanged_extra_means_unchanged_digest():
    """A call with no fallback information builds the record it always did."""
    plain = {"data": {"q_mean": 2.5}, "quality_flags": [{"validator": "v", "status": "pass"}]}
    assert rr.attempt_trail(plain) is None
    kwargs = dict(run_id="r.1", tool="extract_hydrological_signatures", session_id="s",
                  arguments={"q_mean": 2.5}, result=plain, extra={"writer": "post_run"})
    import dataclasses

    def sealed(**kw):                       # fix the one wall-clock field so digests are comparable
        rec = rr.build_run_record(**kw)
        return dataclasses.replace(rec, recorded_at="2026-01-01T00:00:00Z").seal()

    before, again = sealed(**kwargs), sealed(**kwargs)
    assert before.record_digest == again.record_digest
    assert "attempt_trail" not in before.extra
    with_trail = sealed(**{**kwargs, "extra": {"writer": "post_run",
                                               "attempt_trail": {"attempts": [], "source_used": None}}})
    assert with_trail.record_digest != before.record_digest      # the key is inside the seal


@needs_fastmcp2
def test_plain_call_record_has_no_trail_key(world):
    server, _ = world
    body = _call(server, "extract_hydrological_signatures", {"session_id": SID})
    recs = [r for r in _records() if r["tool"] == "extract_hydrological_signatures"]
    assert recs and all("attempt_trail" not in r["extra"] for r in recs)
    assert all(RunRecord.from_dict(r).verify() for r in recs) and body["_run_id"]


# ------------------------------------------------------------------ forced fallback
@needs_fastmcp2
def test_forced_fallback_is_in_the_sealed_record_and_tampering_fails_verification(world):
    server, tmp_path = world
    with patch("aihydro_data._pipeline._fetch_one", side_effect=_fake_fetch_one_factory(fail_first=1)):
        body = _call(server, "data_fetch", {"session_id": SID})
    assert not body.get("error"), body
    served = body["product"]

    recs = [r for r in _records() if r["tool"] == "data_fetch"]
    assert len(recs) == 1
    trail = recs[0]["extra"]["attempt_trail"]
    outcomes = [a["outcome"] for a in trail["attempts"]]
    assert outcomes[0] == "failed" and outcomes[-1] == "served" and len(outcomes) >= 2
    assert "boom" in trail["attempts"][0]["reason"] and "/Users/someone" not in json.dumps(trail)
    assert trail["source_used"] == served == trail["attempts"][-1]["source"]
    assert RunRecord.from_dict(recs[0]).verify()

    capsule = _export(tmp_path)
    log = json.loads((capsule / "run_log.json").read_text())
    exported = log[body["_run_id"]]["record"]["extra"]["attempt_trail"]
    assert exported == trail                                       # the capsule carries every attempt
    code, out = _replay(capsule)
    assert code == 0, out

    log[body["_run_id"]]["record"]["extra"]["attempt_trail"]["attempts"][0]["outcome"] = "served"
    (capsule / "run_log.json").write_text(json.dumps(log))
    _rebuild_manifest(capsule)                                    # an editor can also regenerate the manifest
    code, out = _replay(capsule)
    assert code == 1 and "record_digest mismatch" in out, out


@needs_fastmcp2
def test_deleting_an_attempt_fails_verification_too(world):
    server, tmp_path = world
    with patch("aihydro_data._pipeline._fetch_one", side_effect=_fake_fetch_one_factory(fail_first=1)):
        body = _call(server, "data_fetch", {"session_id": SID})
    capsule = _export(tmp_path)
    log = json.loads((capsule / "run_log.json").read_text())
    del log[body["_run_id"]]["record"]["extra"]["attempt_trail"]["attempts"][0]
    (capsule / "run_log.json").write_text(json.dumps(log))
    _rebuild_manifest(capsule)
    code, out = _replay(capsule)
    assert code == 1 and "record_digest mismatch" in out, out


# ------------------------------------------------------------------ success with limitations
@needs_fastmcp2
def test_success_keeps_attempts_source_used_and_limitations(world):
    server, _ = world
    _call(server, "delineate_watershed_from_point", {"session_id": SID, "mode": "ok"})
    rec = next(r for r in _records() if r["tool"] == "delineate_watershed_from_point")
    t = rec["extra"]["attempt_trail"]
    assert [(a["source"], a["outcome"]) for a in t["attempts"]] == [
        ("merit_gee", "failed"), ("merit_basins_hybrid", "succeeded")]
    assert t["source_used"] == "merit_basins_hybrid"
    assert t["limitations"] == ["MERIT_BASINS_NOT_STAGED", "RAW_DEM_EXPERIMENTAL_FALLBACK"]
    assert "error_code" not in t


# ------------------------------------------------------------------ total failure
@needs_fastmcp2
def test_total_failure_record_lists_every_attempt_with_reasons(world):
    server, tmp_path = world
    body = _call(server, "delineate_watershed_from_point", {"session_id": SID})
    assert body["error"] is True and body["code"] == "DELINEATION_FAILED"
    rec = next(r for r in _records() if r["tool"] == "delineate_watershed_from_point")
    assert rec["status"] == "error"
    t = rec["extra"]["attempt_trail"]
    assert [a["source"] for a in t["attempts"]] == ["merit_gee", "local_merit_flowdir_pyflwdir", "merit_basins_hybrid"]
    assert all(a["outcome"] == "failed" and a["reason"] for a in t["attempts"])
    assert t["attempts"][0]["reason"] == "GEE_MEMORY_LIMIT"
    assert t["source_used"] is None and t["error_code"] == "DELINEATION_FAILED"
    blob = json.dumps(t)
    assert "/Users/someone" not in blob and "abc123secret" not in blob       # paths and secrets scrubbed
    assert RunRecord.from_dict(rec).verify()

    capsule = _export(tmp_path)
    log = json.loads((capsule / "run_log.json").read_text())
    row = next(v for v in log.values() if v.get("tool_name") == "delineate_watershed_from_point")
    assert row["record"]["extra"]["attempt_trail"] == t
    code, out = _replay(capsule)
    assert code == 0, out


@needs_fastmcp2
def test_total_failure_of_a_real_fetch_keeps_every_product_tried(world):
    server, _ = world
    with patch("aihydro_data._pipeline._fetch_one", side_effect=_fake_fetch_one_factory(0, fail_all=True)):
        body = _call(server, "data_fetch", {"session_id": SID})
    assert body.get("error") is True
    rec = next(r for r in _records() if r["tool"] == "data_fetch")
    t = rec["extra"]["attempt_trail"]
    assert len(t["attempts"]) >= 2 and all(a["outcome"] == "failed" for a in t["attempts"])
    assert t["source_used"] is None
    assert len({a["source"] for a in t["attempts"]}) == len(t["attempts"])


def test_failure_details_are_read_from_a_raised_exception_too():
    exc = ToolError("DELINEATION_FAILED", "all failed", details={"fallback_history": _history_of_failure()})
    t = rr.attempt_trail({"error": True, "message": "ToolError: all failed"}, exc)
    assert len(t["attempts"]) == 3 and t["error_code"] == "DELINEATION_FAILED"


# ------------------------------------------------------------------ older records
def test_a_record_from_before_the_change_still_verifies_and_has_no_trail():
    legacy = rr.build_run_record(run_id="old.1", tool="data_fetch", session_id="s", arguments={},
                                 result={"data": {"x": 1}, "fallback_history": [{"product": "A", "outcome": "served"}]},
                                 extra={"writer": "middleware"})
    assert "attempt_trail" not in legacy.extra and legacy.verify()
    row = {"run_id": "old.1", "tool_name": "data_fetch", "record": legacy.to_dict()}
    assert rr.verify_run_log_entry(row)["record_ok"] is True       # absence is "not recorded", not a failure


# ------------------------------------------------------------------ the real router (needs the library change)
def test_real_delineation_router_failure_carries_its_history(monkeypatch):
    from aihydro_watershed.delineation import router, small_catchment

    if not hasattr(router, "_delineate_from_point"):
        pytest.skip("aihydro-watershed without the attempt-trail change: a failure keeps no history")
    monkeypatch.setattr(small_catchment, "delineate_small_catchment",
                        lambda lat, lon, *a, **k: (_ for _ in ()).throw(RuntimeError("3DEP down")))
    from ai_hydro.mcp.helpers import _tool_error_to_dict

    with pytest.raises(ToolError) as caught:
        router.delineate_from_point(40.0, -100.0, method="small_catchment")
    envelope = _tool_error_to_dict(caught.value)
    t = rr.attempt_trail(envelope)
    assert t and t["attempts"][0]["source"] == "small_catchment_3dep" and "3DEP down" in t["attempts"][0]["reason"]
