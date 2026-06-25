"""
Async subprocess runner for inundation physics validation jobs.

Called as: python -m ai_hydro.analysis.inundation_physics_runner <artifact_dir>
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("ai_hydro.analysis.inundation_physics_runner")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _write_status(
    artifact_dir: Path,
    job_id: str,
    status: str,
    *,
    progress: dict | None = None,
    partial_results: dict | None = None,
    error: dict | None = None,
) -> None:
    log_path = str(artifact_dir / "physics.log")
    payload = {
        "job_id": job_id,
        "status": status,
        "progress": progress or {"step": "pending", "steps_total": 3},
        "partial_results": partial_results,
        "error": error,
        "log_path": log_path,
        "updated_at": _now(),
    }
    (artifact_dir / "status.json").write_text(json.dumps(payload, indent=2))


def run(artifact_dir: Path) -> None:
    config_path = artifact_dir / "job_config.json"
    if not config_path.exists():
        sys.exit(f"job_config.json not found in {artifact_dir}")

    cfg = json.loads(config_path.read_text())
    job_id: str = cfg["job_id"]

    log_path = artifact_dir / "physics.log"
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    _write_status(
        artifact_dir,
        job_id,
        "running",
        progress={"step": "hand", "steps_total": 3},
    )

    try:
        from ai_hydro.analysis.inundation_physics import (
            compute_physics_validation_artifacts,
            benchmark_inundation_methods,
            build_physics_benchmark_report,
        )

        _write_status(
            artifact_dir,
            job_id,
            "running",
            progress={"step": "physics", "steps_total": 3},
        )
        (
            hand_mask,
            physics_mask,
            hand_summary,
            physics_summary,
            cell_size_m,
            backend,
        ) = compute_physics_validation_artifacts(cfg)
        benchmark = benchmark_inundation_methods(
            hand_mask,
            physics_mask,
            cell_size_m=cell_size_m,
            reference_label="physics",
        )
        report = build_physics_benchmark_report(
            hand_summary=hand_summary,
            physics_summary=physics_summary,
            benchmark=benchmark,
            backend=backend,
        )

        import numpy as np

        np.savez_compressed(
            artifact_dir / "validation_masks.npz",
            hand_mask=hand_mask,
            physics_mask=physics_mask,
            cell_size_m=np.array([cell_size_m], dtype=np.float64),
        )

        _write_status(
            artifact_dir,
            job_id,
            "running",
            progress={"step": "benchmark", "steps_total": 3},
        )

        session_id = cfg.get("session_id")
        if session_id:
            from ai_hydro.citations import citation_keys_for_tool
            from ai_hydro.session import HydroSession

            session = HydroSession.load(session_id)
            session.put_result(
                "inundation_physics",
                "__legacy__",
                str(report.get("csi") or "proxy"),
                report,
            )
            session.add_citations(citation_keys_for_tool("run_inundation_physics_validation"))
            session.save()

        _write_status(
            artifact_dir,
            job_id,
            "complete",
            progress={"step": "done", "steps_total": 3},
            partial_results=report,
        )
        log.info(
            "Physics validation complete: method=%s csi=%s",
            report.get("physics_method"),
            report.get("csi"),
        )

    except Exception:
        tb = traceback.format_exc()
        log.error("Physics validation failed:\n%s", tb)
        _write_status(
            artifact_dir,
            job_id,
            "failed",
            error={"code": "PHYSICS_VALIDATION_ERROR", "message": tb[-800:]},
        )
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python -m ai_hydro.analysis.inundation_physics_runner <artifact_dir>")
    run(Path(sys.argv[1]))
