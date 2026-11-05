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
    def data_fetch(session_id: str, variable: str = "precipitation", cache: bool = False) -> dict:
        """The real unified fetch, so the real FetchResult.fallback_history reaches the middleware."""
        from ai_hydro.mcp.tools_data_fetch import data_fetch as real
        return real(variable, "2020-06-01", "2020-06-03", geometry="POINT(-90 40)",
                    cache=cache, session_id=session_id)

    shapes = {
        # single-source fetchers, in the shapes the real tools return (declared source, no fallback_history)
        "fetch_camels_us": {"data": {"p_mean": 2.1}, "meta": {"tool": "fetch_camels_us",
                            "source": "CAMELS-US via pygeohydro.get_camels()"}},
        "fetch_soil_attributes_ssurgo": {"data": {"sand_pct_mean": 30.0}, "meta": {
            "tool": "ssurgo", "product": "SSURGO", "source": "USDA NRCS"}},
        "fetch_streamflow_data": {"data": {"gauge_id": "01013500"}, "meta": {
            "tool": "t", "sources": [{"name": "USGS Water Data API"}], "upstream": {"backend": "usgs_waterdata"}}},
        "fetch_forcing_data": {"data": {"n_days": 3}, "meta": {}, "product": "GRIDMET_PRECIP", "source": "hyriver"},
        "create_cn_grid": {"data": {"landcover_product": "NLCD", "soil_product": "gNATSGO", "product": "NLCD",
                           "source": "MRLC", "cn_mean": 71.0}, "meta": {"tool": "cn"}},
        "compute_twi": {"data": {"twi_mean": 6.0}, "meta": {"tool": "compute_twi"}},      # acquires, declares nothing
        "summarize_series": {"data": {"q_mean": 1.0}, "meta": {"source": "not an acquirer"}},
    }
    for _name, _shape in shapes.items():
        def _make(shape):
            def tool(session_id: str) -> dict:
                return json.loads(json.dumps(shape))
            return tool
        _fn = _make(_shape)
        _fn.__name__ = _name
        server.tool(name=_name)(_fn)

    @server.tool()
    def data_batch_fetch(session_id: str) -> dict:
        """aihydro-data's batch shape: one result or error per label."""
        return {"variable": "precipitation", "labels": ["a", "b", "c"], "results": {
            "a": {"product": "DAYMET_PRECIP", "fallback_history": [
                {"product": "GRIDMET_PRECIP", "source": "hyriver", "outcome": "failed", "reason": "boom"},
                {"product": "DAYMET_PRECIP", "source": "hyriver", "outcome": "served", "reason": ""}]},
            "b": {"product": "CHIRPS", "cache_hit": True, "fallback_history": [
                {"product": "CHIRPS", "source": "gee", "outcome": "served", "reason": ""}]}},
            "errors": {"c": {"error": True, "code": "ALL_PRODUCTS_FAILED", "details": {"fallback_history": [
                {"product": "ERA5L_PRECIP", "source": "gee", "outcome": "failed", "reason": "quota"}]}}},
            "summary": {"succeeded": 2, "failed": 1}}

    @server.tool()
    def get_data_fetch_result(job_id: str, session_id: str = SID) -> dict:
        from ai_hydro.mcp.tools_data_async import get_data_fetch_result as real
        return real(job_id)

    @server.tool()
    def blows_up(session_id: str) -> dict:
        from ai_hydro.mcp.helpers import _tool_error_to_dict
        exc = KeyError("unexpected")
        exc.fallback_history = _history_of_failure()
        return _tool_error_to_dict(exc)

    @server.tool()
    def cached_tool(session_id: str, with_history: bool = True) -> dict:
        res = {"data": {"area_km2": 774.15, "gauge_id": "01031500"}, "meta": {"tool": "delineate_watershed",
               "computed_at": "2026-06-27T21:37:05+00:00"}, "_cached": True}
        if with_history:
            res["data"]["fallback_history"] = [
                {"method": "merit_gee", "outcome": "failed", "reason": "GEE_MEMORY_LIMIT"},
                {"method": "merit_basins_hybrid", "outcome": "succeeded", "reason": ""}]
        return res

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


