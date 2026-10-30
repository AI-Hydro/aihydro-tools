"""
AI Modelling MCP tools (M1 — 5 primary tools + 4 infrastructure tools).

Primary (Tier 1):
  describe_model_space  — introspect the full knob SearchSpace + availability.
  propose_and_train     — validate a ModelSpec then kick off a training job.
  train_hydro_model     — legacy flat-param kickoff (backward compat).
  get_model_results     — read cached model results from session.

Infrastructure (Tier 3):
  get_training_status, wait_for_job, cancel_job, list_jobs
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
from ai_hydro.registry.paths import aihydro_home

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
    train_end: str = "2005-09-30",
    val_start: str = "2005-10-01",
    val_end: str = "2007-09-30",
    test_start: str = "2007-10-01",
    test_end: str = "2010-09-30",
    epochs: int | None = None,
    n_restarts: int | None = None,
    hidden_size: int | None = None,
    learning_rate: float | None = None,
) -> dict:
    """
    Kick off a model training job (detached subprocess). Returns {job_id}
    immediately — poll with get_training_status (≤1× per minute).
    Requires watershed+forcing (HBV) or watershed+streamflow+forcing (LSTM).
    framework: hbv (HBV-light, default) | neuralhydrology (LSTM, needs pip
    install neuralhydrology). model (NH only): cudalstm | ealstm | transformer.
    epochs / learning_rate / n_restarts / hidden_size: leave unset for the backend's own
    defaults (HBV 500 epochs, lr 0.05; neural 30 epochs, lr 0.001).
    The request goes through the same gate as propose_and_train: periods must be disjoint
    (train < val < test), values in range, and the backend available.
    Typical runtime: 2-15 min (HBV), 15-60 min (LSTM).
    """
    try:
        session_id = _resolve_session(session_id, None)
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)

        # One gate for every training request (audit F6, F12): build a ModelSpec, validate it,
        # check the backend. Unset optimiser fields resolve from the backend family.
        from ai_hydro.modelling.spec_gate import flat_to_spec_dict, gate_spec
        raw, bad = flat_to_spec_dict(
            framework, model,
            train_start=train_start, train_end=train_end, val_start=val_start, val_end=val_end,
            test_start=test_start, test_end=test_end, epochs=epochs, n_restarts=n_restarts,
            hidden_size=hidden_size, learning_rate=learning_rate,
        )
        if bad:
            return bad

        # Spec errors (unknown framework/model, overlapping periods, ranges) are reported
        # before missing prerequisites so they are not hidden behind them. Without the
        # modelling package the gate is deferred: the other refusals still apply first.
        try:
            spec, bad = gate_spec(raw)
            if bad:
                return bad
        except ModuleNotFoundError:
            spec = None

        if raw["backend"] != "hbv":
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

        from ai_hydro.modelling.gauge import basin_consistency_error, gauge_error
        _gerr = gauge_error(session, session_id)
        if _gerr:
            return _gerr
        _berr = basin_consistency_error(session, session.site_id)
        if _berr:
            return _berr

        if spec is None:       # modelling package absent: raises -> MODELLING_NOT_INSTALLED
            spec, bad = gate_spec(raw)
            if bad:
                return bad

        job_id = uuid.uuid4().hex[:12]

        # Resolve artifact dir (preserve workspace-based location)
        ws = workspace_dir or session.workspace_dir
        base = Path(ws) if ws else aihydro_home() / "models"
        artifact_dir = base / "runs" / job_id

        config = {
            "job_id": job_id,
            "session_id": session_id,
            "framework": framework,
            "model": model,
            # The resolved values (the gate's spec), not the raw arguments: what runs is
            # what is recorded.
            "train_start": spec.train_start,
            "train_end": spec.train_end,
            "val_start": spec.val_start,
            "val_end": spec.val_end,
            "test_start": spec.test_start,
            "test_end": spec.test_end,
            "epochs": spec.epochs,
            "n_restarts": spec.n_restarts,
            "hidden_size": spec.hidden_size,
            "learning_rate": spec.learning_rate,
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
    if "inundation" in kind and "physics" in kind:
        return f"get_inundation_physics_result('{job_id}')"
    if "inundation" in kind and "surrogate" in kind:
        return f"get_inundation_surrogate_result('{job_id}')"
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
def describe_model_space() -> str:
    """
    Return the full model SearchSpace + per-backend availability as JSON.

    Describes every configurable knob (type, range/choices, default, which
    backends it applies to, one-line meaning) and reports whether each backend
    can actually run on this host (deps + device reality check).

    Use this before calling propose_and_train so you know the valid action space
    and don't waste compute on unavailable backends or out-of-range configs.

    Returns JSON with two keys:
      "backends": {name: {available, reason}}
      "knobs":    {name: {type, choices?, min_val?, max_val?, default,
                          backends?, description}}
    """
    try:
        import json
        import aihydro_modelling as m
        return json.dumps(m.describe_space(), indent=2, default=str)
    except Exception as e:
        log.error("describe_model_space failed: %s", e)
        import json
        return json.dumps(_tool_error_to_dict(e))


@mcp.tool()
def propose_and_train(
    spec_json: str,
    session_id: str | None = None,
    workspace_dir: str | None = None,
) -> dict:
    """
    Validate a ModelSpec then kick off a training job if the spec is valid.

    Takes the full typed spec (train_hydro_model is the flat-parameter form of the same gate):
      1. Parses spec_json → ModelSpec (typed, with per-backend conditionals).
      2. Runs static pre-flight validation BEFORE touching the GPU or disk.
         Invalid science (train/test overlap, seq_length > record, etc.) fails
         fast with a teaching error explaining WHY and HOW to fix it.
      3. Checks that the requested backend can run on this host.
      4. If valid: starts the training job and returns {job_id} immediately.
         Poll with wait_for_job(job_id); retrieve results with get_model_results.

    spec_json : str
        JSON string of ModelSpec fields.  Example:
          {"backend": "hbv", "epochs": 300, "n_restarts": 5, "seed": 42,
           "train_start": "2000-10-01", "train_end": "2005-09-30",
           "val_start": "2005-10-01",   "val_end": "2007-09-30",
           "test_start": "2007-10-01", "test_end": "2010-09-30"}
        Fit (train), select (val) and report (test) periods must be disjoint
        (endpoints inclusive); validation rejects overlaps.

        Call describe_model_space() first to see all valid fields and ranges.

    session_id : str | None
        Auto-resolved from chat context if omitted.

    Returns {job_id, status, run_id, validation_passed, ...} or
            {error: True, code: "VALIDATION_ERROR", errors: [...]} on failure.
    """
    import json as _json

    try:
        session_id = _resolve_session(session_id, None)

        # ── Parse spec ────────────────────────────────────────────────────
        import aihydro_modelling as m
        try:
            raw = _json.loads(spec_json)
        except _json.JSONDecodeError as je:
            return {
                "error": True,
                "code": "INVALID_JSON",
                "message": f"spec_json is not valid JSON: {je}",
                "hint": "Pass a JSON string — e.g. propose_and_train(spec_json='{\"backend\": \"hbv\"}')",
            }

        if not isinstance(raw, dict):
            return {
                "error": True,
                "code": "INVALID_SPEC",
                "message": "spec_json must be a JSON object of ModelSpec fields.",
                "hint": "Call describe_model_space() to see valid fields and ranges.",
            }

        # Unknown keys, static pre-flight validation and backend availability (one gate,
        # shared with train_hydro_model).
        from ai_hydro.modelling.spec_gate import gate_spec
        spec, bad = gate_spec(raw)
        if bad:
            return bad

        # ── Session prerequisite check ────────────────────────────────────
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)

        if spec.is_neural:
            required = ("watershed", "streamflow", "forcing")
        else:
            required = ("watershed", "forcing")

        missing = [s for s in required if getattr(session, s) is None]
        if missing:
            return {
                "error": True,
                "code": "MISSING_PREREQUISITES",
                "validation_passed": True,
                "message": (
                    f"Cannot train — session is missing: {missing}. "
                    "Run: "
                    + ", ".join({
                        "watershed":  "delineate_watershed",
                        "streamflow": "fetch_streamflow_data",
                        "forcing":    "fetch_forcing_data",
                    }[s] for s in missing)
                ),
            }

        from ai_hydro.modelling.gauge import basin_consistency_error, gauge_error
        _gerr = gauge_error(session, session_id)
        if _gerr:
            return _gerr
        _berr = basin_consistency_error(session, session.site_id)
        if _berr:
            return _berr

        # ── Kick off job ──────────────────────────────────────────────────
        job_id = uuid.uuid4().hex[:12]

        ws = workspace_dir or session.workspace_dir
        base = Path(ws) if ws else aihydro_home() / "models"
        artifact_dir = base / "runs" / job_id

        config = {
            "job_id": job_id,
            "session_id": session_id,
            # M1 path: runner detects "spec" key and uses ModelSpec dispatch
            "spec": spec.model_dump(),
            "run_id": job_id,  # run_id == job_id in M1; distinct in M2 loop
            # Legacy fields (kept so runner fallback works if spec parse fails)
            "framework": spec.backend if spec.backend == "hbv" else "neuralhydrology",
            "epochs": spec.epochs,
            "n_restarts": spec.n_restarts,
            "learning_rate": spec.learning_rate,
            "train_start": spec.train_start,
            "train_end": spec.train_end,
            "val_start": spec.val_start,
            "val_end": spec.val_end,
            "test_start": spec.test_start,
            "test_end": spec.test_end,
            "hidden_size": spec.hidden_size,
            "model": spec.nh_model or "cudalstm",
        }

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
            "run_id": job_id,
            "status": "pending",
            "validation_passed": True,
            "backend": spec.backend,
            "artifact_dir": job["artifact_dir"],
            "log_path": job["log_path"],
            "started_at": job["started_at"],
            "_note": (
                f"Training started (backend={spec.backend}). "
                f"Wait with wait_for_job('{job['job_id']}') — "
                "a single call blocks server-side at zero token cost until done. "
                f"Cancel with cancel_job('{job['job_id']}'). "
                "Typical runtime: 2–15 min (HBV), 15–60 min (neural)."
            ),
            "wait_with": f"wait_for_job('{job['job_id']}')",
        }

    except Exception as e:
        log.error("propose_and_train failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def run_autoresearch(
    hypothesis: str,
    session_id: str | None = None,
    workspace_dir: str | None = None,
    backend: str = "hbv",
    strategy: str = "random",
    budget_hours: float | None = None,
    max_experiments: int | None = 20,
    proxy_epochs: int = 50,
    space_overrides_json: str | None = None,
    prereg_id: str | None = None,
    comparison_metric: str = "nse",
    full_retrain: bool = True,
) -> dict:
    """Launch a CI-aware autoresearch episode.

    Proposes -> trains (fast proxy) -> CI-aware keep/discard, repeating until the budget is
    exhausted; the champion is re-trained at full epochs if full_retrain=True. Every run goes to
    leaderboard.json in the job artifact dir (get_leaderboard(job_id)). A challenger replaces
    the incumbent only if the paired-difference bootstrap CI on skill(challenger) -
    skill(incumbent) has ci_lower > 0, not on a higher point estimate.

    Parameters
    ----------
    hypothesis : str
        One sentence: what you expect to improve and why. Recorded in the job and passed to
        register_research_plan if prereg_id is supplied.
    backend : str
        "hbv" (default), "nh_lstm", "nh_ealstm", ...; describe_model_space() lists what is available.
    strategy : str
        "random" (default) or "grid".
    budget_hours : float | None
        Wall-clock ceiling (None = no limit).
    max_experiments : int | None
        Max training runs (default 20); with budget_hours both caps apply.
    proxy_epochs : int
        Epochs per proxy run (default 50).
    space_overrides_json : str | None
        ModelSpec overrides as JSON, e.g. '{"n_restarts": 5, "epochs": 300}'.
    prereg_id : str | None
        Pre-registration id from register_research_plan (recommended).
    comparison_metric : str
        "nse" (default) or "kge".
    full_retrain : bool
        Re-train the winner at base_spec.epochs after the search (default True).

    Returns {job_id, status: "pending", ...}; wait with wait_for_job(job_id).
    """
    import json as _json

    try:
        session_id = _resolve_session(session_id, None)

        # ── Parse overrides ───────────────────────────────────────────────
        space_overrides: dict = {}
        if space_overrides_json:
            try:
                space_overrides = _json.loads(space_overrides_json)
            except _json.JSONDecodeError as je:
                return {
                    "error": True,
                    "code": "INVALID_JSON",
                    "message": f"space_overrides_json is not valid JSON: {je}",
                }

        # ── Backend availability check ────────────────────────────────────
        import aihydro_modelling as m
        avail = m.availability()
        backend_status = avail.get(backend)
        if backend_status is not None and not backend_status.available:
            return {
                "error": True,
                "code": "BACKEND_UNAVAILABLE",
                "backend": backend,
                "reason": backend_status.reason,
                "hint": (
                    "Pick an available backend (call describe_model_space() to see which) "
                    "or install the required dependency."
                ),
            }

        # ── Session prerequisite check ────────────────────────────────────
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)

        is_neural = backend.startswith("nh_")
        required = ("watershed", "streamflow", "forcing") if is_neural else ("watershed", "forcing")
        missing = [s for s in required if getattr(session, s) is None]
        if missing:
            return {
                "error": True,
                "code": "MISSING_PREREQUISITES",
                "message": (
                    f"Cannot autoresearch — session is missing: {missing}. "
                    "Run: "
                    + ", ".join({
                        "watershed":  "delineate_watershed",
                        "streamflow": "fetch_streamflow_data",
                        "forcing":    "fetch_forcing_data",
                    }[s] for s in missing)
                ),
            }

        from ai_hydro.modelling.gauge import basin_consistency_error, gauge_error
        _gerr = gauge_error(session, session_id)
        if _gerr:
            return _gerr
        _berr = basin_consistency_error(session, session.site_id)
        if _berr:
            return _berr

        # ── Kick off job ──────────────────────────────────────────────────
        job_id = uuid.uuid4().hex[:12]

        ws = workspace_dir or session.workspace_dir
        base = Path(ws) if ws else aihydro_home() / "models"
        artifact_dir = base / "runs" / job_id

        # Extract base periods from session forcing if available
        forcing = getattr(session, "forcing", None) or {}
        config: dict = {
            "job_id":          job_id,
            "session_id":      session_id,
            "hypothesis":      hypothesis,
            "backend":         backend,
            "strategy":        strategy,
            "budget_hours":    budget_hours,
            "max_experiments": max_experiments,
            "proxy_epochs":    proxy_epochs,
            "space_overrides": space_overrides,
            "prereg_id":       prereg_id,
            "comparison_metric": comparison_metric,
            "full_retrain":    full_retrain,
            # Base spec periods (runner fills sensible defaults if absent)
            "train_start":     forcing.get("train_start") or space_overrides.get("train_start"),
            "train_end":       forcing.get("train_end")   or space_overrides.get("train_end"),
            "test_start":      forcing.get("test_start")  or space_overrides.get("test_start"),
            "test_end":        forcing.get("test_end")    or space_overrides.get("test_end"),
        }

        pending_status = {
            "job_id": job_id,
            "status": "pending",
            "hypothesis": hypothesis,
            "backend": backend,
            "n_done": 0,
            "n_total": max_experiments,
        }

        job = jobs.start_job(
            kind="autoresearch",
            runner_module="ai_hydro.mcp.search_runner",
            config=config,
            artifact_dir=artifact_dir,
            log_name="train.log",
            status_seed=pending_status,
        )

        return {
            "job_id":          job["job_id"],
            "status":          "pending",
            "hypothesis":      hypothesis,
            "backend":         backend,
            "strategy":        strategy,
            "max_experiments": max_experiments,
            "budget_hours":    budget_hours,
            "proxy_epochs":    proxy_epochs,
            "comparison_metric": comparison_metric,
            "prereg_id":       prereg_id,
            "artifact_dir":    job["artifact_dir"],
            "started_at":      job["started_at"],
            "_note": (
                f"Autoresearch started (backend={backend}, strategy={strategy}, "
                f"max_experiments={max_experiments}). "
                f"Wait with wait_for_job('{job['job_id']}'). "
                f"Retrieve leaderboard with get_leaderboard('{job['job_id']}')."
            ),
            "wait_with":     f"wait_for_job('{job['job_id']}')",
            "retrieve_with": f"get_leaderboard('{job['job_id']}')",
        }

    except Exception as e:
        log.error("run_autoresearch kickoff failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def get_leaderboard(job_id: str) -> dict:
    """
    Return the autoresearch leaderboard for a completed or running job.

    Reads leaderboard.json written by the search loop after every run.
    Each entry records the proposed spec, achieved metrics, the CI comparison
    result, and whether this config became the incumbent.

    Returns the leaderboard entries + a summary (best metric, n_experiments,
    n_incumbents) + `episode`: n_trials, promotions (CI excludes zero?, Holm-
    adjusted significance), seeds_recorded, prereg. No score or grade.
    Works on in-progress jobs (partial leaderboard) too.

    job_id : str — from the run_autoresearch response.
    """
    try:
        # Resolve artifact dir from job registry
        status = jobs.get_job_status(job_id)
        if status.get("error"):
            return status

        artifact_dir = Path(status.get("artifact_dir", ""))
        if not artifact_dir.exists():
            return {
                "error": True,
                "code": "ARTIFACT_DIR_NOT_FOUND",
                "job_id": job_id,
                "artifact_dir": str(artifact_dir),
            }

        from aihydro_modelling.leaderboard import (
            autoresearch_summary, leaderboard_summary, read_leaderboard)
        entries = read_leaderboard(artifact_dir)
        summary = leaderboard_summary(entries)

        # The episode's facts, not a grade (aihydro-modelling >= 0.6.1): trials, promotions with
        # the paired-difference CI and the Holm-adjusted verdict over ALL completed comparisons,
        # whether every trial recorded a seed, and the pre-registration. The deprecated score /
        # grade keys are dropped here so a client cannot render them.
        out = {
            "job_id":      job_id,
            "job_status":  status.get("status"),
            "summary":     summary,
            "leaderboard": entries,
        }
        full = autoresearch_summary(entries)
        if "promotions" in full and "n_trials" in full:
            out["episode"] = {k: v for k, v in full.items()
                              if k not in ("episode_score", "episode_grade", "score_breakdown",
                                           "deprecation")}
        else:
            # aihydro-modelling 0.6.0 reads the old flat structure and would report a real
            # episode as zero CI-significant promotions and no pre-registration. Never present
            # that as facts.
            out["version_note"] = (
                "episode facts omitted: the installed aihydro-modelling is older than 0.6.1 and "
                "its autoresearch_summary does not read the leaderboard the loop writes. "
                "Upgrade aihydro-modelling to >=0.6.1."
            )
        return out

    except Exception as e:
        log.error("get_leaderboard failed: %s", e)
        return _tool_error_to_dict(e)


def _rating(nse) -> str:
    return ("excellent" if nse is not None and nse >= 0.75 else
            "satisfactory" if nse is not None and nse >= 0.50 else
            "poor" if nse is not None else "unknown")


def _kernel_staleness(result: dict) -> str | None:
    """Why a stored HBV result cannot be trusted, or None.

    HBV results made before aihydro-modelling 0.6.2 came from a kernel that created water
    (audit F1). They carry no ``hbv_kernel`` stamp; a stamp that differs from the installed
    kernel's is also stale. Non-HBV results are not affected.
    """
    if str(result.get("model_type") or "").lower() != "hbv" and \
            str(result.get("framework") or "").lower() != "hbv-light":
        return None
    have = result.get("hbv_kernel")
    try:
        from aihydro_modelling.backends.hbv import HBV_KERNEL
    except ImportError:
        HBV_KERNEL = None
    if not have:
        return ("it carries no hbv_kernel stamp: it was produced by the HBV kernel that created water "
                "(aihydro-modelling < 0.6.2)")
    if HBV_KERNEL and have != HBV_KERNEL:
        return f"it was produced by HBV kernel '{have}', the installed kernel is '{HBV_KERNEL}'"
    return None


def _stale_model(reason: str, *, session_id, source: str, result: dict, job_id: str | None = None) -> dict:
    """A stored model that must be re-run: reported, but never as a clean trained model."""
    out = {
        "session_id": session_id,
        "error": False,
        "model_trained": False,
        "stale": True,
        "code": "MODEL_STALE_KERNEL",
        "source": source,
        "message": f"The stored HBV result is stale: {reason}. Its skill numbers are withheld; re-run "
                   "the training (propose_and_train / train_hydro_model).",
        "framework": result.get("framework"),
        "model_dir": result.get("model_dir"),
        "hbv_kernel": result.get("hbv_kernel"),
    }
    if job_id:
        out["job_id"] = job_id
    return out


def _job_error(job_id: str, code: str, message: str, **extra) -> dict:
    """A job-scoped refusal. ``model_trained`` is always False: no other model's skill is ever shown."""
    return {"error": True, "code": code, "job_id": job_id, "model_trained": False,
            "message": message, **extra}


