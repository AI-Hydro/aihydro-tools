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

A finished model's ``partial_results`` carry ``simulation_series`` (path + sha256 of the simulated
series, or why none was stored), and the run-log rows the job wrote are sealed with the same
``run_records.record_call`` the MCP middleware uses (``ai_hydro.modelling.job_seal``).
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


#: Modelling refusals recognised by exception class name (duck-typed on ``.code`` so this holds
#: whether or not the class exists in the installed aihydro-modelling release).
_MODELLING_REFUSALS = frozenset({
    "QuarantinedError", "BackendQuarantinedError", "UncertaintyHeadQuarantinedError",
    "LatitudeRequiredError", "WaterBalanceError", "InsufficientDataError",
})


def _refusal_code(exc: BaseException) -> str | None:
    """The code of an exception that is a deliberate refusal, else None (not any ``.code``)."""
    code = getattr(exc, "code", None)
    if not code:
        return None
    try:
        from aihydro_core.primitives.errors import ToolError
        if isinstance(exc, ToolError):
            return str(code)
    except ImportError:
        pass
    # Any class in the MRO named as a modelling refusal (covers subclasses too).
    if any(c.__name__ in _MODELLING_REFUSALS for c in type(exc).__mro__):
        return str(code)
    return None


def _note_simulation(seal: Any, result: dict) -> None:
    """Put the simulated series' digest in the record's ``extra`` (the capture must still be open)."""
    ref = result.get("simulation_series")
    if isinstance(ref, dict) and ref.get("status") == "stored":
        seal.note(simulation_sha256=ref.get("sha256"), simulation_scope=ref.get("scope"))


