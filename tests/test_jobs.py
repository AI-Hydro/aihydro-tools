"""Contract tests for the async-job substrate (ai_hydro.mcp.jobs)."""
from __future__ import annotations

import os
import time

import pytest

from ai_hydro.mcp import jobs

# A minimal runner runnable as `python -m dummyrunner <artifact_dir>`: it mirrors
# the real runner contract (read job_config.json, write status.json).
_RUNNER_SRC = '''
import json, sys, time
from pathlib import Path
d = Path(sys.argv[1])
cfg = json.loads((d / "job_config.json").read_text())
if cfg.get("sleep"):
    time.sleep(cfg["sleep"])
(d / "status.json").write_text(json.dumps({
    "job_id": cfg["job_id"], "status": "complete",
    "progress": None, "partial_results": {"value": 42},
    "error": None, "log_path": str(d / "job.log"),
}))
'''


@pytest.fixture
def jobs_env(tmp_path, monkeypatch):
    """Isolate the registry to tmp and put a dummy runner on the child's path."""
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "_JOBS_DIR", jobs_dir)
    monkeypatch.setattr(jobs, "_REGISTRY", jobs_dir / "registry.json")

    rpath = tmp_path / "rpath"
    rpath.mkdir()
    (rpath / "dummyrunner.py").write_text(_RUNNER_SRC)
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(rpath) + (os.pathsep + existing if existing else ""))
    return tmp_path


def test_start_status_result(jobs_env):
    art = jobs_env / "art"
    out = jobs.start_job("test", "dummyrunner", {"sleep": 0}, artifact_dir=art)
    jid = out["job_id"]
    assert out["status"] == "pending" and out["pid"] > 0

    status = {}
    for _ in range(50):
        status = jobs.get_job_status(jid)
        if status.get("status") == "complete":
            break
        time.sleep(0.1)
    assert status["status"] == "complete"

    result = jobs.get_job_result(jid)
    assert result["status"] == "complete"
    assert result["result"] == {"value": 42}

    listed = jobs.list_jobs("test")
    assert any(j["job_id"] == jid and j["status"] == "complete" for j in listed["jobs"])
    # kind filter excludes other kinds
    assert jobs.list_jobs("nonexistent-kind")["count"] == 0


def test_cancel_kills_process(jobs_env):
    art = jobs_env / "art"
    out = jobs.start_job("test", "dummyrunner", {"sleep": 30}, artifact_dir=art)
    jid, pid = out["job_id"], out["pid"]

    time.sleep(0.5)
    assert jobs._pid_alive(pid)

    res = jobs.cancel_job(jid)
    assert res["status"] == "cancelled" and res["signalled"] is True
    assert jobs.get_job_status(jid)["status"] == "cancelled"

    # idempotent: cancelling a terminal job is a no-op, not an error
    again = jobs.cancel_job(jid)
    assert again["status"] == "cancelled" and "error" not in again


def test_unknown_job(jobs_env):
    assert jobs.get_job_status("nope")["code"] == "JOB_NOT_FOUND"
    assert jobs.get_job_result("nope")["code"] == "JOB_NOT_FOUND"
    assert jobs.cancel_job("nope")["code"] == "JOB_NOT_FOUND"