# ================================================================== cache and reuse truth
@needs_fastmcp2
def test_a_cache_hit_never_presents_the_origins_attempts_as_its_own(world):
    server, _ = world
    with patch("aihydro_data._pipeline._fetch_one", side_effect=_fake_fetch_one_factory(fail_first=1)):
        first = _call(server, "data_fetch", {"session_id": SID, "cache": True})
    assert first["cache_hit"] is False
    with patch("aihydro_data._pipeline._fetch_one", side_effect=AssertionError("must be served from the cache")):
        second = _call(server, "data_fetch", {"session_id": SID, "cache": True})
    assert second["cache_hit"] is True
    recs = sorted((r for r in _records() if r["tool"] == "data_fetch"), key=lambda r: r["recorded_at"])
    t1, t2 = (r["extra"]["attempt_trail"] for r in recs)
    assert "served_from" not in t1 and [a["outcome"] for a in t1["attempts"]][0] == "failed"
    assert t2["served_from"] == "cache" and t2["attempts"] == []
    assert t2["origin_attempts"] == t1["attempts"] and t2["source_used"] == t1["source_used"] == second["product"]


@needs_fastmcp2
def test_a_reuse_record_never_presents_the_origins_attempts_as_its_own(world):
    server, _ = world
    body = _call(server, "cached_tool", {"session_id": SID})
    assert body.get("reused") is True or body.get("_cached") is True
    rec = next(r for r in _records() if r["tool"] == "cached_tool")
    t = rec["extra"]["attempt_trail"]
    assert rec["extra"].get("reused") is True
    assert t["served_from"] == "reuse" and t["attempts"] == []
    assert [a["outcome"] for a in t["origin_attempts"]] == ["failed", "succeeded"]
    assert t["source_used"] == "merit_basins_hybrid"


@needs_fastmcp2
def test_a_reuse_record_takes_the_trail_from_the_origin_row_when_the_result_has_none(world):
    server, _ = world
    origin = rr.build_run_record(
        run_id="watershed.origin1", tool="delineate_watershed", session_id=SID, arguments={}, result={"data": {}},
        extra={"writer": "post_run", "attempt_trail": {
            "basis": "fallback_history", "source_used": "merit_basins_hybrid", "limitations": ["X"],
            "attempts": [{"source": "merit_gee", "outcome": "failed", "reason": "r"},
                         {"source": "merit_basins_hybrid", "outcome": "succeeded", "reason": ""}]}},
        entry={"run_id": "watershed.origin1"})
    store._run_log_record(SID, "watershed.origin1", {
        "run_id": "watershed.origin1", "tool_name": "delineate_watershed", "session_id": SID,
        "timestamp": "2026-06-27T21:37:05+00:00", "key_outputs": {"area_km2": 774.15, "gauge_id": "01031500"},
        "record": origin.to_dict()}, writer="middleware")
    _call(server, "cached_tool", {"session_id": SID, "with_history": False})
    rec = next(r for r in _records() if r["tool"] == "cached_tool")
    t = rec["extra"]["attempt_trail"]
    assert t["served_from"] == "reuse" and t["attempts"] == [] and t["derived_from"] == "watershed.origin1"
    assert [a["source"] for a in t["origin_attempts"]] == ["merit_gee", "merit_basins_hybrid"]


# ================================================================== coverage
@needs_fastmcp2
def test_batch_fetch_gets_a_trail_per_label(world):
    server, _ = world
    _call(server, "data_batch_fetch", {"session_id": SID})
    t = next(r for r in _records() if r["tool"] == "data_batch_fetch")["extra"]["attempt_trail"]
    assert set(t["labels"]) == {"a", "b", "c"}
    assert [a["outcome"] for a in t["labels"]["a"]["attempts"]] == ["failed", "served"]
    assert t["labels"]["a"]["source_used"] == "DAYMET_PRECIP"
    assert t["labels"]["b"]["served_from"] == "cache" and t["labels"]["b"]["attempts"] == []
    assert t["labels"]["c"]["attempts"][0]["reason"] == "quota" and t["labels"]["c"]["source_used"] is None
    assert t["labels"]["c"]["error_code"] == "ALL_PRODUCTS_FAILED"