def _require_scored(result: dict) -> None:
    """A run that scored nothing is a failure, never a ``complete`` model (audit F7)."""
    if result.get("nse") is None:
        from aihydro_core.primitives.errors import ToolError
        raise ToolError(
            code="INSUFFICIENT_DATA",
            message=(
                "The model produced no test-period skill: fewer than 10 days had both an "
                "observation and a simulation inside the test window after spin-up "
                f"(scored_period={result.get('scored_period')}, n_scored={result.get('n_scored')})."
            ),
            recovery="Choose a test window the observed streamflow record covers.",
        )


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
    # Fit / select / report defaults are disjoint (match aihydro_modelling.ModelSpec).
    train_end: str = cfg.get("train_end", "2005-09-30")
    val_start: str = cfg.get("val_start", "2005-10-01")
    val_end: str = cfg.get("val_end", "2007-09-30")
    test_start: str = cfg.get("test_start", "2007-10-01")
    test_end: str = cfg.get("test_end", "2010-09-30")
    hidden_size: int = cfg.get("hidden_size", 64)
    model: str = cfg.get("model", "cudalstm")
    sessions_dir: str | None = cfg.get("sessions_dir")
    # Explicit observed-flow source; a config without the key (older job) means "session".
    streamflow_source: str = cfg.get("streamflow_source") or "session"

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

        from ai_hydro.modelling.gauge import require_gauge
        usgs_gauge_id = require_gauge(session, session_id)     # site_id or a GAUGE_NOT_SET error

        # ── M1+ path: ModelSpec-based dispatch ────────────────────────────────
        # propose_and_train embeds the full validated spec in config["spec"].
        # This path uses the clean aihydro_modelling.train() contract, attaches
        # bootstrap CI, and calls post_run() so the result lands in the run-log.
        spec_dict = cfg.get("spec")
        run_id_hint: str | None = cfg.get("run_id")

        # The rows this job writes (post_run's and the model slot's) are sealed like any tool call's
        # when the result is final: same capture, same record_call, the job's own inputs and result.
        from ai_hydro.modelling.job_seal import JobSeal
        if spec_dict:
            seal = JobSeal("propose_and_train", session_id,
                           {"session_id": session_id, "spec": spec_dict,
                            "streamflow_source": streamflow_source})
        else:
            seal = JobSeal("train_hydro_model", session_id, {
                "session_id": session_id, "framework": framework, "model": model, "epochs": epochs,
                "n_restarts": n_restarts, "learning_rate": learning_rate, "hidden_size": hidden_size,
                "train_start": train_start, "train_end": train_end, "val_start": val_start,
                "val_end": val_end, "test_start": test_start, "test_end": test_end,
                "streamflow_source": streamflow_source})
        seal.note(job_id=job_id, executed_by="background_job")

        if spec_dict:
            from aihydro_modelling.spec import ModelSpec
            from aihydro_modelling.train import train as _mod_train
            from ai_hydro.modelling.metrics import extract_basin_data, streamflow_record

            spec = ModelSpec.model_validate(spec_dict)
            n_restarts_total = spec.n_restarts if spec.backend == "hbv" else 1

            _write_status(
                artifact_dir, job_id, "running",
                progress={"restarts_done": 0, "restarts_total": n_restarts_total,
                          "current_nse": None},
            )

            training_data, _ = extract_basin_data(
                session, usgs_gauge_id, artifact_dir, streamflow_source=streamflow_source)
            from ai_hydro.modelling.spec_gate import require_trainable
            require_trainable(spec, training_data)
            hydro_result = _mod_train(spec, training_data)
            result: dict = dict(hydro_result.data)
            _require_scored(result)
            result["spec"] = spec.model_dump()
            result.update(streamflow_record(streamflow_source))
            # The simulated series, by path and sha256 (aihydro-modelling >= 0.7.0 writes the file).
            from ai_hydro.modelling.simulation_artifact import RESULT_KEY, simulation_record
            result[RESULT_KEY] = simulation_record(result, artifact_dir)

            # Inject _run_id and write run-log entry via enforcement.post_run().
            try:
                from ai_hydro.mcp.enforcement import post_run as _post_run
                result = _post_run(
                    "propose_and_train", session_id, result,
                    inputs={"streamflow_source": streamflow_source})
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
            _note_simulation(seal, result)
            seal.seal(result)
            result["seal_status"] = seal.status      # self-reported; added after the digest was taken

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

            # Same pre-flight as the spec path: the record must cover every window.
            from aihydro_modelling.spec import ModelSpec as _MS
            from ai_hydro.modelling.spec_gate import flat_to_spec_dict
            _raw, _gerr = flat_to_spec_dict(
                framework, model,
                train_start=train_start, train_end=train_end, val_start=val_start,
                val_end=val_end, test_start=test_start, test_end=test_end, epochs=epochs,
                n_restarts=n_restarts, learning_rate=learning_rate, hidden_size=hidden_size,
            )
            if _gerr:
                raise ValueError(_gerr["message"])
            legacy_spec = _MS.model_validate(_raw)

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
                    spec=legacy_spec,
                    streamflow_source=streamflow_source,
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
                    spec=legacy_spec,
                    streamflow_source=streamflow_source,
                )
            else:
                raise ValueError(f"Unknown framework: {framework!r}")
            _require_scored(result)
            from ai_hydro.modelling.metrics import streamflow_record as _sf_record
            result.update(_sf_record(streamflow_source))
            from ai_hydro.modelling.simulation_artifact import RESULT_KEY, simulation_record
            if RESULT_KEY not in result:       # the HBV path sets it; the neural path has no series to reference
                result[RESULT_KEY] = simulation_record(result, artifact_dir)

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
            _note_simulation(seal, result)
            seal.seal(result)
            result["seal_status"] = seal.status      # self-reported; added after the digest was taken

            _write_status(
                artifact_dir, job_id, "complete",
                progress={"restarts_done": n_restarts, "restarts_total": n_restarts,
                          "current_nse": nse},
                partial_results=result,
            )
            log.info("Training complete: NSE=%.4f", nse or 0)

    except Exception as exc:
        seal_note = None
        try:
            seal.close()
            seal_note = ("not sealed: the job failed before a final result; a post_run row written "
                         "before the failure stays unsealed")
        except NameError:
            pass
        tb = traceback.format_exc()
        log.error("Training failed:\n%s", tb)
        # A deliberate refusal (ToolError, InsufficientDataError) keeps its own code and full
        # message; any other exception is an unexpected TRAINING_ERROR. The traceback is kept
        # in both cases (train.log has it in full; the status carries its tail).
        code = _refusal_code(exc)
        if code:
            error = {"code": code, "message": str(exc)[:1500], "traceback": tb[-800:]}
        else:
            error = {"code": "TRAINING_ERROR", "message": tb[-500:]}
        if seal_note:
            error["seal_status"] = seal_note
        _write_status(artifact_dir, job_id, "failed", error=error)
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python -m ai_hydro.modelling.runner <artifact_dir>")
    run(Path(sys.argv[1]))
