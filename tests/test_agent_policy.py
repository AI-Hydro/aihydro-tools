"""Agent orchestration, Stage 1: server identity and floor.

What is tested (each item of the Stage 1 acceptance list):

* ``delegable`` is on every row of the annotation table, with a closed vocabulary and the
  classification principle (claims, plan, interpretation, binding, lifecycle: lead only);
* ``AgentPolicyMiddleware`` refuses lead tools and host-audience tools to a call that names an agent,
  refuses an explicit ``session_id`` that is not the call's study, refuses every agent in an
  evaluation run, and is a pure pass-through for a call with no agent;
* a call that names an agent never rebinds the chat;
* the agent identity is sealed into the run record (``extra.agent``, ``asserted_by: client``), the
  record verifies, and a plain call's record differs from an agent call's by that one key only;
* ``aihydro_record_agent_run`` stores insert-only, ``export_session`` writes ``agents/`` and the
  capsule passes the privacy scan and the standalone replay.

``agent`` is asserted by the client. Nothing here treats it as authority; the tests check that it
only ever narrows.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from ai_hydro.mcp import agent_policy, tool_surface
from ai_hydro.mcp.agent_policy import (
    AGENT_CONTEXT_MISMATCH, AGENT_IN_EVALUATION, AGENT_NOT_DELEGABLE, DELEGABLE_VALUES,
    AgentPolicyMiddleware, delegable_of, parse_agent,
)
from ai_hydro.mcp.tool_surface import load_annotation_rows
from ai_hydro.session import agent_runs
from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore, get_binding_store
from ai_hydro.session.store import HydroSession

META = "aihydro/context"
CHAT = "01KV1EPM88P9FV5CWNVJ9V60JY"
SID = "agent-study"
OTHER = "agent-other"
AGENT = {"agent_id": "ag_1", "parent_agent_id": "lead", "role": "explorer",
         "definition_sha256": "ab" * 32, "team_id": "t1", "lease": {"calls": 5}}
ROWS = load_annotation_rows()


# ----------------------------------------------------------------- the annotation table
def problems(rows: dict) -> list[str]:
    """Every way a row can break the delegable contract. Empty means the table is sound."""
    out = []
    for name, row in rows.items():
        d = row.get("delegable")
        if d not in DELEGABLE_VALUES:
            out.append(f"{name}: delegable {d!r} is missing or not one of {sorted(DELEGABLE_VALUES)}")
            continue
        if d == "read" and not row["readOnlyHint"]:
            out.append(f"{name}: only a readOnlyHint tool may be delegable=read")
        if d != "lead" and (row.get("audience") == "host" or row.get("requiresConsent")
                            or row["destructiveHint"]):
            out.append(f"{name}: host-audience, consent and destructive tools are lead-only")
        if d == "scoped" and not row.get("acquires"):
            out.append(f"{name}: scoped is for data acquisition (acquires) only")
    return out


def test_every_row_has_a_delegable_class():
    assert problems(ROWS) == []
    assert len(ROWS) >= 180 and all("delegable" in r for r in ROWS.values())


def test_a_new_row_without_delegable_fails_the_check():
    """The guard itself: drop the key from one row, or add a row that never classified itself."""
    broken = {k: dict(v) for k, v in ROWS.items()}
    del broken["add_note"]["delegable"]
    assert any(p.startswith("add_note:") for p in problems(broken))
    broken["brand_new_tool"] = {k: v for k, v in ROWS["add_note"].items() if k != "delegable"}
    assert any(p.startswith("brand_new_tool:") for p in problems(broken))
    broken["typo"] = {**ROWS["add_note"], "delegable": "Lead"}
    assert any(p.startswith("typo:") for p in problems(broken))


def test_every_registered_tool_is_served_with_its_class():
    from ai_hydro.mcp import mcp

    async def go():
        async with Client(mcp) as c:
            return await c.list_tools()

    listed = asyncio.run(go())
    assert listed
    for t in listed:
        assert (t.meta or {}).get("aihydro/delegable") in DELEGABLE_VALUES, t.name


LEAD_ONLY = {
    # claims, plan, interpretation, binding
    "add_claim", "update_claim_status", "add_assumption", "promote_claim_to_registry",
    "bind_claim_to_item", "bind_map_to_claim", "register_research_plan", "write_research_interpretation",
    "log_researcher_observation",
    # study lifecycle, and delineation (it creates a study)
    "start_session", "start_project", "clear_session", "archive_session", "merge_session_shards",
    "aihydro_rebind_chat", "delineate_watershed", "delineate_watershed_from_point", "export_session",
    # profile and skills, host operations, code execution
    "get_researcher_profile", "update_researcher_profile", "load_skill", "list_skills", "save_skill",
    "aihydro_record_agent_run", "ensure_project", "run_python",
    # bulk downloads and long runs stay with the lead
    "merit_ensure_region", "run_experiment",
    # bulk and background acquisition: a later stage promotes these on purpose
    "data_batch_fetch", "lsh_batch", "data_fetch_background",
    # reach beyond the call's own study
    "get_project_summary", "find_studies_for_gauge", "gauge_flowlines",
}


def test_the_floor_is_lead_only():
    for name in LEAD_ONLY:
        assert ROWS[name]["delegable"] == "lead", name


def test_classification_counts_and_basis():
    counts = {c: sum(1 for r in ROWS.values() if r["delegable"] == c) for c in DELEGABLE_VALUES}
    assert sum(counts.values()) == len(ROWS)
    # `read` is exactly the read-only tools minus the profile/skills reads; nothing else is
    ro = {n for n, r in ROWS.items() if r["readOnlyHint"]}
    assert {n for n, r in ROWS.items() if r["delegable"] == "read"} == ro - {
        "get_researcher_profile", "list_skills", "load_skill",   # profile and skills are the lead's
        "get_project_summary", "find_studies_for_gauge"}         # reach beyond the call's own study
    assert counts["scoped"] > 0 and counts["lead"] > counts["scoped"]


def test_merge_session_shards_is_host_audience():
    assert ROWS["merge_session_shards"]["audience"] == "host"


def test_unknown_or_unclassified_tool_reads_as_lead():
    assert delegable_of("no_such_tool", rows={}) == "lead"
    assert delegable_of("t", rows={"t": {"readOnlyHint": True}}) == "lead"
    assert delegable_of("t", rows={"t": {"delegable": "everyone"}}) == "lead"
    assert delegable_of("t", rows={"t": {"delegable": "scoped"}}) == "scoped"


# ----------------------------------------------------------------- parse_agent
def test_parse_agent_absent_is_none_and_garbled_is_still_an_agent():
    assert parse_agent(None) is None
    assert parse_agent("x")["agent_id"] == "unidentified"           # cannot shed the floor by garbling
    assert parse_agent([1])["agent_id"] == "unidentified"
    assert parse_agent({})["agent_id"] == "unidentified"
    got = parse_agent({**AGENT, "evil": "x", "role": "r" * 999})
    assert set(got) == {"agent_id", "parent_agent_id", "role", "definition_sha256", "team_id", "lease"}
    assert len(got["role"]) == 200 and got["lease"] == {"calls": 5}


# ----------------------------------------------------------------- fixtures
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("AIHYDRO_EVAL_NONCE", raising=False)
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    from ai_hydro.session import chat_binding

    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    (tmp_path / "sessions").mkdir()
    HydroSession(SID).save()
    HydroSession(OTHER).save()
    rr.reset_stats()
    return tmp_path


def _rows(monkeypatch, **classes):
    """Replace the cached annotation table the policy reads (the real table is tested above)."""
    rows = {n: {"delegable": c, "readOnlyHint": c == "read"} for n, c in classes.items()}
    monkeypatch.setattr(tool_surface, "_ROWS_CACHE", rows)
    return rows


def _server(*, record=False):
    from ai_hydro.mcp import app

    srv = FastMCP(name="agent-probe")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(AgentPolicyMiddleware())
    if record:
        srv.add_middleware(app.RunRecordMiddleware())

    @srv.tool()
    def read_tool() -> dict:
        return {"data": {"ok": True}}

    @srv.tool()
    def scoped_tool(session_id: str | None = None) -> dict:
        from ai_hydro.mcp.helpers import _resolve_session

        return {"data": {"resolved": _resolve_session(session_id, allow_auto_create=False)}}

    @srv.tool()
    def lead_tool() -> dict:
        return {"data": {"ran": True}}

    @srv.tool()
    def unclassified_tool() -> dict:
        return {"data": {"ran": True}}

    @srv.tool()
    def host_tool() -> dict:
        return {"data": {"ran": True}}

    return srv


def _call(srv, name, args=None, agent=None, **ctx):
    c = dict(ctx)
    if agent is not None:
        c["agent"] = agent
    meta = {META: c} if c else None

    async def go():
        async with Client(srv) as cl:
            return await cl.call_tool(name, args or {}, meta=meta, raise_on_error=False)

    return asyncio.run(go())


def _err(res) -> dict:
    assert res.is_error
    return json.loads(res.content[0].text)


@pytest.fixture
def policy_rows(monkeypatch):
    return _rows(monkeypatch, read_tool="read", scoped_tool="scoped", lead_tool="lead")


# ----------------------------------------------------------------- the middleware
def test_a_call_with_no_agent_passes_through_every_class(env, policy_rows):
    srv = _server()
    for name in ("read_tool", "scoped_tool", "lead_tool", "unclassified_tool"):
        res = _call(srv, name, study_id=SID)
        assert not res.is_error, name


def test_read_and_scoped_pass_for_an_agent_lead_and_unclassified_are_refused(env, policy_rows):
    srv = _server()
    assert not _call(srv, "read_tool", agent=AGENT, study_id=SID).is_error
    assert not _call(srv, "scoped_tool", agent=AGENT, study_id=SID).is_error
    for name in ("lead_tool", "unclassified_tool"):
        body = _err(_call(srv, name, agent=AGENT, study_id=SID))
        assert body["code"] == AGENT_NOT_DELEGABLE, name
        assert "lead" in body["message"] and name in body["message"] and "ag_1" in body["message"]


def test_a_garbled_agent_field_is_still_refused_a_lead_tool(env, policy_rows):
    srv = _server()
    for bad in ("yes", ["a"], {"agent_id": ""}, {"role": 3}):
        assert _err(_call(srv, "lead_tool", agent=bad, study_id=SID))["code"] == AGENT_NOT_DELEGABLE


def test_host_audience_is_refused_even_if_labelled_scoped(env, monkeypatch):
    rows = _rows(monkeypatch, host_tool="scoped")
    rows["host_tool"]["audience"] = "host"
    srv = _server()
    assert not _call(srv, "host_tool", study_id=SID).is_error          # the host itself
    assert _err(_call(srv, "host_tool", agent=AGENT, study_id=SID))["code"] == AGENT_NOT_DELEGABLE


def test_an_agent_cannot_name_another_study(env, policy_rows):
    srv = _server()
    body = _err(_call(srv, "scoped_tool", {"session_id": OTHER}, agent=AGENT, study_id=SID, chat_id=CHAT))
    assert body["code"] == AGENT_CONTEXT_MISMATCH
    # no study in the context: an explicit id is not the call's study either
    assert _err(_call(srv, "scoped_tool", {"session_id": OTHER}, agent=AGENT))["code"] == AGENT_CONTEXT_MISMATCH
    # naming the call's own study is fine
    res = _call(srv, "scoped_tool", {"session_id": SID}, agent=AGENT, study_id=SID)
    assert not res.is_error and res.data["data"]["resolved"] == SID
    # a plain call may still name any study (unchanged behaviour)
    assert not _call(srv, "scoped_tool", {"session_id": OTHER}, study_id=SID).is_error


def test_a_child_does_not_rebind_the_parent_chat(env, policy_rows):
    get_binding_store().bind(CHAT, OTHER)
    srv = _server()
    # the child's context names study SID (and so does its explicit argument): a plain call would rebind
    res = _call(srv, "scoped_tool", {"session_id": SID}, agent=AGENT, study_id=SID, chat_id=CHAT)
    assert not res.is_error and res.data["data"]["resolved"] == SID
    assert get_binding_store().lookup_study(CHAT) == OTHER
    res = _call(srv, "scoped_tool", agent=AGENT, study_id=SID, chat_id=CHAT)          # meta rule, same
    assert get_binding_store().lookup_study(CHAT) == OTHER
    # contrast: the same plain call does rebind, so the assertion above is the guard's doing
    _call(srv, "scoped_tool", study_id=SID, chat_id=CHAT)
    assert get_binding_store().lookup_study(CHAT) == SID


def test_session_resolution_itself_never_binds_for_an_agent(env):
    """Defence in depth under the middleware: even a direct explicit id binds nothing."""
    from ai_hydro.mcp.app import ACTIVE_AGENT
    from ai_hydro.mcp.helpers import _resolve_session

    get_binding_store().bind(CHAT, OTHER)
    from ai_hydro.mcp.app import ACTIVE_STUDY_ID
    from ai_hydro.mcp.helpers import AgentContextMismatchError

    token = ACTIVE_AGENT.set({"agent_id": "ag_1"})
    t2 = ACTIVE_STUDY_ID.set(SID)
    try:
        assert _resolve_session(SID, CHAT) == SID
        with pytest.raises(AgentContextMismatchError):          # the resolver itself refuses another study
            _resolve_session(OTHER, CHAT)
    finally:
        ACTIVE_STUDY_ID.reset(t2)
        ACTIVE_AGENT.reset(token)
    assert get_binding_store().lookup_study(CHAT) == OTHER
    assert _resolve_session(SID, CHAT) == SID                                  # no agent: binds as before
    assert get_binding_store().lookup_study(CHAT) == SID


def test_evaluation_runs_refuse_agents_and_plain_calls_are_untouched(env, policy_rows, monkeypatch):
    srv = _server()
    before = _call(srv, "read_tool", study_id=SID)
    monkeypatch.setenv("AIHYDRO_EVAL_NONCE", "n")
    body = _err(_call(srv, "read_tool", agent=AGENT, study_id=SID))
    assert body["code"] == AGENT_IN_EVALUATION
    plain = _call(srv, "read_tool", study_id=SID)                              # plain: not the policy's business
    assert not plain.is_error and plain.data == before.data
    assert not _call(srv, "lead_tool", study_id=SID).is_error                  # not refused as lead either


def test_the_real_server_refuses_claim_authoring_to_an_agent(env):
    from binding_helpers import call

    for tool, args in (
        ("add_claim", {"claim_id": "c1", "statement": "x"}),
        ("update_claim_status", {"claim_id": "c1", "status": "supported"}),
        ("write_research_interpretation", {"interpretation": "x"}),
        ("register_research_plan", {}),
        ("aihydro_rebind_chat", {"study_id": SID}),
        ("aihydro_record_agent_run", {"record": {"agent_id": "x"}}),
    ):
        res, body = call(tool, args, meta={META: {"study_id": SID, "chat_id": CHAT, "agent": AGENT}})
        assert res.is_error, tool
        assert body["code"] == AGENT_NOT_DELEGABLE, (tool, body)
        assert tool in body["message"] and "lead" in body["message"].lower()
    s = HydroSession.load(SID)
    assert not s.claims, "a refused add_claim must write nothing"
    assert get_binding_store().lookup_study(CHAT) is None
    # a read tool still works for the agent, and the same claim call without `agent` is not refused here
    res, body = call("list_claims", {"session_id": SID}, meta={META: {"study_id": SID, "agent": AGENT}})
    assert not res.is_error
    res, body = call("add_claim", {"claim_id": "c1", "statement": "x"}, meta={META: {"study_id": SID}})
    assert not (body or {}).get("code") == AGENT_NOT_DELEGABLE


# ----------------------------------------------------------------- sealing and the plain-call guarantee
def _latest_extra(session_id=SID, tool="scoped_tool"):
    rows = HydroSession.load(session_id).get("_run_log") or {}
    mine = [r for r in rows.values() if r.get("tool_name") == tool]
    assert mine, "no run-log row for the call"
    return mine[-1]["record"]["extra"], mine[-1]


def test_agent_identity_is_sealed_and_a_plain_calls_record_differs_by_that_key_only(env, policy_rows):
    srv = _server(record=True)
    plain = _call(srv, "scoped_tool", study_id=SID)
    assert not plain.is_error
    plain_extra, plain_row = _latest_extra()
    child = _call(srv, "scoped_tool", agent=AGENT, study_id=SID)
    assert not child.is_error
    child_extra, child_row = _latest_extra()

    assert "agent" not in plain_extra                                  # a plain call: no trace of the feature
    assert child_extra["agent"] == {**parse_agent(AGENT), "asserted_by": "client"}
    # the whole difference between the two records' provenance fields is that one key
    assert set(child_extra) - set(plain_extra) == {"agent"}
    assert set(plain_extra) - set(child_extra) == set()
    # both seal and verify; the digest covers the agent (editing it breaks the seal)
    for row in (plain_row, child_row):
        v = rr.verify_run_log_entry(row)
        assert v["has_record"] and v["record_ok"], v
    forged = json.loads(json.dumps(child_row))
    forged["record"]["extra"]["agent"]["agent_id"] = "someone_else"
    assert not rr.verify_run_log_entry(forged)["record_ok"]


def test_a_plain_call_never_reaches_the_agent_branch(env, policy_rows, monkeypatch):
    """Kill condition: nothing about a plain call's context or record changes."""
    seen = []
    orig = rr.build_run_record

    def spy(**kw):
        seen.append(kw["extra"])
        return orig(**kw)

    monkeypatch.setattr(rr, "build_run_record", spy)
    _call(_server(record=True), "scoped_tool", study_id=SID, chat_id=CHAT, client="vscode/1")
    assert seen and all("agent" not in e for e in seen)
    assert {"context_source", "context_client", "context_study_id"} <= set(seen[-1])


