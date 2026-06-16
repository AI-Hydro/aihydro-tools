"""
AI Modelling MCP tools (3 tools — 2.0.0).

train_hydro_model   — kickoff-only; returns {job_id} immediately (R2 compliant).
get_training_status — poll a running/finished training job.
get_model_results   — read cached model results from session.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ai_hydro.mcp import jobs
from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import (
    _normalize_session_id,
    _resolve_session,
    _tool_error_to_dict,
)

log = logging.getLogger("ai_hydro.mcp")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _pending_status(job_id: str, artifact_dir: Path) -> dict:
    log_path = artifact_dir / "train.log"
    return {
        "job_id": job_id,
        "status": "pending",
        "progress": {"restarts_done": 0, "restarts_total": 0, "current_nse": None},
        "partial_results": None,
        "error": None,
        "log_path": str(log_path),
        "updated_at": _now(),
    }


@mcp.tool()
def train_hydro_model(
    session_id: str | None = None,
    workspace_dir: str | None = None,
    framework: str = "hbv",
    model: str = "cudalstm",
    train_start: str = "2000-10-01",
    train_end: str = "2007-09-30",
    val_start: str = "2000-10-01",
    val_end: str = "2005-09-30",
    test_start: str = "2007-10-01",
    test_end: str = "2010-09-30",
    epochs: int = 500,
    n_restarts: int = 3,
    hidden_size: int = 64,
    learning_rate: float = 0.05,
) -> dict:
    """
    Kick off a model training job (detached subprocess). Returns {job_id}
    immediately — poll with get_training_status (≤1× per minute).
    Requires watershed+forcing (HBV) or watershed+streamflow+forcing (LSTM).
    framework: hbv (HBV-light, default) | neuralhydrology (LSTM, needs pip
    install neuralhydrology). model (NH only): cudalstm | ealstm | transformer.
    Typical runtime: 2-15 min (HBV), 15-60 min (LSTM).
    """
    try:
        session_id = _resolve_session(session_id, None)
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)

        fw = (framework or "hbv").lower().replace("-", "").replace("_", "")
        if fw in ("neuralhydrology", "nh", "lstm"):
            required = ("watershed", "streamflow", "forcing")
        else:
            required = ("watershed", "forcing")

        missing = [s for s in required if getattr(session, s) is None]
        if missing:
            return {
                "error": True,
                "code": "MISSING_PREREQUISITES",
                "message": (
                    f"Cannot train — missing: {missing}. "
                    "Run: "
                    + ", ".join({
                        "watershed":  "delineate_watershed",
                        "streamflow": "fetch_streamflow_data",
                        "forcing":    "fetch_forcing_data",
                    }[s] for s in missing)
                ),
            }

        job_id = uuid.uuid4().hex[:12]

        # Resolve artifact dir (preserve workspace-based location)
        ws = workspace_dir or session.workspace_dir
        base = Path(ws) if ws else Path.home() / ".aihydro" / "models"
        artifact_dir = base / "runs" / job_id

        config = {
            "job_id": job_id,
            "session_id": session_id,
            "framework": framework,
            "model": model,
            "train_start": train_start,
            "train_end": train_end,
            "val_start": val_start,
            "val_end": val_end,
            "test_start": test_start,
            "test_end": test_end,
            "epochs": epochs,
            "n_restarts": n_restarts,
            "hidden_size": hidden_size,
            "learning_rate": learning_rate,
        }

        # Spawn as a registered, cancellable job (PID persisted → cancel_job works).
        job = jobs.start_job(
            kind="training",
            runner_module="ai_hydro.modelling.runner",
            config=config,
            artifact_dir=artifact_dir,
            log_name="train.log",
            status_seed=_pending_status(job_id, artifact_dir),
        )

        return {
            "job_id": job["job_id"],
            "status": "pending",
            "artifact_dir": job["artifact_dir"],
            "log_path": job["log_path"],
            "started_at": job["started_at"],
            "_note": (
                f"Training started. Wait for it with wait_for_job('{job['job_id']}') — "
                "a single call blocks server-side at zero token cost until the job "
                "finishes; do NOT poll get_training_status in a loop. "
                f"Cancel with cancel_job('{job['job_id']}'). "
                "Typical runtime: 2-15 min (HBV), 15-60 min (LSTM)."
            ),
            "wait_with": f"wait_for_job('{job['job_id']}')",
        }

    except Exception as e:
        log.error("train_hydro_model kickoff failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def get_training_status(job_id: str) -> dict:
    """
    Poll the status of a training job started by train_hydro_model.

    Reads the status.json checkpoint written by the training subprocess.
    Poll ≤1× per minute (subprocess writes per-epoch checkpoints).
    Returns status (pending|running|complete|failed|cancelled) + progress + partial_results.
    """
    try:
        return jobs.get_job_status(job_id)
    except Exception as e:
        log.error("get_training_status failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def cancel_job(job_id: str) -> dict:
    """Cancel a running job (e.g. a training job) by killing its detached process group."""
    try:
        return jobs.cancel_job(job_id)
    except Exception as e:
        log.error("cancel_job failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def list_jobs(kind: str | None = None) -> dict:
    """List recent background jobs (job_id, kind, status, whether still running). kind filters, e.g. 'training'."""
    try:
        return jobs.list_jobs(kind)
    except Exception as e:
        log.error("list_jobs failed: %s", e)
        return _tool_error_to_dict(e)


# Terminal job states across all native job families (training, data-fetch, …).
_TERMINAL_JOB_STATES = {"complete", "completed", "done", "failed", "error", "cancelled"}

# Server-side wait budget ceiling. The VS Code MCP client times calls out at
# DEFAULT_MCP_TIMEOUT_SECONDS = 300 s; staying comfortably under it lets a single
# wait_for_job call ride out almost any job without tripping the transport timeout.
_MAX_WAIT_SECONDS = 280


def _retrieve_hint(status: dict, job_id: str) -> str:
    """Best matching result-fetch call for a finished job, keyed off its kind."""
    kind = str(status.get("kind") or "").lower()
    if "data" in kind or "fetch" in kind:
        return f"get_data_fetch_result('{job_id}')"
    if "train" in kind or "model" in kind:
        return f"get_model_results(job_id='{job_id}')"
    # Generic fallback: get_training_status returns the full status.json (incl.
    # partial_results) for any job_id, regardless of kind.
    return f"get_training_status('{job_id}')"


@mcp.tool()
def wait_for_job(job_id: str, max_wait_seconds: int = _MAX_WAIT_SECONDS, poll_interval_seconds: float = 3.0) -> dict:
    """
    Block until a background job finishes, polling its status server-side so you do
    NOT spend a turn per poll. Use this once after starting ANY async job
    (train_hydro_model, data_fetch_background, …) instead of calling
    get_job_status / get_training_status in a loop.

    The polling happens inside this single call at zero token cost. Most jobs
    finish within one wait_for_job call. If the job is still running when the wait
    budget elapses, it returns status "still_running" — just call wait_for_job
    again with the SAME job_id to keep waiting. Do not wrap this in your own loop,
    shorten poll_interval_seconds, or fall back to manual polling.

    Returns a TERSE status only (state, progress, elapsed). Fetch full results with
    the call named in `retrieve_with` once `done` is true.
    """
    try:
        budget = max(1, min(int(max_wait_seconds), _MAX_WAIT_SECONDS))
        interval = max(0.5, float(poll_interval_seconds))
        deadline = time.monotonic() + budget
        polls = 0
        while True:
            status = jobs.get_job_status(job_id)
            polls += 1
            if status.get("error"):
                return status  # JOB_NOT_FOUND etc. — surface immediately, no waiting
            state = str(status.get("status") or "").lower()
            if state in _TERMINAL_JOB_STATES:
                return {
                    "job_id": job_id,
                    "status": state,
                    "done": True,
                    "polls": polls,
                    "progress": status.get("progress"),
                    "error": status.get("error"),
                    "retrieve_with": _retrieve_hint(status, job_id),
                    "note": "Job finished. Fetch full results with the retrieve_with call.",
                }
            if time.monotonic() >= deadline:
                return {
                    "job_id": job_id,
                    "status": "still_running",
                    "done": False,
                    "waited_seconds": budget,
                    "progress": status.get("progress"),
                    "next_step": f"wait_for_job('{job_id}') again with the same job_id to keep waiting.",
                    "note": "Polling is server-side and costs no tokens — just call wait_for_job again.",
                }
            time.sleep(interval)
    except Exception as e:
        log.error("wait_for_job failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def get_model_results(session_id: str | None = None, job_id: str | None = None) -> dict:
    """
    Return cached training results: NSE, KGE, RMSE, model_dir. job_id reads
    from the job artifact dir; otherwise from session.model.
    session_id : str | None (optional from Wave 3) — auto-resolved from chat context.
    """
    try:
        session_id = _resolve_session(session_id, None)

        # If job_id provided, try to read from artifact dir first
        if job_id:
            status = get_training_status(job_id)
            if not status.get("error") and status.get("status") == "complete":
                partial = status.get("partial_results")
                if partial:
                    nse = partial.get("nse")
                    return {
                        "model_trained": True,
                        "source": "job_artifact",
                        **partial,
                        "performance_rating": (
                            "excellent" if nse is not None and nse >= 0.75 else
                            "satisfactory" if nse is not None and nse >= 0.50 else
                            "poor" if nse is not None else "unknown"
                        ),
                    }
            elif not status.get("error"):
                return {
                    "model_trained": False,
                    "job_status": status.get("status"),
                    "progress": status.get("progress"),
                    "message": f"Job {job_id} is still {status.get('status', 'running')}.",
                }

        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)

        if session.model is None:
            return {
                "error": False,
                "model_trained": False,
                "message": (
                    f"No model trained yet for session '{session_id}'. "
                    "Call train_hydro_model to start training."
                ),
                "prerequisite_status": {
                    s: (getattr(session, s) is not None) for s in
                    ("watershed", "streamflow", "forcing", "camels")
                },
            }

        result = session.model
        nse = result.get("nse")
        return {
            "model_trained": True,
            "source": "session_cache",
            **result,
            "performance_rating": (
                "excellent" if nse is not None and nse >= 0.75 else
                "satisfactory" if nse is not None and nse >= 0.50 else
                "poor" if nse is not None else "unknown"
            ),
        }

    except Exception as e:
        log.error("get_model_results failed: %s", e)
        return _tool_error_to_dict(e)

