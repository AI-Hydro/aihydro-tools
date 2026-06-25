"""
Subprocess runner for autoresearch jobs.

Executed by jobs.start_job(kind="autoresearch",
                           runner_module="ai_hydro.mcp.search_runner", ...).

Config keys expected (set by run_autoresearch tool):
  session_id      : str
  job_id          : str
  hypothesis      : str      (recorded in status; passed through to prereg)
  backend         : str      (hbv | nh_lstm | …)
  strategy        : str      ("random" | "grid")
  max_experiments : int | None
  budget_hours    : float | None
  proxy_epochs    : int
  search_knobs    : list[str] | None
  prereg_id       : str | None

The runner:
  1. Loads the HydroSession and extracts the basin dataset (fetch-once).
  2. Reconstructs the base ModelSpec from session periods + backend.
  3. Builds the search Budget + Strategy.
  4. Calls run_loop() — which writes leaderboard.json after every run.
  5. Writes final status.json with the winner and summary.
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path

log = logging.getLogger("ai_hydro.mcp.search_runner")


def _write_status(artifact_dir: Path, job_id: str, status: str, **extra) -> None:
    p = artifact_dir / "status.json"
    payload = {"job_id": job_id, "status": status, **extra}
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2, default=str))
    except Exception:
        pass


def run(config: dict, artifact_dir: Path) -> None:
    """
    Entry point called by the job harness.

    The harness imports this module and calls run(config, artifact_dir) in a
    subprocess, redirecting stdout/stderr to <artifact_dir>/train.log.
    """
    job_id = config.get("job_id", "unknown")
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    _write_status(artifact_dir, job_id, "running",
                  hypothesis=config.get("hypothesis", ""),
                  n_done=0, n_total=config.get("max_experiments"))

    try:
        _run_inner(config, artifact_dir, job_id)
    except Exception as exc:
        tb = traceback.format_exc()
        log.error("autoresearch job %s failed: %s\n%s", job_id, exc, tb)
        _write_status(artifact_dir, job_id, "failed",
                      error=str(exc), traceback=tb[:2000])
        sys.exit(1)


def _run_inner(config: dict, artifact_dir: Path, job_id: str) -> None:
    session_id = config["session_id"]
    backend = config.get("backend", "hbv")
    hypothesis = config.get("hypothesis", "")
    proxy_epochs = int(config.get("proxy_epochs", 50))
    max_experiments = config.get("max_experiments")
    budget_hours = config.get("budget_hours")
    strategy_name = config.get("strategy", "random")
    search_knobs = config.get("search_knobs")  # None → DEFAULT_SEARCH_KNOBS
    prereg_id = config.get("prereg_id")
    comparison_metric = config.get("comparison_metric", "nse")
    full_retrain = bool(config.get("full_retrain", True))

    # ── Load session ──────────────────────────────────────────────────────────
    from ai_hydro.session import HydroSession
    session = HydroSession.load(session_id)

    usgs_gauge_id = getattr(session, "usgs_gauge_id", None) or session_id
    workspace_dir = getattr(session, "workspace_dir", None) or str(artifact_dir.parent)

    # ── Fetch-once dataset ────────────────────────────────────────────────────
    from ai_hydro.modelling.metrics import extract_basin_data
    log.info("[search %s] extracting basin data for gauge %s", job_id, usgs_gauge_id)
    training_data, _ = extract_basin_data(session, usgs_gauge_id, artifact_dir)
    log.info("[search %s] dataset ready", job_id)

    # ── Build base ModelSpec from session + config ────────────────────────────
    from aihydro_modelling.spec import ModelSpec

    base_spec_dict = {
        "backend": backend,
        "train_start": config.get("train_start") or "2000-10-01",
        "train_end":   config.get("train_end")   or "2007-09-30",
        "test_start":  config.get("test_start")  or "2007-10-01",
        "test_end":    config.get("test_end")    or "2010-09-30",
        "epochs":      int(config.get("epochs", 300)),
        "n_restarts":  int(config.get("n_restarts", 3)),
        "learning_rate": float(config.get("learning_rate", 0.05)),
        "warm_up":     int(config.get("warm_up", 365)),
        "seed":        config.get("seed"),
    }

    # Merge any space_overrides from the tool call
    overrides = config.get("space_overrides") or {}
    base_spec_dict.update(overrides)

    if backend.startswith("nh_"):
        base_spec_dict.setdefault("val_start", config.get("val_start") or "2007-10-01")
        base_spec_dict.setdefault("val_end",   config.get("val_end")   or "2009-09-30")
        base_spec_dict.setdefault("hidden_size", int(config.get("hidden_size", 64)))
        base_spec_dict.setdefault("seq_length",  int(config.get("seq_length", 365)))
        base_spec_dict.setdefault("batch_size",  int(config.get("batch_size", 256)))

    base_spec = ModelSpec.model_validate(base_spec_dict)

    # ── Build Budget ──────────────────────────────────────────────────────────
    from aihydro_modelling.search.budget import Budget

    budget_kwargs: dict = {}
    if max_experiments is not None:
        budget_kwargs["max_experiments"] = int(max_experiments)
    if budget_hours is not None:
        budget_kwargs["max_seconds"] = float(budget_hours) * 3600
    if not budget_kwargs:
        budget_kwargs["max_experiments"] = 20  # sensible default

    budget = Budget(**budget_kwargs)

    # ── Build Strategy ────────────────────────────────────────────────────────
    from aihydro_modelling.search.strategies import make_strategy

    grid = config.get("grid")  # only used if strategy_name == "grid"
    strategy_seed = config.get("strategy_seed")
    strategy = make_strategy(strategy_name, grid=grid, seed=strategy_seed)

    # ── Status writer ─────────────────────────────────────────────────────────
    def _status_fn(progress: dict) -> None:
        _write_status(artifact_dir, job_id, "running",
                      hypothesis=hypothesis,
                      prereg_id=prereg_id,
                      **progress)

    # ── Run the loop ──────────────────────────────────────────────────────────
    from aihydro_modelling.search.loop import run_loop

    log.info("[search %s] starting loop — backend=%s, strategy=%s, budget=%s",
             job_id, backend, strategy_name, budget.summary())

    result = run_loop(
        base_spec=base_spec,
        training_data=training_data,
        strategy=strategy,
        budget=budget,
        artifact_dir=artifact_dir,
        job_id=job_id,
        search_knobs=search_knobs,
        prereg_id=prereg_id,
        proxy_epochs=proxy_epochs,
        full_retrain=full_retrain,
        comparison_metric=comparison_metric,
        write_status_fn=_status_fn,
    )

    # ── Write final status ────────────────────────────────────────────────────
    final_status = {
        "hypothesis": hypothesis,
        "prereg_id": prereg_id,
        "backend": backend,
        "strategy": strategy_name,
        "n_experiments": result["n_experiments"],
        "n_improvements": result["n_improvements"],
        "budget_exhausted": result["budget_exhausted"],
        "strategy_exhausted": result["strategy_exhausted"],
        "full_retrain_done": result["full_retrain_done"],
        "comparison_metric": comparison_metric,
        "incumbent_spec": result["incumbent_spec"],
        "incumbent_metrics": result["incumbent_metrics"],
        "incumbent_ci": result["incumbent_ci"],
    }
    _write_status(artifact_dir, job_id, "complete", **final_status)

    log.info("[search %s] complete — %d experiments, %d improvements",
             job_id, result["n_experiments"], result["n_improvements"])
