"""Evaluation condition layer (P1 / W2): arms, marker gating, sealing order.

Probe servers are wired like ``ai_hydro.mcp.app`` (context middleware, then the eval
layer, then run records) with the real middleware classes; one test also checks the
registration order and tool lists on the real app. Synthetic fixtures only.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from ai_hydro.mcp import app
from ai_hydro.mcp import eval_condition as ec
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession

SID = "evalsess"
NONCE = "nonce-for-tests-123"
META = "aihydro/context"
ALL_TOOLS = ["probe", "probe_list", "add_claim", "write_research_interpretation",
             "promote_claim_to_registry", "list_registry_claims", "check_registry_staleness",
             "check_unit_consistency", "check_record_length", "run_skeptic", "audit_interpretation",
             "register_research_plan"]
REGISTRY = {"promote_claim_to_registry", "list_registry_claims", "check_registry_staleness"}
C1_EXTRA = {"check_unit_consistency", "check_record_length", "run_skeptic", "audit_interpretation",
            "register_research_plan"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AIHYDRO_HOME", str(h))
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.delenv(ec.NONCE_ENV, raising=False)
    HydroSession(SID).save()
    return h


def arm(home, monkeypatch, condition, nonce=NONCE, env_nonce=NONCE):
    (home / ec.MARKER_FILE).write_text(json.dumps({"schema": "aihydro.eval_home/1",
                                                   "condition": condition, "nonce": nonce}))
    if env_nonce is None:
        monkeypatch.delenv(ec.NONCE_ENV, raising=False)
    else:
        monkeypatch.setenv(ec.NONCE_ENV, env_nonce)


def label(condition):
    return ec.expected_client_label(condition, NONCE)


@pytest.fixture
def server(home):
    from ai_hydro.mcp.enforcement import post_run

    srv = FastMCP(name="eval-probe")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(ec.EvalConditionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())

    def make(name):
        def tool(session_id: str | None = None) -> dict:
            return {"data": {"tool": name}}
        tool.__name__ = name
        srv.tool(tool)

    for n in ALL_TOOLS:
        if n not in ("probe", "probe_list"):
            make(n)

    @srv.tool()
    def probe_list(session_id: str | None = None) -> list[dict]:
        return [{"n": 1}]          # array output: FastMCP derives a {result: [...]} outputSchema

    @srv.tool()
    def probe(session_id: str | None = None) -> dict:
        return post_run("probe", SID, {
            "data": {"x": 1.5}, "key_outputs": {"x": 1.5},
            "quality_flags": [{"validator": "v", "status": "warning"}],
            "promotion_check": [{"code": "EVIDENCE_REQUIRED"}],
            "skeptic_verdict": "ok", "_skeptic_advisory": "look", "skeptic_issue_count": 0,
            "next_steps": ["do something"]})

    return srv


def names(srv):
    async def run():
        async with Client(srv) as c:
            return {t.name for t in await c.list_tools()}
    return asyncio.run(run())


def call(srv, name="probe", client=None, **extra_ctx):
    ctx = {"study_id": SID, **extra_ctx}
    if client is not None:
        ctx["client"] = client

    async def run():
        async with Client(srv) as c:
            return await c.call_tool(name, {}, meta={META: ctx}, raise_on_error=False)
    return asyncio.run(run())


def refusal(res):
    """The refusal envelope of an MCP-error result (the text is the JSON envelope)."""
    assert res.is_error
    return json.loads("".join(getattr(b, "text", "") for b in res.content))


def rows():
    return HydroSession.load(SID).get("_run_log") or {}


# ---------------------------------------------------------------- tool lists

def test_tool_lists_per_arm(server, home, monkeypatch):
    assert names(server) == set(ALL_TOOLS)                     # no marker: production
    arm(home, monkeypatch, "C3")
    c3 = names(server)
    assert c3 == set(ALL_TOOLS) - {"write_research_interpretation"}
    arm(home, monkeypatch, "C2")
    c2 = names(server)
    assert c2 == c3 - REGISTRY
    arm(home, monkeypatch, "C1")
    c1 = names(server)
    assert c1 == c2 - C1_EXTRA == {"probe", "probe_list", "add_claim"}
    assert c1 < c2 < c3 < set(ALL_TOOLS)


def test_real_app_tool_lists_and_registration_order(home, monkeypatch):
    import ai_hydro.mcp  # noqa: F401  (registers every tool on the singleton)

    kinds = [type(m).__name__ for m in app.mcp.middleware]
    assert kinds.index("_ContextInjectionMiddleware") < kinds.index("EvalConditionMiddleware") \
        < kinds.index("RunRecordMiddleware"), kinds
    full = names(app.mcp)
    for needed in ("write_research_interpretation", "promote_claim_to_registry", "run_skeptic",
                   "audit_interpretation", "register_research_plan", "add_claim"):
        assert needed in full
    arm(home, monkeypatch, "C3")
    c3 = names(app.mcp)
    assert full - c3 == ec.HIDDEN_ALL_ARMS == {"write_research_interpretation", "run_python"}
    arm(home, monkeypatch, "C2")
    c2 = names(app.mcp)
    assert c3 - c2 == REGISTRY
    arm(home, monkeypatch, "C1")
    c1 = names(app.mcp)
    removed = c2 - c1
    assert C1_EXTRA <= removed
    assert all(n.startswith("check_") for n in removed - C1_EXTRA)
    assert not any(n.startswith("check_") for n in c1)
    assert "add_claim" in c1 and "update_claim_status" in c1


def test_code_execution_is_hidden_in_every_arm(home, monkeypatch):
    import ai_hydro.mcp  # noqa: F401  (registers every tool on the singleton)
    assert "run_python" in ec.HIDDEN_ALL_ARMS
    assert "run_python" in names(app.mcp)                        # production keeps it
    for condition in ("C1", "C2", "C3"):
        arm(home, monkeypatch, condition)
        assert "run_python" not in names(app.mcp)
    res = call(app.mcp, "run_python", client=label("C3"))
    assert res.is_error and "Unknown tool" in str(res.content)


def test_hidden_tool_cannot_be_called_directly(server, home, monkeypatch):
    arm(home, monkeypatch, "C1")
    res = call(server, "run_skeptic", client=label("C1"))
    assert res.is_error and "Unknown tool" in str(res.content)
    arm(home, monkeypatch, "C3")
    assert call(server, "write_research_interpretation", client=label("C3")).is_error
    assert not call(server, "promote_claim_to_registry", client=label("C3")).is_error


# ---------------------------------------------------------------- inactive cases

def test_unset_env_nonce_is_a_complete_noop(server, home, monkeypatch):
    arm(home, monkeypatch, "C1", env_nonce=None)         # marker present, env unset: production
    assert names(server) == set(ALL_TOOLS)
    res = call(server)                                   # no client label, nothing refused
    assert not res.is_error
    sc = res.structured_content
    assert {"quality_flags", "promotion_check", "next_steps", "skeptic_verdict"} <= set(sc)
    assert sc["_run_id"]


def test_unset_env_nonce_touches_no_filesystem(home, monkeypatch):
    import ai_hydro.registry.paths as paths
    monkeypatch.setattr(paths, "aihydro_home", lambda: (_ for _ in ()).throw(AssertionError("fs access")))
    assert ec.active_state() is None


@pytest.mark.parametrize("variant", ["no_marker", "wrong_nonce", "malformed_marker", "non_dict",
                                     "wrong_schema", "unreadable"])
def test_env_nonce_set_with_bad_marker_fails_closed(server, home, monkeypatch, variant):
    marker = home / ec.MARKER_FILE
    monkeypatch.setenv(ec.NONCE_ENV, NONCE)
    if variant == "wrong_nonce":
        arm(home, monkeypatch, "C1", nonce="marker-nonce", env_nonce="different")
    elif variant == "malformed_marker":
        marker.write_text("{not json")
    elif variant == "non_dict":
        marker.write_text("[1, 2]")
    elif variant == "wrong_schema":
        marker.write_text(json.dumps({"schema": "other/1", "condition": "C3", "nonce": NONCE}))
    elif variant == "unreadable":
        marker.mkdir()                                   # a directory: read_text raises
    assert names(server) == set()
    for client in (None, label("C1"), label("C3")):
        res = call(server, client=client)
        assert refusal(res)["code"] == ec.CONTEXT_MISMATCH
    assert rows() == {}


def test_marker_deleted_mid_run_fails_closed(server, home, monkeypatch):
    arm(home, monkeypatch, "C1")
    assert names(server) == {"probe", "probe_list", "add_claim"}
    (home / ec.MARKER_FILE).unlink()
    assert names(server) == set()
    assert refusal(call(server, client=label("C1")))["code"] == ec.CONTEXT_MISMATCH


def test_arm_comes_from_marker_not_env(server, home, monkeypatch):
    arm(home, monkeypatch, "C3")
    monkeypatch.setenv("AIHYDRO_EVAL_CONDITION", "C1")        # a free env var must not matter
    monkeypatch.setenv("AIHYDRO_CONDITION", "C1")
    assert "run_skeptic" in names(server)


def test_matching_nonce_with_invalid_condition_fails_closed(server, home, monkeypatch):
    arm(home, monkeypatch, "C9")
    assert names(server) == set()
    res = call(server, client=label("C9"))
    assert refusal(res)["code"] == ec.CONTEXT_MISMATCH
    assert rows() == {}


# ---------------------------------------------------------------- stripping vs sealing

STRIPPED = {"quality_flags", "promotion_check", "next_steps", "skeptic_verdict",
            "_skeptic_advisory", "skeptic_issue_count"}


def test_c1_strips_after_sealing_and_keeps_run_id(server, home, monkeypatch):
    arm(home, monkeypatch, "C1")
    res = call(server, client=label("C1"))
    assert not res.is_error
    sc = res.structured_content
    assert not (STRIPPED & set(sc)), sc
    assert sc["data"] == {"x": 1.5} and sc["_run_id"]
    assert not any(k in json.dumps([getattr(b, "text", "") for b in res.content]) for k in
                   ("quality_flags", "promotion_check", "next_steps", "skeptic"))
    # the sealed row (read through the public session API) still has what the agent was not shown
    row = rows()[sc["_run_id"]]
    assert row["key_outputs"]["_quality_flags"] == [{"validator": "v", "status": "warning"}]
    assert row["evidence"]["quality_flags"] == [{"validator": "v", "status": "warning"}]
    assert row["record"]["extra"]["context_client"] == label("C1")
    assert label("C1") == f"p1eval/C1/{ec.nonce_digest(NONCE)}"
    assert len(ec.nonce_digest(NONCE)) == 16 and NONCE not in label("C1")


@pytest.mark.parametrize("condition", ["C2", "C3"])
def test_c2_c3_keep_every_field(server, home, monkeypatch, condition):
    arm(home, monkeypatch, condition)
    sc = call(server, client=label(condition)).structured_content
    assert STRIPPED <= set(sc) and sc["_run_id"]
    assert rows()[sc["_run_id"]]["record"]["extra"]["context_client"] == label(condition)


def test_kernel_output_identical_across_arms(server, home, monkeypatch):
    seen = {}
    for condition in (None, "C3", "C2", "C1"):
        if condition:
            arm(home, monkeypatch, condition)
        sc = call(server, client=label(condition) if condition else None).structured_content
        row = rows()[sc["_run_id"]]
        seen[condition] = (row["record"]["output_digest"], row["record"]["input_digest"],
                           row["key_outputs"], row["evidence"])
    assert len({json.dumps(v, sort_keys=True) for v in seen.values()}) == 1, seen


# ---------------------------------------------------------------- recursive strip (S2)

def test_strip_fields_is_recursive_and_covers_underscore_variants():
    tree = {"data": {"x": 1, "_quality_flags": [1], "inner": [{"quality_flags": [], "keep": 2,
            "deep": {"_promotion_check": 1, "_next_steps": 2, "_skeptic_x": 3, "Skeptic": 4}}]},
            "_run_id": "r1", "__quality_flags": 1, "skeptic_verdict": 1}
    out = ec.strip_fields(tree)
    assert out == {"data": {"x": 1, "inner": [{"keep": 2, "deep": {"Skeptic": 4}}]}, "_run_id": "r1"}
    assert "_quality_flags" in tree["data"]                  # input untouched


def _keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


def test_c1_result_tree_has_no_stripped_key_at_any_depth(home, monkeypatch):
    from ai_hydro.mcp.enforcement import post_run

    srv = FastMCP(name="eval-nested")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(ec.EvalConditionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())

    @srv.tool()
    def nested(session_id: str | None = None) -> dict:
        r = post_run("nested", SID, {"data": {"x": 1.0}, "key_outputs": {"x": 1.0},
                                     "quality_flags": [{"validator": "v", "status": "ok"}]})
        r["rows"] = [{"_quality_flags": [1], "sub": {"promotion_check": [], "ok": 1,
                                                       "list": [{"_skeptic_note": "n", "next_steps": []}]}}]
        return r

    arm(home, monkeypatch, "C1")
    res = call(srv, "nested", client=label("C1"))
    assert not res.is_error and res.structured_content["rows"][0]["sub"]["ok"] == 1
    bad = [k for k in _keys(res.structured_content) if ec._is_stripped(k)]
    assert bad == []
    assert not any(t in json.dumps([b.text for b in res.content]) for t in
                   ("quality_flags", "promotion_check", "next_steps", "skeptic"))


# ---------------------------------------------------------------- hidden-name scrub (S1)

def test_scrub_hidden_names_is_generic_and_recursive():
    pat = ec.hidden_name_pattern("C1")
    tree = {"_instruction": "Next call write_research_interpretation, then run_skeptic or check_record_length.",
            "next_tools": ["add_claim", "promote_claim_to_registry", "check_stationarity"],
            "by_name": {"run_skeptic": 1, "keep": "see add_claim"},
            "nested": [{"hint": "use register_research_plan"}], "n": 3}
    out = ec.scrub_hidden_names(tree, pat)
    flat = json.dumps(out)
    for name in ("write_research_interpretation", "run_skeptic", "check_record_length", "check_stationarity",
                 "promote_claim_to_registry", "register_research_plan"):
        assert name not in flat
    assert out["next_tools"] == ["add_claim"] and out["by_name"] == {"keep": "see add_claim"} and out["n"] == 3
    # per-arm: C3 hides only the all-arm tools, C2 adds the registry
    assert ec.hidden_name_pattern("C3").search("promote_claim_to_registry") is None
    assert ec.hidden_name_pattern("C3").search("run_python")
    assert ec.hidden_name_pattern("C2").search("promote_claim_to_registry")
    assert ec.hidden_name_pattern("C2").search("run_skeptic") is None
    assert ec.hidden_name_pattern("C1").search("my_check_record") is None      # no partial-word hits


@pytest.mark.parametrize("condition,named,visible", [
    ("C1", ["write_research_interpretation", "run_skeptic", "promote_claim_to_registry"], ["add_claim"]),
    ("C2", ["write_research_interpretation", "promote_claim_to_registry"], ["run_skeptic", "add_claim"]),
    ("C3", ["write_research_interpretation", "run_python"], ["run_skeptic", "promote_claim_to_registry"]),
])
def test_results_never_name_hidden_tools(home, monkeypatch, condition, named, visible):
    srv = FastMCP(name="eval-scrub")
    srv.add_middleware(app._ContextInjectionMiddleware())
    srv.add_middleware(ec.EvalConditionMiddleware())
    srv.add_middleware(app.RunRecordMiddleware())

    @srv.tool()
    def describe(session_id: str | None = None) -> dict:
        return {"_instruction": "call " + ", ".join(named + visible), "tools": named + visible}

    arm(home, monkeypatch, condition)
    res = call(srv, "describe", client=label(condition))
    assert not res.is_error
    blob = json.dumps(res.structured_content) + "".join(b.text for b in res.content)
    assert not any(n in blob for n in named), blob
    assert all(v in blob for v in visible)
    assert res.structured_content["tools"] == visible


def test_real_discovery_tools_do_not_name_hidden_tools(home, monkeypatch):
    import ai_hydro.mcp  # noqa: F401

    arm(home, monkeypatch, "C1")
    pat = ec.hidden_name_pattern("C1")

    async def run():
        async with Client(app.mcp) as c:
            out = []
            for name, args in (("aihydro_describe_capability", {"domain": "claims"}),
                               ("list_available_tools", {}),
                               ("get_session_raw_state", {"session_id": SID})):
                r = await c.call_tool(name, args, meta={META: {"client": label("C1")}}, raise_on_error=False)
                out.append((name, json.dumps(r.structured_content) + "".join(b.text for b in r.content)))
            return out
    for name, blob in asyncio.run(run()):
        assert pat.search(blob) is None, (name, pat.search(blob).group(0))


# ---------------------------------------------------------------- context sealing

@pytest.mark.parametrize("client", [None, "", "p1eval/C2/" + ec.nonce_digest(NONCE),
                                    "p1eval/C1/0000000000000000", "vscode/1"])
def test_wrong_or_missing_client_label_is_refused_and_not_recorded(server, home, monkeypatch, client):
    arm(home, monkeypatch, "C1")
    env = refusal(call(server, client=client))
    assert env["code"] == ec.CONTEXT_MISMATCH and env["error"] is True
    assert rows() == {}


# Refusals raised by the condition layer are MCP errors, so a ``-> list[dict]`` tool's
# output schema cannot replace the envelope with "Output validation error".

def _expect_code_survives(res, code):
    text = "".join(getattr(b, "text", "") for b in res.content)
    assert res.is_error and "Output validation error" not in text, text
    assert json.loads(text)["code"] == code


def test_list_tool_refusals_keep_their_code_on_every_path(server, home, monkeypatch):
    # context mismatch
    arm(home, monkeypatch, "C3")
    _expect_code_survives(call(server, "probe_list", client="wrong"), ec.CONTEXT_MISMATCH)
    _expect_code_survives(call(server, "probe_list"), ec.CONTEXT_MISMATCH)
    # the same tool is fine with the right label (array output still validates)
    ok = call(server, "probe_list", client=label("C3"))
    assert not ok.is_error and ok.structured_content == {"result": [{"n": 1}]}
    # invalid marker
    (home / ec.MARKER_FILE).unlink()
    _expect_code_survives(call(server, "probe_list", client=label("C3")), ec.CONTEXT_MISMATCH)
    # sanitise failure
    arm(home, monkeypatch, "C3")

    def boom(*a, **k):
        raise RuntimeError("boom")
    with monkeypatch.context() as m:
        m.setattr(ec, "_sanitize_tool_result", boom)
        _expect_code_survives(call(server, "probe_list", client=label("C3")), ec.CONTEXT_MISMATCH)
    # hidden tool: an MCP error as well (and not an output-validation error)
    arm(home, monkeypatch, "C1")
    res = call(server, "run_skeptic", client=label("C1"))
    assert res.is_error and "Unknown tool" in str(res.content)


# ---------------------------------------------------------------- locality

def test_eval_condition_logic_lives_only_in_eval_condition_module():
    pkg = Path(__file__).resolve().parent.parent / "ai_hydro"
    tokens = re.compile(r"AIHYDRO_EVAL_NONCE|eval_home|p1eval|EVAL_CONTEXT_MISMATCH|STRIPPED_FIELDS")
    offenders = [str(p.relative_to(pkg)) for p in pkg.rglob("*.py")
                 if p.name != "eval_condition.py" and tokens.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
    app_src = (pkg / "mcp" / "app.py").read_text(encoding="utf-8")
    assert app_src.count("add_middleware(_EvalConditionMiddleware())") == 1   # app.py only registers it
    assert app_src.count("from ai_hydro.mcp.eval_condition import") == 1
