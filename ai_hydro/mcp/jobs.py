"""
Shared async-job substrate (AGENT_EXECUTION_MODEL.md §3).

The contract every long-running / parallel / cancellable tool adopts instead of
blocking the MCP transport:

    start_job(kind, runner_module, config, artifact_dir) -> {job_id, status, pid, ...}
    get_job_status(job_id) -> status.json payload (registry-first, legacy fallback)
    get_job_result(job_id) -> final artifact (or in-progress / error)
    cancel_job(job_id)     -> kills the persisted PID's process group, marks cancelled
    list_jobs(kind=None)   -> registry view, reconciled against status.json

Why this exists: a detached subprocess is the only way to run slow/parallel work
without stalling the one stdio pipe, AND to make it cancellable — but only if the
PID is *persisted*. The previous modelling implementation spawned the process and
threw the PID away, so nothing could ever be cancelled. The registry below is the
single source of truth for {pid, kind, artifact_dir, status} so cancel and
restart-recovery work across MCP server restarts.

A "job" here is transport-agnostic: the runner can be a numerical solver
(modelling) or an agent loop (subagent, §4). Same lifecycle either way.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("ai_hydro.mcp.jobs")

_JOBS_DIR = Path.home() / ".aihydro" / "jobs"
_REGISTRY = _JOBS_DIR / "registry.json"

_TERMINAL = {"complete", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Registry (single source of truth for PID + status)
# --------------------------------------------------------------------------- #
def _read_registry() -> dict:
    try:
        return json.loads(_REGISTRY.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_registry(reg: dict) -> None:
    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _REGISTRY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.replace(_REGISTRY)  # atomic


def _registry_put(job_id: str, entry: dict) -> None:
    reg = _read_registry()
    reg[job_id] = {**reg.get(job_id, {}), **entry, "updated_at": _now()}
    _write_registry(reg)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _read_status_file(artifact_dir: Path) -> dict | None:
    try:
        return json.loads((artifact_dir / "status.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #
def start_job(
    kind: str,
    runner_module: str,
    config: dict,
    artifact_dir: str | Path | None = None,
    *,
    log_name: str = "job.log",
    status_seed: dict | None = None,
) -> dict:
    """Spawn a detached job subprocess and register its PID.

    runner_module is run as ``python -m <runner_module> <artifact_dir>`` and is
    expected to read ``job_config.json`` and write ``status.json`` checkpoints
    (see ai_hydro.modelling.runner for the reference runner).
    """
    job_id = config.get("job_id") or uuid.uuid4().hex[:12]
    config = {**config, "job_id": job_id}

    if artifact_dir is None:
        artifact_dir = _JOBS_DIR / job_id
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    log_path = artifact_dir / log_name
    (artifact_dir / "job_config.json").write_text(json.dumps(config, indent=2))

    seed = status_seed or {
        "job_id": job_id,
        "status": "pending",
        "progress": None,
        "partial_results": None,
        "error": None,
        "log_path": str(log_path),
        "updated_at": _now(),
    }
    (artifact_dir / "status.json").write_text(json.dumps(seed, indent=2))

    proc = subprocess.Popen(
        [sys.executable, "-m", runner_module, str(artifact_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # own process group → killpg can cancel it
    )
    _registry_put(job_id, {
        "pid": proc.pid,
        "kind": kind,
        "runner_module": runner_module,
        "artifact_dir": str(artifact_dir),
        "status": "pending",
        "started_at": _now(),
    })
    log.info("Job %s (%s) spawned pid=%d", job_id, kind, proc.pid)

    return {
        "job_id": job_id,
        "status": "pending",
        "kind": kind,
        "pid": proc.pid,
        "artifact_dir": str(artifact_dir),
        "log_path": str(log_path),
        "started_at": _now(),
    }


def _resolve_artifact_dir(job_id: str) -> Path | None:
    """Registry-first artifact-dir lookup, with legacy path fallback."""
    entry = _read_registry().get(job_id)
    if entry and entry.get("artifact_dir"):
        return Path(entry["artifact_dir"])

    # Legacy fallback: jobs started before the registry existed.
    candidates = [Path.home() / ".aihydro" / "models" / "runs" / job_id]
    try:
        from ai_hydro.session.store import _SESSIONS_DIR
        if _SESSIONS_DIR.exists():
            for sf in _SESSIONS_DIR.glob("*.json"):
                try:
                    ws = json.loads(sf.read_text()).get("workspace_dir")
                    if ws:
                        candidates.append(Path(ws) / "runs" / job_id)
                except (OSError, json.JSONDecodeError):
                    pass
    except Exception:  # pragma: no cover - session layer optional
        pass

    for c in candidates:
        if (c / "status.json").exists():
            return c
    return None


def get_job_status(job_id: str) -> dict:
    """Return the job's status.json, annotated with liveness if it looks stale."""
    artifact_dir = _resolve_artifact_dir(job_id)
    if artifact_dir is None:
        return {
            "error": True,
            "code": "JOB_NOT_FOUND",
            "message": (
                f"No job found for job_id='{job_id}'. It may not have started yet "
                "or its artifact directory was moved."
            ),
        }

    status = _read_status_file(artifact_dir)
    if status is None:
        return {"error": True, "code": "JOB_NOT_FOUND",
                "message": f"status.json missing for job_id='{job_id}'."}

    # Reconcile: a non-terminal status whose process is gone is a crashed job.
    entry = _read_registry().get(job_id)
    if status.get("status") not in _TERMINAL and entry and not _pid_alive(entry.get("pid")):
        status["_pid_alive"] = False
        status["_note"] = "Process is no longer running but status is non-terminal — it may have crashed."
    return status


