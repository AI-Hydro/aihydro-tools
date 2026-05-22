"""
aihydro-bench v0 — 20-task scientific correctness benchmark.

Run fixture tasks (no network, every push):
    pytest tests/test_bench.py -m bench -v

Run live tasks (nightly CI, needs USGS APIs):
    pytest tests/test_bench.py -m bench_live -v

Each task's expected output is human-adjudicated; ranges and values are
sourced from published CAMELS statistics and USGS StreamStats metadata.
See bench/tasks.yaml for full rationale per task.
"""
from __future__ import annotations

import pytest
import yaml
from pathlib import Path

BENCH_DIR = Path(__file__).parent.parent / "bench"
TASKS_FILE = BENCH_DIR / "tasks.yaml"

# ---------------------------------------------------------------------------
# Load tasks at collection time
# ---------------------------------------------------------------------------

def _load_tasks(mark: str | None = None) -> list[dict]:
    with open(TASKS_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    tasks = data.get("tasks", [])
    if mark:
        tasks = [t for t in tasks if t.get("mark") == mark]
    return tasks


FIXTURE_TASKS = _load_tasks("bench")
LIVE_TASKS = _load_tasks("bench_live")


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _build_session(session_id: str, setup: dict) -> None:
    """Pre-populate a HydroSession with synthetic slot data for validator tests."""
    from ai_hydro.session.store import HydroSession
    session = HydroSession(session_id)
    for slot_name, slot_data in setup.get("slots", {}).items():
        session.set(slot_name, slot_data)
    for claim_id, claim_data in setup.get("claims", {}).items():
        session.claims[claim_id] = claim_data
    session.save()


def _call_mcp_tool(tool_name: str, kwargs: dict) -> dict:
    """Resolve and call an MCP tool function by name."""
    import ai_hydro.mcp  # trigger @mcp.tool() registration
    # Import from the module where the tool is defined
    _TOOL_MODULES = [
        "ai_hydro.mcp.tools_analysis",
        "ai_hydro.mcp.tools_validators",
        "ai_hydro.mcp.tools_ledger",
        "ai_hydro.mcp.tools_knowledge",
        "ai_hydro.mcp.tools_session",
        "ai_hydro.mcp.tools_modelling",
        "ai_hydro.mcp.tools_project",
        "ai_hydro.mcp.tools_skills",
        "ai_hydro.mcp.tools_workflows",
        "ai_hydro.mcp.tools_execution",
    ]
    import importlib
    for mod_name in _TOOL_MODULES:
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, tool_name, None)
        if fn is not None:
            return fn(**kwargs)
    raise RuntimeError(f"MCP tool '{tool_name}' not found in any tool module")


def _call_enforcement_fn(task: dict, tmp_path) -> dict:
    """
    Test the post_run enforcement layer in isolation.

    Builds the session (to give validators something to read), then calls
    post_run() with a synthetic seed_result as if the Tier 1 tool had just
    returned it.  No actual MCP tool is invoked — pure enforcement testing.
    """
    import ai_hydro.mcp  # trigger validator registration
    from ai_hydro.mcp.enforcement import post_run

    setup = task.get("setup", {})
    call = task["call"]
    session_id = setup["session_id"]
    _build_session(session_id, setup)

    seed_result = dict(call.get("seed_result", {}))
    tool_name = call["tool_name"]
    return post_run(tool_name, session_id, seed_result)


def _call_compute_fn(task: dict) -> dict:
    """
    Call an underlying compute_* function with synthetic data.

    call.fn maps to a function in ai_hydro.analysis.signatures.
    call.data selects the synthetic series (humid or arid).
    """
    from ai_hydro.analysis import signatures as sig_mod
    from bench.synthetic import (
        humid_daily_q_mm,
        arid_daily_q_mm,
        humid_daily_p_mm,
    )

    call = task["call"]
    fn_name = call["fn"]
    data_key = call.get("data", "humid")

    q = humid_daily_q_mm() if data_key == "humid" else arid_daily_q_mm()
    fn = getattr(sig_mod, fn_name)

    if fn_name == "compute_water_balance_camels":
        rr_target = call.get("p_runoff_ratio", 0.42)
        p = humid_daily_p_mm(q, runoff_ratio=rr_target)
        return fn(q, p)

    return fn(q)


# ---------------------------------------------------------------------------
# Parametrised fixture-mode tests
# ---------------------------------------------------------------------------

@pytest.mark.bench
@pytest.mark.parametrize("task", FIXTURE_TASKS, ids=[t["id"] for t in FIXTURE_TASKS])
def test_bench_fixture(task: dict, tmp_path, monkeypatch) -> None:
    """
    Run a fixture-mode bench task.

    No network access.  Result is compared against adjudicated assertions
    in tasks.yaml using the oracle evaluator.
    """
    from bench.oracle import assert_all

    task_id = task["id"]
    call_style = task["call_style"]
    setup = task.get("setup", {})
    call = task["call"]
    assertions = task["assertions"]

    # Override session storage to tmp_path so tests don't pollute ~/.aihydro
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    import ai_hydro.session.store as _store
    monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

    if call_style == "session_op":
        session_id = setup["session_id"]
        _build_session(session_id, setup)
        result = _call_mcp_tool(call["tool"], call["kwargs"])

    elif call_style == "mcp_tool":
        result = _call_mcp_tool(call["tool"], call["kwargs"])

    elif call_style == "compute_fn":
        result = _call_compute_fn(task)

    elif call_style == "enforcement_fn":
        result = _call_enforcement_fn(task, tmp_path)

    else:
        pytest.fail(f"Unknown call_style: {call_style!r}")

    assert_all(result, assertions, task_id)


# ---------------------------------------------------------------------------
# Parametrised live-mode tests
# ---------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.bench_live
@pytest.mark.parametrize("task", LIVE_TASKS, ids=[t["id"] for t in LIVE_TASKS])
def test_bench_live(task: dict, tmp_path, monkeypatch) -> None:
    """
    Run a live-mode bench task (requires network).

    Calls real external APIs (USGS NWIS, NLDI, GridMET).
    Marked bench_live so it runs in the nightly CI job only.
    """
    from bench.oracle import assert_all

    task_id = task["id"]
    call = task["call"]
    setup = task.get("setup", {})
    assertions = task["assertions"]

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    import ai_hydro.session.store as _store
    monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

    if setup:
        session_id = setup.get("session_id", f"bench-live-{task_id}")
        _build_session(session_id, setup)

    result = _call_mcp_tool(call["tool"], call["kwargs"])
    assert_all(result, assertions, task_id)