@needs_fastmcp2
def test_background_job_history_survives_success_and_failure(world, tmp_path, monkeypatch):
    from ai_hydro.mcp import tools_data_async as tda

    server, _ = world

    monkeypatch.setattr(tda, "_artifact_dir", lambda job_id: tmp_path / "jobs" / job_id)
    ok, bad = tmp_path / "jobs" / "okjob", tmp_path / "jobs" / "badjob"
    ok.mkdir(parents=True); bad.mkdir(parents=True)
    (ok / "status.json").write_text(json.dumps({
        "status": "complete", "variable": "precipitation", "product": "DAYMET_PRECIP", "source": "hyriver",
        "fallback_history": [{"product": "GRIDMET_PRECIP", "source": "hyriver", "outcome": "failed", "reason": "boom"},
                             {"product": "DAYMET_PRECIP", "source": "hyriver", "outcome": "served", "reason": ""}]}))
    (bad / "status.json").write_text(json.dumps({
        "status": "failed", "error": {"code": "SOURCE_UNAVAILABLE", "message": "all failed", "fallback_history": [
            {"product": "GRIDMET_PRECIP", "source": "hyriver", "outcome": "failed", "reason": "boom"},
            {"product": "DAYMET_PRECIP", "source": "hyriver", "outcome": "failed", "reason": "boom2"}]}}))
    _call(server, "get_data_fetch_result", {"job_id": "okjob", "session_id": SID})
    _call(server, "get_data_fetch_result", {"job_id": "badjob", "session_id": SID})
    trails = [r["extra"]["attempt_trail"] for r in sorted(
        (r for r in _records() if r["tool"] == "get_data_fetch_result"), key=lambda r: r["recorded_at"])]
    assert [a["outcome"] for a in trails[0]["attempts"]] == ["failed", "served"] and trails[0]["source_used"] == "DAYMET_PRECIP"
    assert [a["reason"] for a in trails[1]["attempts"]] == ["boom", "boom2"] and trails[1]["error_code"] == "SOURCE_UNAVAILABLE"


def test_runner_persists_the_history_of_a_job_that_failed():
    from ai_hydro.mcp.runners.data_fetch_runner import DataFetchRunner
    from aihydro_data.exceptions import SourceUnavailable

    exc = SourceUnavailable("all failed", details={"fallback_history": [
        {"product": "P", "source": "s", "outcome": "failed", "reason": "boom"}]})
    err = DataFetchRunner()._extract_error(exc)
    assert err["fallback_history"][0]["reason"] == "boom" and err["terminal"] is False


@needs_fastmcp2
def test_a_non_toolerror_failure_keeps_the_trail_through_the_error_envelope(world):
    server, _ = world
    body = _call(server, "blows_up", {"session_id": SID})
    assert body["code"] == "UNEXPECTED_ERROR" and len(body["fallback_history"]) == 3
    t = next(r for r in _records() if r["tool"] == "blows_up")["extra"]["attempt_trail"]
    assert [a["source"] for a in t["attempts"]][0] == "merit_gee" and t["error_code"] == "UNEXPECTED_ERROR"


@needs_fastmcp2
@pytest.mark.parametrize("tool,source", [
    ("fetch_camels_us", "CAMELS-US via pygeohydro.get_camels()"),
    ("fetch_soil_attributes_ssurgo", "SSURGO"),
    ("fetch_streamflow_data", "usgs_waterdata"),
    ("fetch_forcing_data", "GRIDMET_PRECIP"),
])
def test_single_source_fetchers_record_a_one_attempt_trail_from_the_declared_source(world, tool, source):
    server, _ = world
    _call(server, tool, {"session_id": SID})
    t = next(r for r in _records() if r["tool"] == tool)["extra"]["attempt_trail"]
    assert t["basis"] == "declared_source"
    assert [(a["source"], a["outcome"]) for a in t["attempts"]] == [(source, "served")] and t["source_used"] == source


@needs_fastmcp2
def test_no_declared_source_or_not_an_acquirer_means_no_trail(world):
    server, _ = world
    _call(server, "compute_twi", {"session_id": SID})                     # acquires, declares no source
    _call(server, "summarize_series", {"session_id": SID})                # declares one but acquires nothing
    for name in ("compute_twi", "summarize_series"):
        rec = next(r for r in _records() if r["tool"] == name)
        assert "attempt_trail" not in rec["extra"], name


