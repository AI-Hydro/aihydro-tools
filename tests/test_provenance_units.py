"""Provenance, units and schema hygiene found by the 01031500 session study.

S1 units reach the tool response and the run log; S2 cache-served results become reuse
records; one execution has one run id; S3 add_claim warns when the prose asserts a CI the
cited metric lacks and its enums are in the schema; tracebacks and local paths never reach
the model; research.md is not rewritten on every save; start_session / fetch_camels_us are
lean. No network.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import fastmcp
import mcp.types as mcp_types
import pytest
from fastmcp import FastMCP

from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

pytestmark = pytest.mark.skipif(
    int(fastmcp.__version__.split(".")[0]) >= 3,
    reason="real ai_hydro tools need the pinned FastMCP 2.x API",
)

SID = "prov-units"
ORIGINAL_AT = "2026-06-27T21:37:05.128708+00:00"
UNITS = {"q_mean": "mm/day", "runoff_ratio": "dimensionless"}


def call(server, name, arguments=None):
    handler = server._mcp_server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments or {}))
    result = asyncio.run(handler(req))
    root = result.root if hasattr(result, "root") else result
    text = root.content[0].text if root.content else ""
    try:
        return root.isError, json.loads(text)
    except ValueError:
        return root.isError, text


def rows():
    return HydroSession.load(SID).get("_run_log") or {}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ai_hydro.session import chat_binding

    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "home"))
    HydroSession(SID).save()
    rr.reset_stats()
    return tmp_path


def seed_original(rid="watershed.ce56bc643d4c", tool="aihydro_watershed.characterize.watershed.delineate_watershed",
                  timestamp=ORIGINAL_AT, key_outputs=None):
    store._run_log_record(SID, rid, {
        "run_id": rid, "tool_name": tool, "session_id": SID, "timestamp": timestamp,
        "key_outputs": key_outputs or {"area_km2": 774.15, "gauge_id": "01031500"},
        "evidence": {"schema_version": 1, "data": {"area_km2": 774.15}, "uncertainty": None},
        "slot": "watershed"})
    return rid


@pytest.fixture
def server(env):
    from ai_hydro.mcp import app
    from ai_hydro.mcp.enforcement import post_run
    from ai_hydro.mcp.helpers import _session_store, _tool_error_to_dict

    srv = FastMCP(name="probe")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())
    srv.add_middleware(app.ErrorHygieneMiddleware())

    @srv.tool()
    def cached_tool(session_id: str, computed_at: str | None = ORIGINAL_AT) -> dict:
        meta = {"tool": "delineate_watershed"}
        if computed_at:
            meta["computed_at"] = computed_at
        return {"data": {"area_km2": 774.15}, "meta": meta, "_cached": True, "_note": "Watershed already in session."}

    @srv.tool()
    def delineate_watershed(session_id: str) -> dict:         # legacy slot: no computed_at in meta
        return {"data": {"area_km2": 774.15}, "meta": {"tool": "x"}, "_cached": True}

    @srv.tool()
    def gauge_hit_tool(session_id: str) -> dict:
        return {"data": {"area_km2": 774.15, "gauge_id": "01031500"}, "meta": {"tool": "delineate_watershed"},
                "_cached": True}

    @srv.tool()
    def feature_hit_tool(session_id: str) -> dict:
        return {"data": {"area_km2": 774.15}, "meta": {"tool": "t"}, "_cache_hit": True,
                "_run_id": "watershed.ce56bc643d4c", "source_run_id": "watershed.ce56bc643d4c"}

    @srv.tool()
    def fresh_tool(session_id: str) -> dict:
        return {"data": {"area_km2": 1.0}, "meta": {"tool": "t"}}

    @srv.tool()
    def slot_and_post_run(session_id: str) -> dict:
        res = {"data": {"q_mean": 2.0, "units": dict(UNITS)}, "meta": {"tool": "extract_hydrological_signatures"}}
        _session_store(session_id, "signatures", res, tool_name="extract_hydrological_signatures")
        return post_run("extract_hydrological_signatures", session_id, res, inputs={"start_date": "1989-10-01"})

    @srv.tool()
    def leaky_tool(session_id: str) -> dict:
        return {"error": True, "code": "X", "message": "failed reading /Users/someone/secret/data.nc",
                "_traceback": "Traceback (most recent call last):\n  File \"/Users/someone/x.py\"", "traceback": "t"}

    @srv.tool()
    def raising_tool(session_id: str) -> dict:
        try:
            raise RuntimeError("cannot open /Users/someone/secret/data.nc")
        except RuntimeError as exc:
            return _tool_error_to_dict(exc)

    return srv


# ------------------------------------------------------------------- S2 reuse
def test_cached_result_gets_a_first_class_reuse_record(server):
    orig = seed_original()
    err, body = call(server, "cached_tool", {"session_id": SID})
    assert not err and body["_cached"] is True
    assert body["reused"] is True and body["reused_from"] == orig and body["computed_at"] == ORIGINAL_AT
    assert body["origin_match"] == "computed_at" and "copied from the original" in body["_note"]
    assert body["_note"].startswith("REUSED, NOT RECOMPUTED")
    assert "Watershed already in session." in body["_note"]          # the tool's own note is kept
    assert body["_run_id"] != orig

    row = rows()[body["_run_id"]]
    assert row["reused"] is True and row["derived_from"] == orig and row["computed_at"] == ORIGINAL_AT
    assert row["key_outputs"] == {"area_km2": 774.15, "gauge_id": "01031500"}      # the claim can resolve this
    assert not row.get("minimal")
    record = row["record"]
    assert orig in record["parents"]
    assert record["extra"]["reused"] is True and record["extra"]["derived_from"] == orig
    assert rr.verify_run_log_entry(row)["record_ok"]


def test_inferred_origin_is_accepted_only_when_identity_agrees_and_says_inferred(server):
    seed_original(rid="watershed.old", timestamp="2026-06-01T00:00:00+00:00")
    newest = seed_original(rid="watershed.new", timestamp="2026-06-27T00:00:00+00:00")
    _, body = call(server, "delineate_watershed", {"session_id": SID})
    assert body["reused_from"] == newest and body["origin_match"] == "latest_by_tool"
    assert "INFERRED" in body["_note"]
    assert rows()[body["_run_id"]]["origin_match"] == "latest_by_tool"


def test_wrong_feature_candidate_is_not_linked(server):
    # A cached answer for another gauge must not inherit the latest row of the tool.
    seed_original(rid="watershed.other", key_outputs={"area_km2": 55.0, "gauge_id": "99999999"})
    _, body = call(server, "gauge_hit_tool", {"session_id": SID})
    assert body["reused_from"] is None and body["origin_match"] == "unresolved"
    assert "could not be identified" in body["_note"] and "copied from the original" not in body["_note"]
    row = rows()[body["_run_id"]]
    assert row["derived_from"] is None and row["key_outputs"].get("area_km2") == 774.15
    assert "watershed.other" not in row["record"]["parents"]


def test_ndvi_cache_hit_never_inherits_the_ndwi_row(env):
    def idx_row(rid, ts, ko):
        store._run_log_record(SID, rid, {"run_id": rid, "tool_name": "compute_spectral_index", "session_id": SID,
                                         "timestamp": ts, "key_outputs": ko, "evidence": {"data": ko}})
    idx_row("ndvi.1", "2026-01-01T00:00:00+00:00", {"index": "NDVI", "mean": 0.61})
    idx_row("ndwi.2", "2026-01-02T00:00:00+00:00", {"index": "NDWI", "mean": -0.20})
    hit = {"index_name": "NDVI", "data": {"mean": 0.61}, "_cached": True}
    assert rr.find_origin_row(SID, "compute_spectral_index", hit)[0] == "ndvi.1"
    other = {"index_name": "NDMI", "data": {"mean": 0.3}, "_cached": True}
    assert rr.find_origin_row(SID, "compute_spectral_index", other)[0] is None


def test_no_shared_identity_needs_equal_shared_values(env):
    seed_original(rid="w.1", key_outputs={"area_km2": 10.0})
    same = {"data": {"area_km2": 10.0}, "meta": {"tool": "delineate_watershed"}, "_cached": True}
    diff = {"data": {"area_km2": 11.0}, "meta": {"tool": "delineate_watershed"}, "_cached": True}
    none = {"data": {"unrelated": 1}, "meta": {"tool": "delineate_watershed"}, "_cached": True}
    assert rr.find_origin_row(SID, "delineate_watershed", same)[0] == "w.1"
    assert rr.find_origin_row(SID, "delineate_watershed", diff)[0] is None
    assert rr.find_origin_row(SID, "delineate_watershed", none)[0] is None


def test_unresolvable_origin_is_disclosed_and_keeps_response_values(server):
    _, body = call(server, "cached_tool", {"session_id": SID, "computed_at": ""})
    assert body["reused"] is True and body["reused_from"] is None
    assert "could not be identified" in body["_note"] and "copied from the original" not in body["_note"]
    row = rows()[body["_run_id"]]
    assert row["derived_from"] is None and row["key_outputs"]["area_km2"] == 774.15


def test_cache_hit_naming_the_original_run_id_is_replaced_by_the_reuse_row(server):
    orig = seed_original()
    _, body = call(server, "feature_hit_tool", {"session_id": SID})
    assert body["_run_id"] != orig and body["reused_from"] == orig
    assert rows()[orig].get("reused") is None                         # the original row is untouched


def test_reuse_chain_points_at_the_first_computation(server):
    orig = seed_original()
    _, first = call(server, "cached_tool", {"session_id": SID})
    _, second = call(server, "cached_tool", {"session_id": SID})
    assert second["reused_from"] == orig and second["_run_id"] != first["_run_id"]


def test_fresh_result_is_not_marked_reused(server):
    _, body = call(server, "fresh_tool", {"session_id": SID})
    assert "reused" not in body and "reused_from" not in body
    assert not rows()[body["_run_id"]].get("reused")


# ------------------------------------------------------- one run id per execution
def test_slot_row_and_post_run_row_are_one_row(server):
    _, body = call(server, "slot_and_post_run", {"session_id": SID})
    run_id = body["_run_id"]
    session = HydroSession.load(SID)
    assert session.signatures["meta"]["run_id"] == run_id
    sig_rows = {rid: r for rid, r in session.get("_run_log").items()
                if r.get("record", {}).get("tool") == "slot_and_post_run"}
    assert list(sig_rows) == [run_id]
    row = sig_rows[run_id]
    assert row["slot"] == "signatures" and row["inputs"] == {"start_date": "1989-10-01"}
    assert row["tool_name"] == "extract_hydrological_signatures"
    # S1: units ride in the sealed row's key_outputs and evidence.
    assert row["key_outputs"]["units"] == UNITS
    assert row["evidence"]["data"]["units"] == UNITS


def test_post_run_outside_a_recorded_call_still_mints_its_own_id(env):
    from ai_hydro.mcp.enforcement import post_run
    from ai_hydro.mcp.helpers import _session_store

    res = {"data": {"q_mean": 2.0}, "meta": {}}
    _session_store(SID, "signatures", res, tool_name="t")
    out = post_run("t", SID, {"data": {"q_mean": 2.0}, "meta": res["meta"]})
    assert out["_run_id"] != HydroSession.load(SID).signatures["meta"]["run_id"]


# -------------------------------------------------------------- error hygiene
def test_tool_error_dict_has_no_traceback_and_no_local_path(env):
    from ai_hydro.mcp.helpers import _tool_error_to_dict

    try:
        raise RuntimeError("cannot open /Users/someone/secret/data.nc")
    except RuntimeError as exc:
        out = _tool_error_to_dict(exc)
    assert out["code"] == "UNEXPECTED_ERROR"
    assert "_traceback" not in out and "traceback" not in out
    assert "/Users/someone" not in json.dumps(out) and "data.nc" in out["message"]


def test_traceback_is_logged_for_the_operator(env, caplog):
    from ai_hydro.mcp.helpers import _tool_error_to_dict

    with caplog.at_level("ERROR", logger="ai_hydro.mcp"):
        try:
            raise RuntimeError("kaboom")
        except RuntimeError as exc:
            _tool_error_to_dict(exc)
    assert "Traceback" in caplog.text and "kaboom" in caplog.text


def test_middleware_strips_tracebacks_a_tool_built_itself(server):
    _, body = call(server, "leaky_tool", {"session_id": SID})
    assert body["error"] is True and body["code"] == "X"
    assert "_traceback" not in body and "traceback" not in body
    assert "/Users/someone" not in json.dumps(body)
    sealed = json.dumps(rows())
    assert "Traceback" not in sealed and "/Users/someone" not in sealed      # sealed record is clean too


def test_error_hygiene_does_not_touch_successful_results(server):
    _, body = call(server, "fresh_tool", {"session_id": SID})
    assert body["data"] == {"area_km2": 1.0}


# ------------------------------------------------------------------- S3 ledger
@pytest.fixture
def ledger(env, monkeypatch):
    from ai_hydro.registry import store as registry
    monkeypatch.setattr(registry, "REGISTRY_DIR", env / "registry")
    monkeypatch.setattr(registry, "CLAIMS_FILE", env / "registry" / "claims.jsonl")
    monkeypatch.setattr("ai_hydro.mcp.tools_ledger.push_claim_event", lambda **kw: None)
    return env


def _sig_run(with_ci=("q_mean",)):
    rid = "sigs.20261020.prov.aaaa1111"
    store._run_log_record(SID, rid, {
        "run_id": rid, "tool_name": "extract_hydrological_signatures", "session_id": SID,
        "timestamp": "2026-10-20T08:31:43+00:00", "key_outputs": {"q_mean": 2.0, "runoff_ratio": 0.6, "stream_elas": 1.7},
        "evidence": {"schema_version": 1, "data": {"q_mean": 2.0},
                     "uncertainty": {m: {"ci_low": 1.8, "ci_high": 2.2} for m in with_ci}}})
    return rid


def _claim(rid, rationale, metrics=("q_mean", "runoff_ratio", "stream_elas"), statement="Signatures of the basin."):
    from ai_hydro.mcp.tools_ledger import add_claim
    return add_claim(
        session_id=SID, claim_id="c1", statement=statement, claim_type="empirical_result", status="tested",
        confidence="high", confidence_rationale=rationale, basins=["01031500"], period="1989-2009",
        evidence_spans=[{"source_type": "run", "source_id": rid, "metric_ref": m} for m in metrics])


def test_add_claim_warns_when_prose_asserts_a_ci_the_metric_lacks(ledger):
    rid = _sig_run(with_ci=("q_mean",))
    out = _claim(rid, "Bootstrap 90% CIs reported for each statistic.")
    assert out["status"] == "recorded"                      # non-blocking
    (w,) = out["warnings"]
    assert w["code"] == "UNCERTAINTY_ASSERTED_NOT_EVIDENCED" and w["blocking"] is False
    assert {m["metric_ref"] for m in w["metrics_without_ci"]} == {"runoff_ratio", "stream_elas"}
    assert w["metrics_with_ci"] == ["q_mean"]
    assert "runoff_ratio" in out["_note"] and "stream_elas" in out["_note"]


def test_add_claim_does_not_warn_when_every_cited_metric_has_a_ci(ledger):
    rid = _sig_run(with_ci=("q_mean", "runoff_ratio", "stream_elas"))
    out = _claim(rid, "Bootstrap 90% CIs reported for each statistic.")
    assert "warnings" not in out


def test_add_claim_does_not_warn_when_prose_claims_no_uncertainty(ledger):
    rid = _sig_run(with_ci=())
    out = _claim(rid, "Point estimates from 20 years of daily data.")
    assert "warnings" not in out


def test_add_claim_warns_from_the_statement_and_for_a_missing_run(ledger):
    out = _claim("sigs.does.not.exist", "A single run of the tool, no replication.", metrics=("q_mean",),
                 statement="Mean flow is 2.0 mm/day (95% CI 1.8-2.2).")
    (w,) = out["warnings"]
    assert w["metrics_without_ci"][0]["run_found"] is False


def test_claim_enums_are_in_the_json_schema():
    from ai_hydro.mcp import tools_ledger  # noqa: F401  (registers the tools)
    from ai_hydro.mcp.app import mcp

    tools = asyncio.run(mcp.get_tools())
    props = tools["add_claim"].parameters["properties"]
    assert set(props["claim_type"]["enum"]) == {"empirical_result", "methodological", "hypothesis", "negative_result"}
    assert "supported" in props["status"]["enum"] and "high" in props["confidence"]["enum"]
    upd = tools["update_claim_status"].parameters["properties"]
    assert upd["status"]["enum"] == props["status"]["enum"] and upd["confidence"]["enum"] == props["confidence"]["enum"]


def test_a_guessed_claim_type_is_refused_by_the_schema(env):
    from ai_hydro.mcp.app import mcp
    err, body = call(mcp, "add_claim", {
        "session_id": SID, "claim_id": "c", "statement": "s", "claim_type": "quantitative", "status": "tested",
        "confidence": "high", "confidence_rationale": "r", "basins": ["b"], "period": "p"})
    assert err and "claim_type" in json.dumps(body)


# ----------------------------------------------------------- M3 research.md
def _research_md(ws):
    from ai_hydro.registry.paths import rules_dir
    return rules_dir(str(ws)) / "research.md"


def test_save_does_not_rewrite_research_md_but_task_boundaries_do(env):
    ws = env / "ws"
    ws.mkdir()
    session = HydroSession.load(SID)
    session.workspace_dir = str(ws)
    session.save()
    assert not _research_md(ws).exists(), "a mid-task save must not write the agent's instruction file"

    session.save(sync_context=True)
    md = _research_md(ws)
    assert md.exists()
    md.write_text("SENTINEL")
    session.notes.append("a note")
    session.save()
    session.commit()
    assert md.read_text() == "SENTINEL"

    session.archive()                                      # task end
    assert md.read_text() != "SENTINEL"


def test_start_session_and_export_refresh_research_md(env):
    from ai_hydro.mcp.tools_session import export_session, start_session

    ws = env / "ws2"
    ws.mkdir()
    start_session(session_id=SID, workspace_dir=str(ws))
    md = _research_md(ws)
    assert md.exists()
    md.write_text("SENTINEL")
    HydroSession.load(SID).save()
    assert md.read_text() == "SENTINEL"
    export_session(session_id=SID, format="json")
    assert md.read_text() != "SENTINEL"


# -------------------------------------------------------------- M4 payloads
def test_start_session_omits_available_packages_unless_asked(env):
    from ai_hydro.mcp.tools_session import start_session

    with patch("ai_hydro.mcp.tools_session.subprocess.run") as run:
        out = start_session(session_id=SID)
        assert "available_packages" not in out and not run.called
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps([{"name": "numpy", "version": "2.0"}])
        out = start_session(session_id=SID, include_packages=True)
    assert out["available_packages"] == {"numpy": "2.0"}


def _camels_frame():
    import pandas as pd
    return pd.DataFrame({"q_mean": [2.03], "runoff_ratio": [0.576], "elev_mean": [300.0], "gauge_name": ["X"]},
                        index=["01031500"])


def test_fetch_camels_us_returns_each_value_once_and_a_group_index(env):
    import types
    from ai_hydro.mcp.tools_analysis import fetch_camels_us

    fake = types.SimpleNamespace(get_camels=lambda: (_camels_frame(), None))
    with patch.dict("sys.modules", {"pygeohydro": fake}):
        out = fetch_camels_us(session_id=SID, gauge_id="01031500")
    data = out["data"]
    assert data["attributes"]["q_mean"] == 2.03
    assert "attribute_groups" not in data
    assert data["attribute_group_index"]["hydrology"] == ["q_mean", "runoff_ratio"]
    assert json.dumps(out).count("2.03") == 1


def test_fetch_camels_us_cache_hit_does_not_load_the_dataset(env):
    import types
    from ai_hydro.mcp.tools_analysis import fetch_camels_us

    calls = []

    def get_camels():
        calls.append(1)
        return _camels_frame(), None

    with patch.dict("sys.modules", {"pygeohydro": types.SimpleNamespace(get_camels=get_camels)}):
        fetch_camels_us(session_id=SID, gauge_id="01031500")
        assert len(calls) == 1
        again = fetch_camels_us(session_id=SID, gauge_id="01031500")
        assert again["_cached"] is True and len(calls) == 1
        # a cache hit needs neither the dataset nor the dependency
    with patch.dict("sys.modules", {"pygeohydro": None}):
        assert fetch_camels_us(session_id=SID)["_cached"] is True


def test_fetch_camels_us_other_gauge_is_not_answered_from_the_cache(env):
    import types
    from ai_hydro.mcp.tools_analysis import fetch_camels_us

    fake = types.SimpleNamespace(get_camels=lambda: (_camels_frame(), None))
    with patch.dict("sys.modules", {"pygeohydro": fake}):
        fetch_camels_us(session_id=SID, gauge_id="01031500")
        other = fetch_camels_us(session_id=SID, gauge_id="01013500")
    assert not other.get("_cached") and other["data"]["in_camels"] is False


# ------------------------------------------------------ BASIN_REF loop advice
def test_cached_legacy_watershed_without_basin_ref_points_to_clear_session(env):
    from ai_hydro.mcp.tools_analysis import delineate_watershed

    s = HydroSession.load(SID)
    s.site_id = "01031500"
    s.watershed = {"data": {"area_km2": 774.15, "gauge_id": "01031500"}, "meta": {"tool": "t"}}
    s.save()
    out = delineate_watershed(gauge_id="01031500", session_id=SID)
    assert out["_cached"] is True and out["basin_ref_missing"] is True
    assert "clear_session" in out["_note"] and "'watershed'" in out["_note"]


def test_basin_ref_required_recovery_names_clear_session():
    from ai_hydro.identity import BasinRefRequiredError

    d = BasinRefRequiredError("c1", ["01031500"], SID).to_dict()
    assert "clear_session" in d["recovery"]


# ------------------------------------ S1 / streamflow digest through the real tool
def test_real_signatures_tool_carries_units_and_streamflow_digest(env, monkeypatch):
    """The real extract_hydrological_signatures (aihydro-watershed, no network): the response, the
    sealed run-log row's key_outputs and its evidence all carry the unit map and the digest."""
    import numpy as np
    from aihydro_core.records import digest_bytes
    from ai_hydro.mcp import app
    from ai_hydro.mcp.helpers import _session_store

    q = list(5.0 + 3.0 * np.sin(np.linspace(0, 40, 800)) + np.random.default_rng(1).random(800))
    data_file = env / "streamflow.json"
    data_file.write_text(json.dumps({"q_cms": q}))
    session = HydroSession.load(SID)
    session.site_id = "global-basin"
    session.set("watershed", {"data": {"area_km2": 250.0}, "meta": {"tool": "delineate_watershed"}})
    session.save()
    _session_store(SID, "streamflow", {
        "data": {"q_cms": q, "n_days": len(q), "_data_file": str(data_file)},
        "meta": {"tool": "fetch_streamflow_data",
                 "params": {"gauge_id": "global-basin", "start_date": "2000-01-01", "end_date": "2002-03-10"},
                 "retained_series": {"path": str(data_file), "digest": digest_bytes(data_file.read_bytes())}}},
        tool_name="fetch_streamflow_data")
    square = json.dumps({"type": "Polygon", "coordinates": [[[-77.5, 39.2], [-77.4, 39.2], [-77.4, 39.3],
                                                             [-77.5, 39.3], [-77.5, 39.2]]]})
    err, body = call(app.mcp, "extract_hydrological_signatures", {
        "session_id": SID, "geometry_geojson": square, "start_date": "2000-01-01",
        "end_date": "2002-03-10", "precipitation": "skip"})
    assert not err and not body.get("error"), body
    units = body["data"]["units"]
    assert units["q_mean"] == "mm/day" and units["runoff_ratio"] == "dimensionless"
    assert body["data"]["streamflow_digest"].startswith("sha256:")

    row = rows()[body["_run_id"]]
    assert row["key_outputs"]["units"] == units
    assert row["evidence"]["data"]["units"] == units
    assert row["key_outputs"]["streamflow_digest"] == body["data"]["streamflow_digest"]
    slot_ids = [r["meta"].get("run_id") for by_key in HydroSession.load(SID)._slots["signatures"].values()
                for r in by_key.values()]
    assert slot_ids == [body["_run_id"]]                                                 # one id