# ----------------------------------------------------------------- agent runs and the capsule
def test_record_agent_run_is_insert_only(env):
    import ai_hydro.mcp.tools_session as ts

    rec = {"agent_id": "ag_1", "role": "explorer", "status": "completed", "model_requested": "m"}
    first = ts.aihydro_record_agent_run(rec, session_id=SID)
    assert first["status"] == "inserted" and first["asserted_by"] == "client"
    assert ts.aihydro_record_agent_run(dict(rec), session_id=SID)["status"] == "noop"
    clash = ts.aihydro_record_agent_run({**rec, "status": "failed"}, session_id=SID)
    assert clash["error"] and clash["code"] == "AGENT_RUN_CONFLICT"
    assert ts.aihydro_record_agent_run({"role": "x"}, session_id=SID)["code"] == "AGENT_RUN_INVALID"
    assert ts.aihydro_record_agent_run({"agent_id": "x", "n": float("nan")}, session_id=SID)["error"]
    rows = agent_runs.read_all(SID)
    assert list(rows) == ["ag_1"] and agent_runs.verify_row(rows["ag_1"])
    assert rows["ag_1"]["record_digest"] == first["record_digest"]
    # a study that never had an agent keeps a run log without the table
    assert agent_runs.read_all(OTHER) == {}
    import sqlite3

    assert not store._run_log_db_path(OTHER).exists() or "agent_runs" not in {
        r[0] for r in sqlite3.connect(str(store._run_log_db_path(OTHER))).execute(
            "SELECT name FROM sqlite_master")}