def test_forcing_surfaces_the_inner_fetch_history_per_variable(monkeypatch):
    import types

    import numpy as np
    import pandas as pd

    import aihydro_data
    from ai_hydro.data import forcing as fo

    d = pd.date_range("2020-01-01", "2020-01-10", freq="D")
    hist = [{"product": "GRIDMET_PRECIP", "source": "hyriver", "outcome": "failed", "reason": "boom"},
            {"product": "CHIRPS", "source": "gee", "outcome": "served", "reason": ""}]

    def fetch(var, geom, start, end, **kw):
        if var != "precipitation":
            raise RuntimeError("ALL_BACKENDS_FAILED")
        return types.SimpleNamespace(
            data=pd.DataFrame({"date": d, "precipitation": np.full(d.size, 1.0)}), product="CHIRPS", source="gee",
            units="mm/day", coverage_complete=True, days_missing_head=0, days_missing_tail=0,
            days_missing_internal=0, missing_ranges=[], notes=[], fallback_history=hist)

    monkeypatch.setattr(fo, "_adata_fetch", fetch, raising=False)
    monkeypatch.setattr(aihydro_data, "fetch", fetch)
    geom = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    data = fo.fetch_forcing_data_routed(geom, "2020-01-01", "2020-01-10", variables=["pr", "pet"]).data
    rows = data["fallback_history"]
    assert [(r["variable"], r["outcome"]) for r in rows] == [
        ("precipitation", "failed"), ("precipitation", "served"), ("pet", "failed")]
    t = rr.attempt_trail({"data": data})
    assert t["source_used"] == "CHIRPS" and len(t["attempts"]) == 3 and t["attempts"][0]["variable"] == "precipitation"