# --------------------------------------------- UNCERTAINTY heuristic refinements
@pytest.mark.parametrize("text,expected", [
    ("Bootstrap 90% CIs reported for each statistic.", True),
    ("Mean 2.0 mm/day with standard error 0.1.", True),
    ("Estimate 2.0 \u00b1 0.2 (1\u03c3).", True),
    ("The 95% interval is narrow.", True),
    ("No confidence interval was computed for these point estimates.", False),
    ("Uncertainty was not quantified.", False),
    ("Reported without CIs.", False),
    ("Point estimates only.", False),
])
def test_uncertainty_assertion_heuristic(text, expected):
    from ai_hydro.mcp.tools_ledger import _asserts_uncertainty
    assert _asserts_uncertainty(text) is expected


def test_error_hygiene_logs_the_dropped_traceback_at_debug(caplog):
    from ai_hydro.mcp.error_hygiene import sanitize_error
    with caplog.at_level("DEBUG", logger="ai_hydro.mcp.error_hygiene"):
        out = sanitize_error({"error": True, "code": "X", "message": "m", "_traceback": "Traceback TB-MARK"})
    assert "_traceback" not in out and "TB-MARK" in caplog.text


# ------------------------------------------------------ cache vs request (#9)
def test_cached_watershed_for_another_gauge_says_so(env):
    from ai_hydro.mcp.tools_analysis import delineate_watershed

    s = HydroSession.load(SID)
    s.site_id = "01031500"
    s.watershed = {"data": {"area_km2": 774.15, "gauge_id": "01031500"}, "meta": {"tool": "t"}}
    s.save()
    out = delineate_watershed(gauge_id="01013500", session_id=SID)
    assert out["_cached"] is True and out["cache_mismatch"]["cached_gauge_id"] == "01031500"
    assert "NOT the requested" in out["_note"]
