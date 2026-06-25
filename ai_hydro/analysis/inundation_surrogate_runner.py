"""
Async subprocess runner for inundation surrogate training jobs.

Called as: python -m ai_hydro.analysis.inundation_surrogate_runner <artifact_dir>
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("ai_hydro.analysis.inundation_surrogate_runner")


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
    log_path = str(artifact_dir / "surrogate_train.log")
    payload = {
        "job_id": job_id,
        "status": status,
        "progress": progress or {"step": "pending", "steps_total": 2},
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
    dataset_path = Path(cfg["dataset_path"])
    framework = cfg.get("framework") or "morphology"
    max_iterations = int(cfg.get("max_iterations") or 8)

    log_path = artifact_dir / "surrogate_train.log"
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    _write_status(
        artifact_dir,
        job_id,
        "running",
        progress={"step": "train", "steps_total": 2},
    )

    try:
        from ai_hydro.analysis.inundation_surrogate import train_surrogate_from_dataset

        result = train_surrogate_from_dataset(
            dataset_path,
            output_dir=artifact_dir,
            framework=framework,
            max_iterations=max_iterations,
        )

        session_id = cfg.get("session_id")
        if session_id:
            from ai_hydro.session import HydroSession

            session = HydroSession.load(session_id)
            session.put_result("inundation_surrogate", job_id, framework, result)
            try:
                from ai_hydro.citations import citation_keys_for_tool

                session.add_citations(citation_keys_for_tool("train_inundation_surrogate"))
            except Exception:
                pass
            session.save()

        _write_status(
            artifact_dir,
            job_id,
            "complete",
            progress={"step": "done", "steps_total": 2},
            partial_results=result,
        )
        log.info(
            "Surrogate training complete: framework=%s csi=%s gain=%s",
            result.get("framework"),
            result.get("train_csi"),
            float(result.get("train_csi", 0)) - float(result.get("hand_csi_baseline", 0)),
        )
    except Exception as exc:
        tb = traceback.format_exc()
        log.error("Surrogate training failed: %s\n%s", exc, tb)
        _write_status(
            artifact_dir,
            job_id,
            "failed",
            error={"message": str(exc), "traceback": tb},
        )
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python -m ai_hydro.analysis.inundation_surrogate_runner <artifact_dir>")
    run(Path(sys.argv[1]))
