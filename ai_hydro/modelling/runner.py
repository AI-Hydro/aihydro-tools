"""
Training job subprocess runner for async train_hydro_model.

Called as: python -m ai_hydro.modelling.runner <artifact_dir>

Reads <artifact_dir>/job_config.json, runs training, writes checkpoints
and a final <artifact_dir>/status.json.  Designed to run detached so the
MCP server can return immediately with a job_id.

Status JSON schema
------------------
{
  "job_id":   str,
  "status":   "pending" | "running" | "complete" | "failed",
  "progress": {"restarts_done": int, "restarts_total": int,
               "current_nse": float | null},
  "partial_results": {...} | null,
  "error":    {"code": str, "message": str} | null,
  "log_path": str,
  "updated_at": "<iso8601>"
}
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


log = logging.getLogger("ai_hydro.modelling.runner")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _write_status(
    artifact_dir: Path,
    job_id: str,
    status: str,
    progress: dict | None = None,
    partial_results: dict | None = None,
    error: dict | None = None,
) -> None:
    log_path = str(artifact_dir / "train.log")
    payload = {
        "job_id": job_id,
        "status": status,
        "progress": progress or {"restarts_done": 0, "restarts_total": 0, "current_nse": None},
        "partial_results": partial_results,
        "error": error,
        "log_path": log_path,
        "updated_at": _now(),
    }
    status_path = artifact_dir / "status.json"
    status_path.write_text(json.dumps(payload, indent=2))


def run(artifact_dir: Path) -> None:
    config_path = artifact_dir / "job_config.json"
    if not config_path.exists():
        sys.exit(f"job_config.json not found in {artifact_dir}")

    with open(config_path) as f:
        cfg = json.load(f)

    job_id: str = cfg["job_id"]
    session_id: str = cfg["session_id"]
    framework: str = cfg.get("framework", "hbv")
    epochs: int = cfg.get("epochs", 500)
    n_restarts: int = cfg.get("n_restarts", 3)
    learning_rate: float = cfg.get("learning_rate", 0.05)
    train_start: str = cfg.get("train_start", "2000-10-01")
    train_end: str = cfg.get("train_end", "2007-09-30")
    val_start: str = cfg.get("val_start", "2000-10-01")
    val_end: str = cfg.get("val_end", "2005-09-30")
    test_start: str = cfg.get("test_start", "2007-10-01")
    test_end: str = cfg.get("test_end", "2010-09-30")
    hidden_size: int = cfg.get("hidden_size", 64)
    model: str = cfg.get("model", "cudalstm")
    sessions_dir: str | None = cfg.get("sessions_dir")

    log_path = artifact_dir / "train.log"
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    _write_status(
        artifact_dir, job_id, "running",
        progress={"restarts_done": 0, "restarts_total": n_restarts, "current_nse": None},
    )

    try:
        # Optionally redirect sessions dir for test isolation
        if sessions_dir:
            from pathlib import Path as _P
            from ai_hydro.session import store as _store
            _store._SESSIONS_DIR = _P(sessions_dir)

        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)

        usgs_gauge_id = session.site_id or session_id

        # ── M1+ path: ModelSpec-based dispatch ────────────────────────────────
        # propose_and_train embeds the full validated spec in config["spec"].
        # This path uses the clean aihydro_modelling.train() contract, attaches
        # bootstrap CI, and calls post_run() so the result lands in the run-log.
        spec_dict = cfg.get("spec")
        run_id_hint: str | None = cfg.get("run_id")

        if spec_dict:
            from aihydro_modelling.spec import ModelSpec
            from aihydro_modelling.train import train as _mod_train
            from ai_hydro.modelling.metrics import extract_basin_data

            spec = ModelSpec.model_validate(spec_dict)
            n_restarts_total = spec.n_restarts if spec.backend == "hbv" else 1

            _write_status(
                artifact_dir, job_id, "running",
                progress={"restarts_done": 0, "restarts_total": n_restarts_total,
                          "current_nse": None},
            )

            training_data, _ = extract_basin_data(session, usgs_gauge_id, artifact_dir)
            hydro_result = _mod_train(spec, training_data)
            result: dict = dict(hydro_result.data)
            result["spec"] = spec.model_dump()

            # Inject _run_id and write run-log entry via enforcement.post_run().
            try:
                from ai_hydro.mcp.enforcement import post_run as _post_run
                result = _post_run("propose_and_train", session_id, result)
            except Exception as exc:
                log.warning("post_run for propose_and_train failed: %s", exc)
                if run_id_hint:
                    result["_run_id"] = run_id_hint

            nse = result.get("nse")
            result["performance_rating"] = (
                "excellent" if nse is not None and nse >= 0.75 else
                "satisfactory" if nse is not None and nse >= 0.50 else
                "poor" if nse is not None else "unknown"
            )

            session.model = result
            from ai_hydro.citations import citation_keys_for_tool
            session.add_citations(citation_keys_for_tool("train_hydro_model"))
            session.save()

            _write_status(
                artifact_dir, job_id, "complete",
                progress={"restarts_done": n_restarts_total,
                          "restarts_total": n_restarts_total, "current_nse": nse},
                partial_results=result,
            )
            log.info("propose_and_train complete: backend=%s NSE=%.4f",
                     spec.backend, nse or 0)

        else:
            # ── Legacy path: flat-param dispatch (train_hydro_model backward compat)
            fw = (framework or "hbv").lower().replace("-", "").replace("_", "")

            if fw in ("hbv", "hbvlight", "differentiable", "hydrodl2"):
                from ai_hydro.modelling.conceptual.hbv import train_hbv_light
                result = train_hbv_light(
                    gauge_id=usgs_gauge_id,
                    session=session,
                    output_dir=artifact_dir,
                    train_start=train_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                    epochs=epochs,
                    n_restarts=n_restarts,
                    learning_rate=learning_rate,
                )
            elif fw in ("neuralhydrology", "nh", "lstm"):
                from ai_hydro.modelling.neural.lstm import train_neural_hydrology
                result = train_neural_hydrology(
                    gauge_id=usgs_gauge_id,
                    session=session,
                    output_dir=artifact_dir,
                    model=model,
                    train_start=train_start,
                    train_end=train_end,
                    val_start=val_start,
                    val_end=val_end,
                    test_start=test_start,
                    test_end=test_end,
                    epochs=epochs,
                    hidden_size=hidden_size,
                    learning_rate=learning_rate,
                )
            else:
                raise ValueError(f"Unknown framework: {framework!r}")

            # Cache result in session
            session.model = result
            from ai_hydro.citations import citation_keys_for_tool
            session.add_citations(citation_keys_for_tool("train_hydro_model"))
            session.save()

            nse = result.get("nse")
            result["performance_rating"] = (
                "excellent" if nse is not None and nse >= 0.75 else
                "satisfactory" if nse is not None and nse >= 0.50 else
                "poor" if nse is not None else "unknown"
            )

            _write_status(
                artifact_dir, job_id, "complete",
                progress={"restarts_done": n_restarts, "restarts_total": n_restarts,
                          "current_nse": nse},
                partial_results=result,
            )
            log.info("Training complete: NSE=%.4f", nse or 0)

    except Exception:
        tb = traceback.format_exc()
        log.error("Training failed:\n%s", tb)
        _write_status(
            artifact_dir, job_id, "failed",
            error={"code": "TRAINING_ERROR", "message": tb[-500:]},
        )
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python -m ai_hydro.modelling.runner <artifact_dir>")
    run(Path(sys.argv[1]))