def get_job_result(job_id: str) -> dict:
    """Return the final result if complete; otherwise an in-progress/error dict."""
    status = get_job_status(job_id)
    if status.get("error"):
        return status
    state = status.get("status")
    if state == "complete":
        return {"job_id": job_id, "status": "complete",
                "result": status.get("partial_results"), "log_path": status.get("log_path")}
    if state == "failed":
        return {"job_id": job_id, "status": "failed", "error": status.get("error")}
    return {"job_id": job_id, "status": state,
            "message": f"Job {job_id} is {state}; result not ready.",
            "progress": status.get("progress")}


def cancel_job(job_id: str) -> dict:
    """Kill the job's process group by its persisted PID and mark it cancelled."""
    reg = _read_registry()
    entry = reg.get(job_id)
    if entry is None:
        return {"error": True, "code": "JOB_NOT_FOUND",
                "message": f"No registered job '{job_id}' to cancel."}

    if entry.get("status") in _TERMINAL:
        return {"job_id": job_id, "status": entry["status"],
                "message": f"Job already {entry['status']}; nothing to cancel."}

    pid = entry.get("pid")
    killed = False
    if _pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            killed = True
        except ProcessLookupError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            return {"error": True, "code": "CANCEL_FAILED",
                    "message": f"Failed to signal job '{job_id}' (pid={pid}): {e}"}

    # Mark cancelled in both status.json and the registry.
    artifact_dir = Path(entry["artifact_dir"]) if entry.get("artifact_dir") else None
    if artifact_dir and artifact_dir.exists():
        status = _read_status_file(artifact_dir) or {"job_id": job_id}
        status.update({"status": "cancelled", "updated_at": _now(),
                       "error": {"code": "CANCELLED", "message": "Cancelled by user."}})
        (artifact_dir / "status.json").write_text(json.dumps(status, indent=2))
    _registry_put(job_id, {"status": "cancelled"})

    return {"job_id": job_id, "status": "cancelled", "signalled": killed,
            "message": ("Job cancelled." if killed
                        else "Job marked cancelled (process was already gone).")}


def list_jobs(kind: str | None = None) -> dict:
    """List registered jobs (optionally filtered by kind), reconciled with status.json."""
    reg = _read_registry()
    jobs = []
    for job_id, entry in reg.items():
        if kind and entry.get("kind") != kind:
            continue
        status = entry.get("status")
        artifact_dir = entry.get("artifact_dir")
        if artifact_dir:
            sf = _read_status_file(Path(artifact_dir))
            if sf and sf.get("status"):
                status = sf["status"]
        jobs.append({
            "job_id": job_id,
            "kind": entry.get("kind"),
            "status": status,
            "pid_alive": _pid_alive(entry.get("pid")),
            "started_at": entry.get("started_at"),
            "artifact_dir": artifact_dir,
        })
    jobs.sort(key=lambda j: j.get("started_at") or "", reverse=True)
    return {"count": len(jobs), "jobs": jobs}