# ================================================================== scrubber
@pytest.mark.parametrize("raw,gone", [
    ("cannot open file:///Users/someone/proj/data/x.nc now", "/Users/someone"),
    ("cannot open file:///srv/private/cache/x.nc", "/srv/private"),
    ("missing ~/projects/secret_basin/dem.tif", "secret_basin"),
    ("GET failed: Authorization: Bearer abcdef1234567890", "abcdef1234567890"),
    ("sent header Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("retry with Bearer abcdef1234567890xyz", "abcdef1234567890xyz"),
    ("connect https://user:p4ssw0rd@host.example/data failed", "p4ssw0rd"),
    ("token ghp_" + "a1B2c3D4e5" * 4, "a1B2c3D4e5"),
    ("token gho_" + "a1B2c3D4e5" * 4, "a1B2c3D4e5"),
    ("key sk-" + "Zz9" * 12, "Zz9Zz9"),
    ("slack xoxb-1234567890-abcdefghij", "abcdefghij"),
    ("aws AKIA" + "ABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
])
def test_scrubber_removes_each_credential_and_path_shape(raw, gone):
    out = rr.scrub_error_text(raw)
    assert gone not in out and out, out
    assert rr.scrub_error_text(out) == out                                  # idempotent


def test_scrubber_leaves_ordinary_text_alone():
    for text in ("units m/s ~ 3 and a/b", "HTTP 503 from https://api.example.org/v1/items", "NLDI 503", "~ approx",
                 "product sk-SoilGrids-250m-clay-v2 unavailable", "uses Basic Authentication here",
                 "Bearer authentication is not supported", "see https://doi.org/10.1000/xyz123?type=pdf&v=2",
                 "https://api.crossref.org/works/10.1/x?select=title,DOI"):
        assert rr.scrub_error_text(text) == text, text


def test_bearer_is_redacted_at_16_characters_with_or_without_a_digit_and_plain_words_survive():
    for raw, gone in [("Bearer abcdefghijklmnop", "abcdefghijklmnop"), ("Bearer some-long-plain-words-here", "some-long"),
                      ("Bearer abcdefg1", "abcdefg1"), ("retry Bearer ABCDEFGHIJKLMNOPQRST.uvwxyz", "ABCDEFGHIJKLMNOPQRST")]:
        assert gone not in rr.scrub_error_text(raw), raw
    for keep in ("Bearer authentication", "Bearer token required", "uses Bearer scheme", "Basic Authentication",
                 "sk-SoilGrids-250m-clay", "https://doi.org/10.1/x?sig=abc123def"):
        assert rr.scrub_error_text(keep) == keep, keep


def test_scrubber_still_redacts_real_keys_headers_and_non_citation_queries():
    for raw, gone in [("key sk-" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7", "Ab1Cd2Ef3"), ("key sk-proj-" + "a1b2c3d4e5f6g7h8i9j0k1l2", "a1b2c3d4"),
                      ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"), ("Bearer abcdef123456", "abcdef123456"),
                      ("https://example.org/data?access=secretvalue", "secretvalue"),
                      ("https://api.crossref.org/works?mailto=me@example.org&rows=2", "me@example.org")]:
        assert gone not in rr.scrub_error_text(raw), raw


@needs_fastmcp2
def test_error_summary_uses_the_extended_scrubber(world):
    server, _ = world

    @server.tool()
    def leaky(session_id: str) -> dict:
        return {"error": True, "code": "X",
                "message": "failed with Authorization: Bearer abcdef1234567890 at file:///Users/someone/x.nc"}

    _call(server, "leaky", {"session_id": SID})
    log = store._run_log_read_all(SID)
    blob = json.dumps([r for r in log.values() if r.get("tool_name") == "leaky"])
    assert "abcdef1234567890" not in blob and "/Users/someone" not in blob


# ================================================================== batch and backend truth (re-review)
@needs_fastmcp2
def test_a_batch_never_takes_a_declared_source_and_a_failed_label_never_reads_as_served(world):
    server, _ = world

    @server.tool()
    def extract_geomorphic_parameters(session_id: str, mode: str = "mixed") -> dict:
        ok = {"feature_id": "a", "data": {"area_km2": 3.0}, "meta": {"tool": "g", "source": "MERIT Hydro"}}
        bad = {"error": True, "code": "NO_WATERSHED", "message": "no watershed for b"}
        if mode == "all_failed":
            return {"batch": True, "product": "geomorphic", "n_features": 2, "n_success": 0, "n_error": 2,
                    "results": {}, "errors": {"a": dict(bad), "b": dict(bad)}}
        if mode == "failed_with_history":
            return {"batch": True, "product": "geomorphic", "results": {}, "errors": {"a": {
                **bad, "details": {"fallback_history": [
                    {"method": "merit_gee", "outcome": "failed", "reason": "boom"}]}}}}
        return {"batch": True, "product": "geomorphic", "n_features": 2, "n_success": 1, "n_error": 1,
                "results": {"a": ok}, "errors": {"b": bad}}

    _call(server, "extract_geomorphic_parameters", {"session_id": SID, "mode": "mixed"})
    _call(server, "extract_geomorphic_parameters", {"session_id": SID, "mode": "all_failed"})
    _call(server, "extract_geomorphic_parameters", {"session_id": SID, "mode": "failed_with_history"})
    recs = sorted((r for r in _records() if r["tool"] == "extract_geomorphic_parameters"),
                  key=lambda r: r["recorded_at"])
    mixed, all_failed, with_history = (r["extra"].get("attempt_trail") for r in recs)
    assert set(mixed) == {"basis", "labels"} and set(mixed["labels"]) == {"a"}           # no top-level source
    assert [a["source"] for a in mixed["labels"]["a"]["attempts"]] == ["MERIT Hydro"]     # errored label b: nothing
    assert all_failed is None                                                            # never reads as served
    assert with_history["labels"]["a"]["source_used"] is None
    assert with_history["labels"]["a"]["attempts"][0]["outcome"] == "failed"
    assert "geomorphic" not in json.dumps([mixed, all_failed, with_history])


@pytest.mark.parametrize("backend,expected", [("usgs_waterdata", "usgs_waterdata"),
                                              ("legacy_waterservices", "legacy_waterservices")])
def test_the_serving_backend_outranks_the_static_source_list(backend, expected):
    result = {"data": {"gauge_id": "01013500", "upstream": {"backend": backend}},
              "meta": {"sources": [{"name": "USGS Water Data API"}]}}
    t = rr.attempt_trail(result, tool="fetch_streamflow_data")
    assert t["basis"] == "declared_source" and t["source_used"] == expected
    assert "USGS Water Data API" not in json.dumps(t)
    meta_only = {"data": {}, "meta": {"sources": [{"name": "USGS Water Data API"}], "upstream": {"backend": backend}}}
    assert rr.attempt_trail(meta_only, tool="fetch_streamflow_data")["source_used"] == expected
    # no backend reported: the declared list is the fallback
    assert rr.attempt_trail({"data": {}, "meta": {"sources": [{"name": "USGS Water Data API"}]}},
                            tool="fetch_streamflow_data")["source_used"] == "USGS Water Data API"


@needs_fastmcp2
def test_create_cn_grid_lists_the_land_cover_and_the_soil_product(world):
    server, _ = world
    _call(server, "create_cn_grid", {"session_id": SID})
    t = next(r for r in _records() if r["tool"] == "create_cn_grid")["extra"]["attempt_trail"]
    assert t["basis"] == "declared_source"
    assert [a["source"] for a in t["attempts"]] == ["NLCD", "gNATSGO"]
    assert t["attempts"][0]["provider"] == "MRLC" and all(a["outcome"] == "served" for a in t["attempts"])
    # an unset soil product (soil fallback) is not invented
    only = rr.attempt_trail({"data": {"landcover_product": "NLCD", "soil_product": None, "product": "NLCD",
                                      "source": "MRLC"}}, tool="create_cn_grid")
    assert [a["source"] for a in only["attempts"]] == ["NLCD"]
