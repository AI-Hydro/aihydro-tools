"""
Validate-tier flood inundation physics MCP tools (Phase 3).

run_inundation_physics_validation — kickoff-only; returns {job_id} immediately.
get_inundation_physics_result      — poll/read completed benchmark report.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ai_hydro.mcp import jobs
from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import (
    _resolve_session,
    _tool_error_to_dict,
)

log = logging.getLogger("ai_hydro.mcp")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _pending_status(job_id: str, artifact_dir: Path) -> dict:
    log_path = artifact_dir / "physics.log"
    return {
        "job_id": job_id,
        "status": "pending",
        "progress": {"step": "pending", "steps_total": 3},
        "partial_results": None,
        "error": None,
        "log_path": str(log_path),
        "updated_at": _now(),
    }


@mcp.tool()
def run_inundation_physics_validation(
    session_id: str | None = None,
    discharge_m3s: float | None = None,
    return_period: int | None = None,
    use_design_peak: bool = False,
    use_session_peak: bool = False,
    engine: str = "sfincs",
    workspace_dir: str | None = None,
) -> dict:
    """
    Kick off a validate-tier HAND vs 2D-physics benchmark job (detached subprocess).

    Returns {job_id} immediately — wait with wait_for_job, then fetch results with
    get_inundation_physics_result. Requires delineate_watershed and a discharge driver
    (explicit discharge_m3s, return_period, use_design_peak, or use_session_peak).
    When SFINCS/LISFLOOD-FP is not installed, completes with an explicit morphological
    proxy benchmark (labeled in results — not operational physics).
    Typical runtime: 1–5 min (HAND DEM fetch) + physics when available.
    """
    try:
        session_id = _resolve_session(session_id, None)
        from ai_hydro.session import HydroSession

        session = HydroSession.load(session_id)
        if session.watershed is None:
            return {
                "error": True,
                "code": "MISSING_PREREQUISITES",
                "message": (
                    "Cannot run physics validation — missing watershed. "
                    "Run delineate_watershed first."
                ),
                "recovery": "Call delineate_watershed, then retry.",
                "next_tools": ["delineate_watershed"],
            }

        if not any(
            [
                discharge_m3s is not None,
                return_period is not None,
                use_design_peak,
                use_session_peak,
            ]
        ):
            return {
                "error": True,
                "code": "MISSING_DISCHARGE",
                "message": (
                    "Provide discharge_m3s, return_period, use_design_peak=True, "
                    "or use_session_peak=True."
                ),
                "recovery": "Set one discharge driver and retry.",
                "next_tools": ["compute_flood_frequency", "compute_design_hydrograph"],
            }

        job_id = uuid.uuid4().hex[:12]
        ws = workspace_dir or session.workspace_dir
        base = Path(ws) if ws else Path.home() / ".aihydro" / "inundation_physics"
        artifact_dir = base / "runs" / job_id

        config = {
            "job_id": job_id,
            "session_id": session_id,
            "discharge_m3s": discharge_m3s,
            "return_period": return_period,
            "use_design_peak": use_design_peak,
            "use_session_peak": use_session_peak,
            "engine": engine,
        }

        job = jobs.start_job(
            kind="inundation_physics",
            runner_module="ai_hydro.analysis.inundation_physics_runner",
            config=config,
            artifact_dir=artifact_dir,
            log_name="physics.log",
            status_seed=_pending_status(job_id, artifact_dir),
        )

        return {
            "job_id": job["job_id"],
            "status": "pending",
            "artifact_dir": job["artifact_dir"],
            "log_path": job["log_path"],
            "started_at": job["started_at"],
            "engine": engine,
            "_note": (
                f"Physics validation started. Wait with wait_for_job('{job['job_id']}'), "
                "then get_inundation_physics_result. Proxy benchmark when SFINCS absent."
            ),
            "wait_with": f"wait_for_job('{job['job_id']}')",
        }

    except Exception as e:
        log.error("run_inundation_physics_validation kickoff failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def get_inundation_physics_result(job_id: str) -> dict:
    """
    Read the benchmark report from a run_inundation_physics_validation job.

    Poll with wait_for_job instead of looping. Returns partial_results when
    status is complete (HAND summary, physics/proxy summary, CSI metrics).
    """
    try:
        status = jobs.get_job_status(job_id)
        if status.get("error"):
            return status
        state = str(status.get("status") or "").lower()
        if state in ("complete", "completed", "done"):
            return {
                "job_id": job_id,
                "status": state,
                "report": status.get("partial_results"),
                "progress": status.get("progress"),
                "log_path": status.get("log_path"),
            }
        return {
            "job_id": job_id,
            "status": state,
            "progress": status.get("progress"),
            "partial_results": status.get("partial_results"),
            "error": status.get("error"),
            "log_path": status.get("log_path"),
            "_note": "Job still running — call wait_for_job once, not in a loop.",
        }
    except Exception as e:
        log.error("get_inundation_physics_result failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def export_inundation_surrogate_dataset(
    physics_job_id: str | None = None,
    artifact_dir: str | None = None,
    synthetic_mode: bool = False,
    workspace_dir: str | None = None,
) -> dict:
    """
    Export a compact HAND → physics/proxy training dataset (Phase 3 substrate).

    Preferred path: pass ``physics_job_id`` from a completed
    ``run_inundation_physics_validation`` job (reads ``validation_masks.npz``).
    ``synthetic_mode=True`` builds an offline bench fixture without network.
    Does not train a graph model — for morphology baseline training use
    ``train_inundation_surrogate`` on the exported JSON.
    """
    try:
        from ai_hydro.analysis.inundation_surrogate import (
            export_surrogate_dataset,
            export_surrogate_from_physics_job,
        )

        if synthetic_mode:
            ws = Path(workspace_dir) if workspace_dir else Path.home() / ".aihydro" / "surrogate"
            ws.mkdir(parents=True, exist_ok=True)
            out = export_surrogate_dataset(
                {
                    "synthetic_mode": True,
                    "discharge_m3s": 800.0,
                    "engine": "sfincs",
                    "source": "synthetic_export",
                },
                output_path=ws / "inundation_surrogate_synthetic.json",
            )
            out["mode"] = "synthetic"
            return out

        ad: Path | None = Path(artifact_dir) if artifact_dir else None
        if physics_job_id and ad is None:
            status = jobs.get_job_status(physics_job_id)
            if status.get("error"):
                return status
            state = str(status.get("status") or "").lower()
            if state not in ("complete", "completed", "done"):
                return {
                    "error": True,
                    "code": "JOB_NOT_COMPLETE",
                    "message": f"Physics job {physics_job_id!r} is not complete (status={state}).",
                    "recovery": f"Call wait_for_job('{physics_job_id}') then retry.",
                }
            log_path = status.get("log_path")
            if not log_path:
                return {
                    "error": True,
                    "code": "MISSING_ARTIFACT",
                    "message": "Job status has no log_path — cannot locate validation masks.",
                }
            ad = Path(log_path).parent

        if ad is None:
            return {
                "error": True,
                "code": "MISSING_SOURCE",
                "message": (
                    "Provide physics_job_id (after completed validation), artifact_dir, "
                    "or synthetic_mode=True."
                ),
                "recovery": (
                    "run_inundation_physics_validation → wait_for_job → "
                    "export_inundation_surrogate_dataset(physics_job_id=...)"
                ),
            }

        ws = Path(workspace_dir) if workspace_dir else ad
        out = export_surrogate_from_physics_job(
            ad,
            output_path=ws / f"surrogate_{physics_job_id or ad.name}.json",
        )
        out["mode"] = "physics_job"
        return out

    except FileNotFoundError as e:
        return {
            "error": True,
            "code": "MISSING_MASKS",
            "message": str(e),
            "recovery": "Re-run run_inundation_physics_validation to regenerate validation_masks.npz.",
        }
    except Exception as e:
        log.error("export_inundation_surrogate_dataset failed: %s", e)
        return _tool_error_to_dict(e)


def _resolve_surrogate_dataset_path(
    *,
    dataset_path: str | None,
    physics_job_id: str | None,
    synthetic_mode: bool,
    workspace_dir: str | None,
) -> tuple[Path | None, dict | None]:
    """Return (dataset_path, error_dict)."""
    from ai_hydro.analysis.inundation_surrogate import export_surrogate_dataset

    if dataset_path:
        path = Path(dataset_path)
        if not path.exists():
            return None, {
                "error": True,
                "code": "MISSING_DATASET",
                "message": f"Dataset not found: {dataset_path}",
            }
        return path, None

    if synthetic_mode:
        ws = Path(workspace_dir) if workspace_dir else Path.home() / ".aihydro" / "surrogate"
        ws.mkdir(parents=True, exist_ok=True)
        out = export_surrogate_dataset(
            {
                "synthetic_mode": True,
                "discharge_m3s": 800.0,
                "engine": "sfincs",
                "source": "synthetic_train",
            },
            output_path=ws / "inundation_surrogate_train_synthetic.json",
        )
        return Path(out["dataset_path"]), None

    if physics_job_id:
        export_out = export_inundation_surrogate_dataset(
            physics_job_id=physics_job_id,
            workspace_dir=workspace_dir,
        )
        if export_out.get("error"):
            return None, export_out
        return Path(export_out["dataset_path"]), None

    return None, {
        "error": True,
        "code": "MISSING_SOURCE",
        "message": (
            "Provide dataset_path, physics_job_id (after export), or synthetic_mode=True."
        ),
        "recovery": (
            "export_inundation_surrogate_dataset(physics_job_id=...) then "
            "train_inundation_surrogate(dataset_path=...)"
        ),
    }


@mcp.tool()
def train_inundation_surrogate(
    dataset_path: str | None = None,
    physics_job_id: str | None = None,
    synthetic_mode: bool = False,
    framework: str = "morphology",
    max_iterations: int = 8,
    session_id: str | None = None,
    workspace_dir: str | None = None,
) -> dict:
    """
    Kick off a lightweight inundation surrogate training job (detached subprocess).

    Trains a **morphology baseline** that tunes HAND mask dilation against
    physics/proxy targets in an exported surrogate dataset. Returns {job_id}
    immediately — wait with wait_for_job, then get_inundation_surrogate_result.

    Provide ``dataset_path`` from export_inundation_surrogate_dataset, or
    ``physics_job_id`` to export+train, or ``synthetic_mode=True`` for offline bench.
    Typical runtime: seconds (numpy grid search, no torch).
    """
    try:
        session_id = _resolve_session(session_id, None)
        resolved, err = _resolve_surrogate_dataset_path(
            dataset_path=dataset_path,
            physics_job_id=physics_job_id,
            synthetic_mode=synthetic_mode,
            workspace_dir=workspace_dir,
        )
        if err:
            return err
        assert resolved is not None

        job_id = uuid.uuid4().hex[:12]
        from ai_hydro.session import HydroSession

        session = HydroSession.load(session_id)
        ws = workspace_dir or session.workspace_dir
        base = Path(ws) if ws else Path.home() / ".aihydro" / "inundation_surrogate"
        artifact_dir = base / "runs" / job_id

        config = {
            "job_id": job_id,
            "session_id": session_id,
            "dataset_path": str(resolved),
            "framework": framework,
            "max_iterations": int(max_iterations),
        }

        job = jobs.start_job(
            kind="inundation_surrogate",
            runner_module="ai_hydro.analysis.inundation_surrogate_runner",
            config=config,
            artifact_dir=artifact_dir,
            log_name="surrogate_train.log",
            status_seed=_pending_surrogate_status(job_id, artifact_dir),
        )

        return {
            "job_id": job["job_id"],
            "status": "pending",
            "artifact_dir": job["artifact_dir"],
            "log_path": job["log_path"],
            "dataset_path": str(resolved),
            "framework": framework,
            "started_at": job["started_at"],
            "_note": (
                f"Surrogate training started. Wait with wait_for_job('{job['job_id']}'), "
                "then get_inundation_surrogate_result. Morphology baseline only — not SWE-GNN."
            ),
            "wait_with": f"wait_for_job('{job['job_id']}')",
        }
    except Exception as e:
        log.error("train_inundation_surrogate kickoff failed: %s", e)
        return _tool_error_to_dict(e)


def _pending_surrogate_status(job_id: str, artifact_dir: Path) -> dict:
    log_path = artifact_dir / "surrogate_train.log"
    return {
        "job_id": job_id,
        "status": "pending",
        "progress": {"step": "pending", "steps_total": 2},
        "partial_results": None,
        "error": None,
        "log_path": str(log_path),
        "updated_at": _now(),
    }


@mcp.tool()
def get_inundation_surrogate_result(job_id: str) -> dict:
    """
    Read morphology-baseline training results from train_inundation_surrogate.

    Poll with wait_for_job instead of looping. Returns dilation_iterations,
    train_csi, and model_path when complete.
    """
    try:
        status = jobs.get_job_status(job_id)
        if status.get("error"):
            return status
        state = str(status.get("status") or "").lower()
        if state in ("complete", "completed", "done"):
            return {
                "job_id": job_id,
                "status": state,
                "model": status.get("partial_results"),
                "progress": status.get("progress"),
                "log_path": status.get("log_path"),
            }
        return {
            "job_id": job_id,
            "status": state,
            "progress": status.get("progress"),
            "partial_results": status.get("partial_results"),
            "error": status.get("error"),
            "log_path": status.get("log_path"),
            "_note": "Job still running — call wait_for_job once, not in a loop.",
        }
    except Exception as e:
        log.error("get_inundation_surrogate_result failed: %s", e)
        return _tool_error_to_dict(e)
