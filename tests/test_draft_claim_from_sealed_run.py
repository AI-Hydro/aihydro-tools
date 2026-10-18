"""draft_claim_from_run finds a run sealed in the SQLite run log (the canonical store)."""
from __future__ import annotations

import json

import pytest

from test_run_record_middleware import call
from test_series_tools import SID, days, env, mcp_server, serve  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_drafts_from_a_sealed_data_fetch_run_and_stays_read_only(serve, mcp_server, env):  # noqa: F811
    body = serve(days("2020-01-01", 6), [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    rid = body["_run_id"]
    sessions = env / "sessions"
    before = {p.name: p.read_bytes() for p in sessions.glob("*.json")}
    err, out, _ = call(mcp_server, "draft_claim_from_run",
                       {"session_id": SID, "run_id": rid, "metric_ref": "n_finite"})
    assert not err and out.get("status") == "drafted", out
    template = out["claim_template"]
    assert template["evidence_spans"] == [{"source_type": "run", "source_id": rid, "metric_ref": "n_finite"}]
    assert template["claim_id"] == f"claim.{rid}.n_finite"
    assert {p.name: p.read_bytes() for p in sessions.glob("*.json")} == before   # nothing written


def test_unknown_run_id_lists_the_sealed_runs(serve, mcp_server):  # noqa: F811
    body = serve(days("2020-01-01", 3), [1.0, 2.0, 3.0])
    err, out, _ = call(mcp_server, "draft_claim_from_run",
                       {"session_id": SID, "run_id": "no-such-run", "metric_ref": "mean"})
    assert out.get("error") and body["_run_id"] in json.dumps(out)