def _replay(capsule: Path, *args: str):
    proc = subprocess.run([sys.executable, str(capsule / "replay.py"), *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


ABS = re.compile(r"(/Users/|/home/|/private/|/var/folders/|[A-Za-z]:\\\\)")


def test_capsule_holds_agents_passes_the_privacy_scan_and_replays(env, monkeypatch, tmp_path):
    import ai_hydro.mcp.tools_session as ts
    from ai_hydro.capsule import privacy
    from ai_hydro.capsule.manifest import verify_manifest
    from ai_hydro.mcp.enforcement import post_run
    from ai_hydro.mcp.helpers import _session_store

    _rows(monkeypatch, sig_tool="scoped")
    srv = _server(record=True)

    @srv.tool()
    def sig_tool(session_id: str, q_mean: float = 2.5) -> dict:
        data = {"q_mean": q_mean, "baseflow_index": 0.41, "flag": "ok"}
        res = {"data": dict(data), "meta": {"tool": "sig_tool", "params": {}}}
        _session_store(session_id, "signatures", res, tool_name="sig_tool")
        return post_run("sig_tool", session_id, {"data": dict(data), "meta": res["meta"]})

    child = _call(srv, "sig_tool", {"session_id": SID}, agent=AGENT, study_id=SID)
    assert not child.is_error
    _extra, row = _latest_extra(tool="sig_tool")
    assert row["record"]["extra"]["agent"]["agent_id"] == "ag_1"

    secret = f"{tmp_path}/private/workspace/notes.txt"
    ts.aihydro_record_agent_run({
        "agent_id": "ag_1", "role": "explorer", "status": "completed",
        "prompt": f"read {secret}", "cwd": secret,
        "runs_created": [row["record"]["run_id"]], "approvals": {"read": 2}}, session_id=SID)
    ts.aihydro_record_agent_run({"agent_id": "ag/2 odd:name", "status": "failed"}, session_id=SID)

    out = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "capsule"))
    assert "error" not in out, out
    cap = Path(out["capsule_dir"])
    files = sorted(p.name for p in (cap / "agents").iterdir())
    assert len(files) == 2 and "ag_1.json" in files
    manifest = json.loads((cap / "capsule_manifest.json").read_text())
    assert {f["path"] for f in manifest["files"]} >= {f"agents/{n}" for n in files}      # hashed like any file
    section = manifest["agent_runs"]
    assert section["runs"] == 2 and section["verified"] == 2 and section["prompts_included"] is False
    assert verify_manifest(cap)[0]

    doc = json.loads((cap / "agents" / "ag_1.json").read_text())
    assert doc["asserted_by"] == "client" and doc["record_digest"].startswith("sha256:")
    assert "prompt" not in doc["record"] and doc["prompt_withheld"] is True
    assert doc["record"]["prompt_sha256"]                                                # the hash stays
    for f in (cap / "agents").iterdir():                                                 # privacy scan
        assert not ABS.search(f.read_text()), f.name
    assert privacy.scrub_exported_text(cap, None) == 0

    code, text = _replay(cap)
    assert code in (0, 3), text
    assert "FAIL" not in text, text

    # prompts only behind the flag, still path-scrubbed
    out2 = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "capsule2"),
                             include_agent_prompts=True)
    doc2 = json.loads((Path(out2["capsule_dir"]) / "agents" / "ag_1.json").read_text())
    assert "prompt" in doc2["record"] and not ABS.search(json.dumps(doc2))