def _job_model_results(job_id: str, session_id: str | None) -> dict:
    """Results of THIS job and nothing else (audit F2).

    When ``job_id`` is given it is authoritative: an unknown, failed, cancelled, wrong-kind or
    other-session job gets its own structured answer. Control never falls through to the
    session's cached model, whose skill belongs to a different run.
    """
    import json as _json

    status = jobs.get_job_status(job_id)
    # A lookup failure is the envelope ``{"error": True, ...}``; a failed job's own status.json
    # also carries an ``error`` key (a dict describing the failure), which is not a lookup failure.
    if status.get("error") is True:
        return _job_error(
            job_id, "JOB_NOT_FOUND",
            status.get("message") or f"No job found for job_id='{job_id}'.",
            next_tools=["list_jobs"],
        )

    # Kind and owning session come from the job's own records (public registry listing).
    try:
        entry = next((j for j in (jobs.list_jobs().get("jobs") or [])
                      if j.get("job_id") == job_id), {})
    except Exception:
        entry = {}
    adir = entry.get("artifact_dir") or status.get("artifact_dir")
    if not adir:
        # No artifact dir: never fall back to Path("") (the working directory).
        return _job_error(
            job_id, "JOB_NOT_FOUND",
            f"Job {job_id} has no artifact directory on record, so its result cannot be read.",
            next_tools=["list_jobs"],
        )
    artifact_dir = Path(adir)
    cfg: dict = {}
    try:
        cfg = _json.loads((artifact_dir / "job_config.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    kind = str(entry.get("kind") or "").lower()
    if not kind:     # unregistered job: infer from what it carries
        kind = ("training" if ("spec" in cfg or "framework" in cfg or "partial_results" in status)
                else "unknown")
    if kind != "training":
        return _job_error(
            job_id, "NOT_A_TRAINING_JOB",
            f"Job {job_id} is a '{kind}' job, not a training job; it has no single trained model.",
            job_kind=kind,
            next_tools=["get_leaderboard"] if "autoresearch" in kind or "search" in kind
            else ["get_training_status"],
        )

    job_session = str(cfg.get("session_id") or "")
    asked = session_id
    if asked is None:
        # No session named: do not require resolution, but never show a job from another study.
        try:
            from ai_hydro.mcp.helpers import _resolve_session as _rs
            asked = _rs(None, None, allow_auto_create=False, allow_recent_fallback=False, must_exist=True)
        except Exception:
            asked = None
    if job_session and asked and job_session != asked:
        return _job_error(
            job_id, "JOB_SESSION_MISMATCH",
            f"Job {job_id} belongs to session '{job_session}', not '{asked}'. "
            "Its result is not shown in another study.",
            job_session_id=job_session,
            recovery=f"Call get_model_results(job_id='{job_id}', session_id='{job_session}').",
        )
    sid = job_session or asked

    state = status.get("status")
    if state not in ("complete", "failed", "cancelled") and status.get("_pid_alive") is False:
        # status.json says the job is running but its process is gone: it crashed.
        return _job_error(
            job_id, "JOB_PROCESS_GONE",
            f"Job {job_id} reports '{state}' but its process is no longer running; it most likely "
            "crashed. There is no model to report.",
            session_id=sid, job_status=state, job_note=status.get("_note"),
            next_tools=["get_training_status"],
        )
    if state == "failed":
        return _job_error(
            job_id, "JOB_FAILED", f"Job {job_id} failed; there is no model to report.",
            session_id=sid, job_error=status.get("error"), next_tools=["get_training_status"],
        )
    if state == "cancelled":
        return _job_error(
            job_id, "JOB_CANCELLED", f"Job {job_id} was cancelled; there is no model to report.",
            session_id=sid,
        )
    if state != "complete":
        out = {
            "session_id": sid,
            "model_trained": False,
            "job_id": job_id,
            "job_status": state,
            "progress": status.get("progress"),
            "message": f"Job {job_id} is still {state or 'running'}.",
        }
        if status.get("_note"):
            out["job_note"] = status["_note"]
        return out

    partial = status.get("partial_results") or {}
    nse = partial.get("nse")
    if not partial or nse is None:
        return _job_error(
            job_id, "JOB_NO_SKILL",
            f"Job {job_id} finished without a scored model (no test-period NSE); it is not reported as trained.",
            session_id=sid, job_status=state,
        )
    stale = _kernel_staleness(partial)
    if stale:
        return _stale_model(stale, session_id=sid, job_id=job_id, source="job_artifact", result=partial)
    return {
        "session_id": sid,
        "job_id": job_id,
        "model_trained": True,
        "source": "job_artifact",
        **partial,
        "performance_rating": _rating(nse),
    }


@mcp.tool()
def get_model_results(session_id: str | None = None, job_id: str | None = None) -> dict:
    """
    Return training results: NSE, KGE, RMSE, model_dir. With job_id, returns THAT job's result
    only (JOB_NOT_FOUND / JOB_FAILED / JOB_CANCELLED / NOT_A_TRAINING_JOB / JOB_SESSION_MISMATCH
    otherwise; it never falls back to another model). Without job_id, the session's latest model.
    session_id : str | None — auto-resolved from chat context; a job names its own session.
    """
    try:
        if job_id:
            # The job names its own session; resolution is not required (and must not create one).
            return _job_model_results(job_id, session_id)

        session_id = _resolve_session(session_id, None, must_exist=True)

        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)

        if session.model is None:
            return {
                "session_id": session_id,
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
        if nse is None:
            # A cached model with no test-period skill was never scored: not a trained model.
            return {
                "session_id": session_id,
                "error": False,
                "model_trained": False,
                "code": "MODEL_NOT_SCORED",
                "message": "The cached model has no test-period NSE (it scored nothing); "
                           "it is not reported as trained. Re-train on a window the record covers.",
            }
        stale = _kernel_staleness(result)
        if stale:
            return _stale_model(stale, session_id=session_id, source="session_cache", result=result)
        return {
            "session_id": session_id,
            "model_trained": True,
            "source": "session_cache",
            **result,
            "performance_rating": _rating(nse),
        }

    except Exception as e:
        log.error("get_model_results failed: %s", e)
        return _tool_error_to_dict(e)

