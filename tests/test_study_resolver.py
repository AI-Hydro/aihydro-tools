"""One study resolver (R-01, R-29; mechanism M1).

A tool that records into or reads from a study must name one that exists. ``HydroSession.load``
returns an empty session for an unknown id and the next ``save()`` makes it real, so
``add_claim(session_id="typoX")`` used to create a study nobody asked for. Every such tool now goes
through ``resolve_existing_study``; only ``start_session`` and the delineation tools create.
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib

import pytest
from fastmcp import Client

import ai_hydro.mcp  # noqa: F401  (registers every tool)
from ai_hydro.mcp.app import mcp
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession

RAT = "Synthetic fixture rationale, long enough to pass."


@pytest.fixture(autouse=True)
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)
    return tmp_path / "sessions"


def call(name, args, meta=None):
    async def run():
        async with Client(mcp) as c:
            return await c.call_tool(name, args, meta=meta, raise_on_error=False)
    return asyncio.run(run())


def body(res) -> dict:
    sc = getattr(res, "structured_content", None)
    if isinstance(sc, dict) and sc:
        return sc.get("result", sc) if set(sc) == {"result"} and isinstance(sc["result"], dict) else sc
    return json.loads("".join(getattr(b, "text", "") for b in res.content))


# Every tool that records into / reads from a study and does not create it, with minimal valid arguments.
ARGS: dict[str, dict] = {
    "add_claim": dict(claim_id="c1", statement="Baseflow dominates summer low flow.", claim_type="empirical_result",
                      status="proposed", confidence="low", confidence_rationale=RAT, basins=["01013500"],
                      period="2000-2010"),
    "update_claim_status": dict(claim_id="c1", status="tested", confidence="low", rationale=RAT),
    "add_assumption": dict(assumption_id="a1", statement="s", risk="low", risk_rationale="r", affects=["c1"]),
    "promote_claim_to_registry": dict(claim_id="c1"),
    "check_registry_staleness": {},
    "draft_claim_from_run": dict(run_id="r1", metric_ref="q_mean"),
    "register_research_plan": dict(hypothesis="BFI exceeds 0.4", planned_analyses=["extract_hydrological_signatures"]),
    "define_experiment": dict(name="e", tool="extract_hydrological_signatures", features=["01013500"]),
    "run_experiment": dict(experiment_id="exp.zzz"),
    "get_experiment_table": dict(experiment_id="exp.zzz"),
    "run_skeptic": {},
    "check_water_balance_consistency": {},
    "check_temporal_alignment": dict(slot_a="streamflow", slot_b="signatures"),
    "check_unit_consistency": dict(slot="streamflow", expected_units="m3/s"),
    "check_record_length": {},
    "check_usgs_qualification_codes": {},
    "check_regulated_basin": {},
    "check_stationarity": {},
    # session tools that used to answer for any id (R-29)
    "get_session_summary": {},
    "get_session_health": {},
    "get_session_raw_state": {},
    "clear_session": {},
    "archive_session": {},
    "add_note": dict(note="hello"),
    "write_research_interpretation": dict(interpretation="text"),
    "export_session": {},
    # review B2: these used a plain _resolve_session and created the study on a save
    "register_feature": dict(geojson='{"type": "Point", "coordinates": [-100.0, 40.0]}'),
    "list_features": {},
    "set_active_feature": dict(feature_id="f1"),
    "get_model_results": {},
    "audit_interpretation": dict(prose="Baseflow index is 0.4 [claim:c1]."),
}
LIST_TOOLS = {"list_claims": {}, "list_assumptions": {}}


def _on_disk(sessions) -> list[str]:
    return sorted(p.name for p in sessions.glob("*.json")) if sessions.exists() else []


@pytest.mark.parametrize("name", sorted(ARGS))
def test_unknown_study_is_refused_and_nothing_is_created(name, sessions):
    HydroSession("real").save()
    before = _on_disk(sessions)
    res = call(name, {"session_id": "typoX", **ARGS[name]})
    b = body(res)
    assert b.get("code") == "STUDY_NOT_FOUND", (name, b)
    assert b["error"] is True and b["recovery"] and "start_session" in b["next_tools"]
    assert {c["study_id"] for c in b["candidates"]} == {"real"}
    assert _on_disk(sessions) == before, f"{name} created a study for an unknown id"
    assert not HydroSession.exists("typoX")


@pytest.mark.parametrize("name", sorted(LIST_TOOLS))
def test_list_tools_refuse_an_unknown_study(name, sessions):
    res = call(name, {"session_id": "typoX"})
    assert res.is_error
    env = json.loads("".join(getattr(b, "text", "") for b in res.content))
    assert env["code"] == "STUDY_NOT_FOUND" and env["error"] is True
    assert _on_disk(sessions) == []


def test_the_audit_reproduction_creates_no_phantom_studies(sessions):
    """R-01 probe: chat bound to studyA, records addressed to typo ids."""
    HydroSession("studyA").save()
    for name in ("add_claim", "register_research_plan", "add_assumption", "define_experiment"):
        b = body(call(name, {"session_id": f"{name}-typo", **ARGS[name]}))
        assert b["code"] == "STUDY_NOT_FOUND", (name, b)
    assert _on_disk(sessions) == ["studyA.json"]


def test_a_path_like_id_is_not_stored_under_a_rewritten_name(sessions):
    HydroSession("studyA").save()
    b = body(call("add_assumption", {"session_id": "../../evil2", **ARGS["add_assumption"]}))
    assert b["code"] == "STUDY_NOT_FOUND"
    assert _on_disk(sessions) == ["studyA.json"]


def test_existing_study_still_works_through_every_path(sessions):
    HydroSession("good").save()
    ok = body(call("add_claim", {"session_id": "good", **ARGS["add_claim"]}))
    assert ok["status"] == "recorded"
    assert body(call("add_assumption", {"session_id": "good", **ARGS["add_assumption"]}))["status"] == "recorded"
    assert [c["id"] for c in call("list_claims", {"session_id": "good"}).structured_content["result"]] == ["c1"]
    assert body(call("update_claim_status", {"session_id": "good", **ARGS["update_claim_status"]}))["status"] == "updated"


def test_a_saved_legacy_named_study_still_resolves(sessions):
    """A session file written before ids were restricted (a space in the name) keeps working."""
    s = HydroSession("old study 1")
    s.save()
    assert HydroSession.exists("old study 1")
    assert body(call("add_note", {"session_id": "old study 1", "note": "still here"}))


def test_chat_bound_study_is_used_when_no_id_is_given_and_a_deleted_binding_is_refused(sessions):
    from ai_hydro.session.chat_binding import get_binding_store

    HydroSession("studyA").save()
    chat = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
    get_binding_store().bind(chat, "studyA", bound_by="test")
    # tools declared with a required session_id are called with the bound id by the host; the resolver
    # itself must accept None and use the binding
    from ai_hydro.mcp.helpers import resolve_existing_study

    assert resolve_existing_study(None, chat) == "studyA"
    (sessions / "studyA.json").unlink()
    from ai_hydro.mcp.helpers import StudyNotFoundError

    with pytest.raises(StudyNotFoundError):
        resolve_existing_study(None, chat)


def test_start_session_still_creates_a_study(sessions):
    b = body(call("start_session", {"session_id": "brand-new"}))
    assert not b.get("error"), b
    assert HydroSession.exists("brand-new")


# ---- scan: no tool that takes session_id may bypass the resolver --------------------------------

#: Tools whose ``session_id`` is deliberately not routed through ``resolve_existing_study``, with the reason.
ALLOWED = {
    # create or open a study on purpose
    "start_session": "creates a study explicitly",
    "delineate_watershed": "creates the study from a gauge",
    "delineate_watershed_from_point": "creates the study from a point",
    "map_set_roi": "a drawn region of interest opens a study (GeoWorkspace)",
    "map_set_working_geometry": "creates the study around a workspace geometry (GeoWorkspace)",
    "map_save_roi": "creates the study around a saved region (GeoWorkspace)",
    "add_session_to_project": "links an id that need not exist yet (pre-registration)",
    "map_add_layer_from_run": "checks run_records._session_exists itself and returns STUDY_NOT_FOUND",
    "map_get_state": "read-only snapshot: loads but never saves, so it cannot create a study",
    # compute tools: first use of a gauge/point opens its study (auto_create_hint). An unknown EXPLICIT id still
    # creates one here; tightening that touches the basin-identity and modelling lanes and is deferred (PR body).
    **{n: "compute tool that opens its study on first use; explicit unknown id deferred" for n in (
        "compute_design_hydrograph", "compute_drought_index", "compute_flood_frequency",
        "compute_flow_duration_curve", "compute_soil_loss_rusle", "extract_geomorphic_parameters",
        "extract_hydrological_signatures", "fetch_camels_us", "fetch_streamflow_data", "separate_baseflow",
        "train_hydro_model", "propose_and_train", "run_autoresearch", "run_inundation_physics_validation",
        "train_inundation_surrogate")},
    "measure_feature": "series helper resolving with allow_auto_create=False; unknown-id check deferred",
    # not a study record: filters or external services
    "list_registry_claims": "global registry filter; session_id only narrows the listing",
    "gee_preview_layer": "optional label passed to Earth Engine",
    "gee_extract_timeseries": "optional label passed to Earth Engine",
}
#: Resolved through another helper that itself resolves (the scan below checks these exist).
HELPER_RESOLVED = {"summarize_series", "detect_threshold_runs", "compare_series", "bootstrap_statistic"}
#: A plain ``_resolve_session(...)`` does NOT count: its default lets an unknown explicit id through
#: (and ``HydroSession.load`` + ``save`` then creates the study). Only the strict forms count.
RESOLVERS = ("resolve_existing_study", "_load_session_or_raise", "must_exist=True", "must_exist=not", "must_exist=bool(")

MCP_DIR = pathlib.Path(__file__).resolve().parent.parent / "ai_hydro" / "mcp"


def _tools_with_session_id() -> dict[str, tuple[str, str]]:
    out = {}
    for path in sorted(MCP_DIR.glob("*.py")):
        src = path.read_text()
        for node in ast.parse(src).body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if not any("tool" in ast.unparse(d) for d in node.decorator_list):
                continue
            if "session_id" in {a.arg for a in node.args.args + node.args.kwonlyargs}:
                out[node.name] = (path.name, ast.get_source_segment(src, node))
    return out


def test_every_tool_with_a_session_id_goes_through_a_resolver():
    tools = _tools_with_session_id()
    assert len(tools) > 40, "scan found too few tools: the decorator pattern changed"
    bypass = sorted(
        n for n, (_f, seg) in tools.items()
        if not any(r in seg for r in RESOLVERS) and n not in ALLOWED and n not in HELPER_RESOLVED
    )
    assert not bypass, f"tools that take session_id and bypass the study resolver: {bypass}"


@pytest.mark.parametrize("tool", ["data_fetch", "data_fetch_background"])
def test_a_mistyped_id_on_a_data_fetch_is_refused_and_writes_nothing(sessions, tool):
    """The session is optional, but a named one must exist: a typo must not become an unrecorded call."""
    b = body(call(tool, {"variable": "precipitation", "geometry": "POINT (-70 45)",
                         "start": "2020-01-01", "end": "2020-01-31", "session_id": "ghost"}))
    assert b.get("code") == "STUDY_NOT_FOUND", b
    assert not HydroSession.exists("ghost")
    assert not list(sessions.glob("ghost*"))


def test_the_ledger_experiment_skeptic_and_validator_tools_use_the_existing_study_resolver():
    """The 20 tools named by R-01 must use the stricter resolver, not just any resolver."""
    must = set(ARGS) - {"get_session_summary", "get_session_health", "get_session_raw_state", "clear_session",
                        "archive_session", "add_note", "write_research_interpretation", "export_session"}
    must |= set(LIST_TOOLS)
    tools = _tools_with_session_id()
    missing = sorted(n for n in must if n not in tools or not any(r in tools[n][1] for r in RESOLVERS))
    assert not missing, missing


def test_allowlist_has_no_stale_entries():
    tools = _tools_with_session_id()
    stale = sorted(n for n in ALLOWED if n not in tools)
    assert not stale, f"allowlisted tools that no longer take session_id: {stale}"