def test_a_study_without_agents_exports_no_agents_directory(env, tmp_path):
    import ai_hydro.mcp.tools_session as ts

    out = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "cap"))
    assert "error" not in out, out
    cap = Path(out["capsule_dir"])
    assert not (cap / "agents").exists()
    assert "agent_runs" not in json.loads((cap / "capsule_manifest.json").read_text())


# ----------------------------------------------------------------- Stage 1 review fixes
ALIASES = ["session", "sid", "sessionId", "Session_Id", "sesion_id", "session-id"]


@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize("tool", ["list_claims", "get_session_summary", "get_session_health",
                                  "compute_drought_index"])
def test_no_alias_spelling_reaches_another_study(env, tool, alias):
    from binding_helpers import call

    meta = {META: {"study_id": SID, "chat_id": CHAT, "agent": AGENT}}
    res, body = call(tool, {alias: OTHER}, meta=meta)
    assert body is not None and body.get("code") == AGENT_CONTEXT_MISMATCH, (tool, alias, body)
    assert get_binding_store().lookup_study(CHAT) is None
    # the same alias naming the call's own study is not a mismatch
    res, body = call(tool, {alias: SID}, meta=meta)
    assert (body or {}).get("code") != AGENT_CONTEXT_MISMATCH, (tool, alias, body)
    # a plain call may still name another study
    res, body = call(tool, {alias: OTHER}, meta={META: {"study_id": SID}})
    assert (body or {}).get("code") != AGENT_CONTEXT_MISMATCH, (tool, alias, body)


@pytest.mark.parametrize("tool,args", [
    ("search_experiments", {"project_name": "p", "query": "q"}),
    ("search_experiments", {"project_name": "p", "query": "q", "compare_sessions": True}),
    ("search_literature", {"project_name": "p", "query": "q"}),
    ("audit_interpretation", {"prose": "x", "outcome": "pass"}),
    ("audit_interpretation", {"prose": "x", "unmet": ["a"]}),
    ("aihydro_chat_status", {"chat_id": "01OTHERCHAT"}),
    ("map_get_state", {"chat_id": "01OTHERCHAT"}),
    ("map_get_state", {"workspace": "/elsewhere"}),
    ("map_get_state", {"scope": "project"}),
])
def test_identifier_arguments_that_leave_the_study_are_refused(env, tool, args):
    from binding_helpers import call

    meta = {META: {"study_id": SID, "chat_id": CHAT, "workspace": "/ws", "agent": AGENT}}
    res, body = call(tool, args, meta=meta)
    assert body is not None and body.get("code") == AGENT_CONTEXT_MISMATCH, (tool, args, body)


def test_the_call_contexts_own_chat_and_workspace_are_fine(env):
    from binding_helpers import call

    meta = {META: {"study_id": SID, "chat_id": CHAT, "workspace": "/ws", "agent": AGENT}}
    for tool, args in (("aihydro_chat_status", {"chat_id": CHAT}),
                       ("map_get_state", {"chat_id": CHAT, "workspace": "/ws"})):
        res, body = call(tool, args, meta=meta)
        assert (body or {}).get("code") != AGENT_CONTEXT_MISMATCH, (tool, body)
    # plain calls are untouched
    res, body = call("map_get_state", {"scope": "project"}, meta={META: {"study_id": SID}})
    assert (body or {}).get("code") != AGENT_CONTEXT_MISMATCH


def test_record_agent_run_no_op_returns_the_stored_digest_cap_and_allowlist(env, tmp_path, monkeypatch):
    import ai_hydro.mcp.tools_session as ts

    rec = {"agent_id": "a1", "status": "completed", "mystery_field": {"x": 1}}
    first = ts.aihydro_record_agent_run(rec, session_id=SID)
    again = ts.aihydro_record_agent_run(dict(rec), session_id=SID)
    assert again["status"] == "noop" and again["record_digest"] == first["record_digest"]
    assert agent_runs.read_all(SID)["a1"]["record_digest"] == first["record_digest"]

    out = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "cap"))
    doc = json.loads((Path(out["capsule_dir"]) / "agents" / "a1.json").read_text())
    assert doc["record"] == {"agent_id": "a1", "status": "completed"}
    assert doc["withheld_fields"] == ["mystery_field"]
    assert doc["digest_verified_at_export"] is True

    monkeypatch.setattr(agent_runs, "MAX_RUNS_PER_STUDY", 2)
    assert ts.aihydro_record_agent_run({"agent_id": "a2"}, session_id=SID)["status"] == "inserted"
    over = ts.aihydro_record_agent_run({"agent_id": "a3"}, session_id=SID)
    assert over["code"] == "AGENT_RUN_LIMIT"
    assert ts.aihydro_record_agent_run(dict(rec), session_id=SID)["status"] == "noop"   # existing ids still no-op


def test_the_agent_run_recorder_is_not_on_an_evaluation_arms_surface():
    from ai_hydro.mcp import eval_condition as ec

    assert "aihydro_record_agent_run" in ec.HIDDEN_ALL_ARMS
    assert "aihydro_record_agent_run" in ec.hidden_tools("C3", ["aihydro_record_agent_run", "add_claim"])
