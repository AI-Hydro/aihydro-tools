"""
Fleet-scale experiment tools (Phase 2.1).

Three tools form the experiment lifecycle:
  1. define_experiment — registers the design matrix (tool × features × params)
  2. run_experiment    — dispatches the tool for all features; stores results
  3. get_experiment_table — returns the flat tabular view with per-cell run_ids

Design
------
Results live in the session under a single `_experiments` slot keyed by
experiment_id. This keeps the session file lean (actual numeric outputs are
already in run_log / per-feature signatures slots).

An aggregate claim can cite an experiment via:
  evidence_spans: [{source_type: "run", source_id: experiment_id, metric_ref: "mean_q_mean"}]

Supported tools
---------------
  extract_hydrological_signatures — most common; supplies 17 CAMELS metrics
  extract_geomorphic_parameters   — geomorphic indices (28 metrics)

separate_baseflow is NOT supported: it has no ``feature`` argument, operates on
the session's one streamflow slot (so it cannot describe a second basin) and
returns ``bfi``. The per-feature baseflow index is ``baseflow_index`` from
extract_hydrological_signatures.

Features are basins, not labels
-------------------------------
Every feature must be registered in the session (``register_feature``) with the
gauge that observes it. An id that is not registered is a per-feature error
(``FEATURE_NOT_RESOLVED``); it is never answered with the session's own basin.
The gauge and the area come from the feature, not from the session.

Every row of the table carries its own run id (``run_id_origin`` says whether
the run was made for this experiment or served from the per-feature cache) or an
explicit status and reason ("error", "no_metrics", "no_run_id", "not_run").

Adding a new tool: add its function to ``_get_runners`` and its metric keys to
``_TOOL_OUTPUT_KEYS`` / ``_TOOL_METRICS`` below. The keys must be copied from
what the tool really emits; tests/test_experiments_real_tools.py checks them
against the real tool.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ai_hydro.mcp.app import mcp
from ai_hydro.session import HydroSession
from ai_hydro.mcp.helpers import _tool_error_to_dict, resolve_existing_study
from aihydro_core.primitives.hashing import content_hash, param_hash

log = logging.getLogger("ai_hydro.mcp.experiments")

# ── Tool registry ─────────────────────────────────────────────────────────────

def _get_runners() -> dict:
    """Lazy import to avoid circular deps at module load time."""
    from ai_hydro.mcp.tools_analysis import (
        extract_hydrological_signatures,
        extract_geomorphic_parameters,
    )
    return {
        "extract_hydrological_signatures": extract_hydrological_signatures,
        "extract_geomorphic_parameters": extract_geomorphic_parameters,
    }


# Every numeric key each tool emits under ``data`` (verified against the real
# tool output, see the module docstring). A metric outside this set can never be
# filled, so define_experiment refuses it instead of producing an empty column.
_TOOL_OUTPUT_KEYS: dict[str, tuple[str, ...]] = {
    # aihydro_watershed.signatures.signatures.extract_hydrological_signatures
    "extract_hydrological_signatures": (
        "q_mean", "q_std", "q5", "q95", "q_median", "baseflow_index",
        "runoff_ratio", "stream_elas", "high_q_freq", "high_q_dur",
        "low_q_freq", "low_q_dur", "zero_q_freq", "flow_variability",
        "hfd_mean", "half_flow_date_std", "slope_fdc", "slope_fdc_sawicz2011",
    ),
    # aihydro_watershed.characterize.geomorphic.extract_geomorphic_parameters
    "extract_geomorphic_parameters": (
        "DA_km2", "Lp_km", "Lb_km", "Lca_km", "Rff", "Rc", "Re", "Sb", "Ru",
        "H_m", "Rh", "Rp", "Ls_pct", "Cs_m_per_m", "D_km_per_km2", "Rn",
        "C_km2_per_km", "Rf", "Cf_per_km2", "MCh_km", "Slope_10_85_m_per_m",
        "HKR", "G_Gray", "M_Murphey", "pct_water_wetland", "pct_urban",
        "CN_weighted", "pct_sinks",
    ),
}

# Units of each metric. Geomorphic: the tool's own ``units`` dict. Signatures: the tool declares
# none, so these are read from aihydro_watershed.signatures.signatures (flows are q_mm_day, i.e.
# Q divided by basin area; *_freq = event days / n * 365.25; zero_q_freq is a fraction;
# hfd_mean is the day of the water year starting 1 October).
_TOOL_UNITS: dict[str, dict[str, str]] = {
    "extract_hydrological_signatures": {
        "q_mean": "mm/day", "q_std": "mm/day", "q5": "mm/day", "q95": "mm/day",
        "q_median": "mm/day", "baseflow_index": "-", "runoff_ratio": "-", "stream_elas": "-",
        "high_q_freq": "days/year", "high_q_dur": "days", "low_q_freq": "days/year",
        "low_q_dur": "days", "zero_q_freq": "fraction of days", "flow_variability": "-",
        "hfd_mean": "day of water year", "half_flow_date_std": "days", "slope_fdc": "-",
        "slope_fdc_sawicz2011": "-",
    },
    "extract_geomorphic_parameters": {
        "DA_km2": "km2", "Lp_km": "km", "Lb_km": "km", "Lca_km": "km", "Rff": "-", "Rc": "-",
        "Re": "-", "Sb": "-", "Ru": "-", "H_m": "m", "Rh": "-", "Rp": "-", "Ls_pct": "%",
        "Cs_m_per_m": "m/m", "D_km_per_km2": "km/km2", "Rn": "-", "C_km2_per_km": "km2/km",
        "Rf": "-", "Cf_per_km2": "1/km2", "MCh_km": "km", "Slope_10_85_m_per_m": "m/m",
        "HKR": "-", "G_Gray": "-", "M_Murphey": "-", "pct_water_wetland": "%",
        "pct_urban": "%", "CN_weighted": "-", "pct_sinks": "%",
    },
}

# Call arguments that name the basin. They belong to the feature, never to the shared params:
# ``geometry_geojson`` in params overrides every feature's polygon (every row would describe
# the same basin under its own label), ``feature`` collides with the per-row feature, and
# ``session_id`` is the experiment's own.
_RESERVED_PARAMS = ("geometry_geojson", "feature", "session_id")


def _reserved_param_error(params: dict) -> str | None:
    bad = [k for k in _RESERVED_PARAMS if k in (params or {})]
    if not bad:
        return None
    return (
        f"params may not contain {bad}: these select the basin and would make every feature "
        "describe the same one. Register each basin as a feature "
        "(register_feature(geojson, name, gauge_id=...)) and list it in `features`."
    )


# Default metrics per tool: a subset of the keys above.
_TOOL_METRICS: dict[str, list[str]] = {
    "extract_hydrological_signatures": [
        "q_mean", "runoff_ratio", "baseflow_index",
        "flow_variability", "high_q_freq", "low_q_freq",
        "slope_fdc", "stream_elas",
    ],
    "extract_geomorphic_parameters": [
        "DA_km2", "Slope_10_85_m_per_m", "Re",
        "D_km_per_km2", "H_m", "Rc",
    ],
}

_SUPPORTED_TOOLS = list(_TOOL_METRICS.keys())

# ── Session helpers ───────────────────────────────────────────────────────────

_EXP_SLOT = "_experiments"


def _load_experiments(session: HydroSession) -> dict:
    raw = session.get(_EXP_SLOT)
    return raw.get("data", {}) if isinstance(raw, dict) and "data" in raw else {}


def _save_experiments(session: HydroSession, exps: dict) -> None:
    session.set(_EXP_SLOT, {"data": exps, "meta": {"tool": "experiments"}})
    session.save()


# ── Per-feature status vocabulary ─────────────────────────────────────────────

#: A feature row is exactly one of these. Nothing is dropped from the table.
_ST_OK = "ok"                  # metrics extracted, run id recorded
_ST_ERROR = "error"            # the feature could not be resolved or the tool failed
_ST_NO_METRICS = "no_metrics"  # the tool succeeded but none of the tracked metrics came back
_ST_NO_RUN_ID = "no_run_id"    # numbers returned but no run record identifies them
_ST_NOT_RUN = "not_run"        # defined but never reached (interrupted run)


def _metric_gap_reason(metric: str, data: dict) -> str:
    """Why a tracked metric has no value in a tool result."""
    if metric not in data:
        return "key not emitted by the tool"
    prec = data.get("_precipitation")
    if metric in ("runoff_ratio", "stream_elas") and isinstance(prec, dict) and prec.get("reason"):
        return f"tool returned null ({prec['reason']})"
    return "tool returned null (insufficient or invalid data for this basin)"


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def define_experiment(
    session_id: str,
    name: str,
    tool: str,
    features: list[str],
    params: dict | None = None,
    metrics: list[str] | None = None,
) -> dict:
    """
    Define a fleet-scale experiment: a fixed tool applied to N features.

    Registers the design matrix in the session and returns an experiment_id
    that can be passed to run_experiment and get_experiment_table.

    tool     : one of extract_hydrological_signatures,
               extract_geomorphic_parameters.
    features : ids or names of features registered in the session
               (register_feature(geojson, name=..., gauge_id=...)). Each is its
               own basin with its own gauge and area; an id that is not
               registered fails at run time and is never answered with the
               session's basin.
    params   : shared keyword params forwarded to every tool call.
               Example: {"start_date": "1980-01-01", "end_date": "2020-12-31"}.
    metrics  : output keys to track per feature. Defaults to the standard
               metrics for the chosen tool. Keys the tool never emits are
               rejected.

    Returns:
        experiment_id   — stable ID derived from content hash of the design
        name, tool, n_features, params_hash, metrics, created_at,
        unregistered_features (ids that would fail if run now)
    """
    try:
        if not features:
            raise ValueError("features must contain at least one entry.")
        if tool not in _SUPPORTED_TOOLS:
            hint = (" separate_baseflow works on the session's single streamflow slot and "
                    "cannot describe several basins; use extract_hydrological_signatures "
                    "(baseflow_index) instead." if tool == "separate_baseflow" else "")
            raise ValueError(
                f"Tool '{tool}' is not supported for experiments. "
                f"Supported: {_SUPPORTED_TOOLS}.{hint}"
            )
        if not name or not name.strip():
            raise ValueError("name must be a non-empty string.")

        params = params or {}
        bad_params = _reserved_param_error(params)
        if bad_params:
            raise ValueError(bad_params)
        metrics = metrics or _TOOL_METRICS.get(tool, [])
        if not metrics:
            raise ValueError(f"No default metrics known for tool '{tool}'. Pass metrics explicitly.")
        unknown = [m for m in metrics if m not in _TOOL_OUTPUT_KEYS[tool]]
        if unknown:
            raise ValueError(
                f"Metric(s) {unknown} are not emitted by '{tool}', so those columns could never "
                f"be filled. Valid metrics: {list(_TOOL_OUTPUT_KEYS[tool])}."
            )

        session_id = resolve_existing_study(session_id)
        session = HydroSession.load(session_id)

        phash = param_hash(params)
        exp_payload = {
            "name": name.strip(),
            "tool": tool,
            "features": features,
            "params": params,
            "metrics": metrics,
            "params_hash": phash,
        }
        chash = content_hash(exp_payload)[:8]
        experiment_id = f"exp.{chash}"

        exps = _load_experiments(session)
        if experiment_id in exps:
            existing = exps[experiment_id].get("defn", {})
            return {
                "experiment_id": experiment_id,
                "status": "already_exists",
                "note": (
                    f"Experiment '{experiment_id}' already defined in this session. "
                    "Call run_experiment to execute it, or get_experiment_table if "
                    "results are already available."
                ),
                **{k: existing.get(k) for k in ("name", "tool", "n_features", "metrics")},
            }

        from ai_hydro.mcp.tools_analysis import _resolve_registered_feature
        unregistered = [f for f in features if _resolve_registered_feature(session, f)[0] is None]

        created_at = datetime.now(timezone.utc).isoformat()
        exps[experiment_id] = {
            "defn": {
                "experiment_id": experiment_id,
                "name": name.strip(),
                "tool": tool,
                "features": features,
                "params": params,
                "metrics": metrics,
                "params_hash": phash,
                "created_at": created_at,
            },
            "results": None,
        }
        _save_experiments(session, exps)

        return {
            "experiment_id": experiment_id,
            "name": name.strip(),
            "tool": tool,
            "n_features": len(features),
            "params_hash": phash,
            "metrics": metrics,
            "created_at": created_at,
            **({"unregistered_features": unregistered,
                "warning": (
                    f"{len(unregistered)} feature(s) are not registered in this session and will "
                    "fail at run time (FEATURE_NOT_RESOLVED). Register each with "
                    "register_feature(geojson, name=<id>, gauge_id=<gauge>) first."
                )} if unregistered else {}),
            "note": (
                f"Experiment '{experiment_id}' defined. "
                "Call run_experiment(session_id, experiment_id) to execute it."
            ),
        }
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def run_experiment(
    session_id: str,
    experiment_id: str,
) -> dict:
    """
    Execute a pre-defined experiment: calls the registered tool for every
    feature in the design matrix, then stores per-feature metric results
    with their run_ids in the session.

    Each feature is resolved in the session's feature registry first; one that
    is not registered is an error for that feature (FEATURE_NOT_RESOLVED), never
    answered with the session's own basin. The tool then runs on that feature's
    own geometry, gauge and area.

    Each feature is run sequentially (HydroSession is not concurrency-safe
    for writes). Results are committed after every feature so that partial
    progress survives if the run is interrupted.

    Returns:
        experiment_id, status, n_features, n_success, n_error, n_no_metrics,
        run_ids (feature_id → run_id), errors (feature_id → message),
        no_metrics (feature_id → reason)
    """
    try:
        session_id = resolve_existing_study(session_id)
        session = HydroSession.load(session_id)
        exps = _load_experiments(session)

        if experiment_id not in exps:
            raise ValueError(
                f"Experiment '{experiment_id}' not found in session '{session_id}'. "
                "Call define_experiment first."
            )

        defn = exps[experiment_id]["defn"]
        tool_name = defn["tool"]
        features = defn["features"]
        params = defn["params"]
        metrics = defn["metrics"]

        # Definitions stored before define_experiment refused these still exist on disk.
        bad_params = _reserved_param_error(params)
        if bad_params:
            raise ValueError(f"Experiment '{experiment_id}' cannot be run: {bad_params}")

        runners = _get_runners()
        runner = runners.get(tool_name)
        if runner is None:
            raise ValueError(
                f"Tool '{tool_name}' has no runner registered. "
                f"Supported: {list(runners.keys())}."
            )

        # Mark as running
        exps[experiment_id]["results"] = {"status": "running"}
        _save_experiments(session, exps)

        from ai_hydro.mcp.tools_analysis import _feature_scope, _resolve_registered_feature

        # Dispatch one feature at a time to respect HydroSession write-once-per-load contract.
        run_ids: dict[str, str] = {}
        cells: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        error_codes: dict[str, str] = {}
        no_metrics: dict[str, str] = {}
        per_feature: dict[str, dict[str, Any]] = {}

        for feature in features:
            info: dict[str, Any] = {"status": _ST_ERROR, "run_id": None, "run_id_origin": None}
            per_feature[feature] = info
            try:
                # Fail closed before any compute: a feature that is not a registered
                # basin must not be answered with the session's own.
                feat, ferr = _resolve_registered_feature(HydroSession.load(session_id), feature)
                if ferr:
                    errors[feature] = f"{ferr['code']}: {ferr['message']}"
                    error_codes[feature] = ferr["code"]
                    info["reason"] = errors[feature]
                    continue
                info["feature_registry_id"] = feat.feature_id
                _gauge, _area = _feature_scope(feat)
                info["gauge_id"], info["area_km2"] = _gauge, _area

                result = runner(session_id=session_id, feature=feature, **params)
                if result.get("error"):
                    code = result.get("code")
                    msg = str(result.get("message", result.get("error", "error")))
                    errors[feature] = f"{code}: {msg}" if code else msg
                    if code:
                        error_codes[feature] = str(code)
                    info["reason"] = errors[feature]
                    continue
                run_id = result.get("_run_id") or ""
                origin = "cache" if result.get("_cache_hit") else "fresh"

                # Extract metric values and CI bounds from result
                data = result.get("data", {})
                unc = data.get("_uncertainty", {})
                feature_cells: dict[str, Any] = {}
                missing: dict[str, str] = {}
                for m in metrics:
                    val = data.get(m)
                    if val is None:
                        missing[m] = _metric_gap_reason(m, data)
                        continue
                    cell: dict[str, Any] = {"value": val, "run_id": run_id or None}
                    if isinstance(unc, dict) and m in unc:
                        u = unc[m]
                        if isinstance(u, dict):
                            cell["ci_low"] = u.get("ci_low")
                            cell["ci_high"] = u.get("ci_high")
                    feature_cells[m] = cell
                if missing:
                    info["missing_metrics"] = missing

                if not feature_cells:
                    emitted = sorted(k for k in data if not str(k).startswith("_"))
                    no_metrics[feature] = (
                        f"the tool ran but returned none of the tracked metrics {metrics}; "
                        f"{'; '.join(f'{m}: {r}' for m, r in missing.items())}. "
                        f"Keys it returned: {emitted}"
                    )
                    info["status"], info["reason"] = _ST_NO_METRICS, no_metrics[feature]
                    if run_id:
                        info["run_id"], info["run_id_origin"] = run_id, origin
                    continue

                cells[feature] = feature_cells
                if run_id:
                    run_ids[feature] = run_id
                    info.update(status=_ST_OK, run_id=run_id, run_id_origin=origin)
                else:
                    info.update(
                        status=_ST_NO_RUN_ID,
                        reason=(
                            "the tool returned numbers but no run id; they are not tied to a run "
                            "record and are left out of the aggregates"
                            + (" (served from a cache entry written before run ids were stored on it)"
                               if origin == "cache" else "")
                        ),
                    )

            except Exception as exc:
                errors[feature] = str(exc)
                info["status"], info["reason"] = _ST_ERROR, str(exc)

        completed_at = datetime.now(timezone.utc).isoformat()
        statuses = [per_feature[f]["status"] for f in features]
        n_ok = statuses.count(_ST_OK)
        if features and n_ok == len(features):
            status = "complete"
        elif n_ok == 0 and _ST_NO_RUN_ID not in statuses:
            status = "error"
        else:
            status = "partial"

        results_payload = {
            "status": status,
            "run_ids": run_ids,
            "cells": cells,
            "errors": errors,
            "error_codes": error_codes,
            "no_metrics": no_metrics,
            "features": per_feature,
            "n_success": n_ok,
            "n_no_run_id": statuses.count(_ST_NO_RUN_ID),
            "n_error": len(errors),
            "n_no_metrics": len(no_metrics),
            "completed_at": completed_at,
        }

        # Reload session (tool calls may have mutated it)
        session = HydroSession.load(session_id)
        exps = _load_experiments(session)
        exps[experiment_id]["results"] = results_payload
        _save_experiments(session, exps)

        return {
            "experiment_id": experiment_id,
            "status": status,
            "n_features": len(features),
            "n_success": results_payload["n_success"],
            "n_no_run_id": results_payload["n_no_run_id"],
            "n_error": results_payload["n_error"],
            "n_no_metrics": results_payload["n_no_metrics"],
            "run_ids": run_ids,
            **({"errors": errors} if errors else {}),
            **({"no_metrics": no_metrics} if no_metrics else {}),
        }
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def get_experiment_table(
    session_id: str,
    experiment_id: str,
) -> dict:
    """
    Return the experiment results as a flat tabular view.

    Rows = features (every defined feature appears, whatever happened to it),
    columns = metrics (+ CI bounds where available). Each row carries its own
    run_id and run_id_origin ("fresh" = run for this experiment, "cache" = the
    per-feature cache entry, whose run id is the run that produced it), or a
    status (error, no_metrics, no_run_id, not_run) with a reason. Rows without
    a run id are excluded from aggregate_stats.

    The returned 'rows' list is suitable for display in the ExperimentTable
    panel and for constructing aggregate claims that cite the experiment.
    When writing an interpretation that references aggregate statistics (e.g.
    mean q_mean across 20 basins), cite experiment_id as the source_id in
    an evidence_span: {source_type: "run", source_id: experiment_id}.

    Returns:
        experiment_id, name, tool, params, columns, rows,
        units (metric -> unit as declared by the tool), n_rows, n_columns, n_with_ci, status_counts,
        aggregate_stats (mean/min/max per metric across rows with run ids)
    """
    try:
        session_id = resolve_existing_study(session_id)
        session = HydroSession.load(session_id)
        exps = _load_experiments(session)

        if experiment_id not in exps:
            raise ValueError(
                f"Experiment '{experiment_id}' not found in session '{session_id}'."
            )

        exp = exps[experiment_id]
        defn = exp["defn"]
        results = exp.get("results")

        if not results or results.get("status") in (None, "pending", "running"):
            return {
                "experiment_id": experiment_id,
                "status": results.get("status", "pending") if results else "pending",
                "note": (
                    "Experiment has not completed yet. "
                    "Call run_experiment first, then get_experiment_table."
                ),
            }

        metrics = defn["metrics"]
        cells: dict[str, dict[str, Any]] = results.get("cells", {})
        run_ids: dict[str, str] = results.get("run_ids", {})
        errors: dict[str, str] = results.get("errors", {})
        no_metrics: dict[str, str] = results.get("no_metrics", {})
        per_feature: dict[str, dict] = results.get("features", {})

        # Build columns: feature_id + metric + ci_low/ci_high (if any have CIs)
        has_ci = any(
            "ci_low" in cell_data
            for feature_cells in cells.values()
            for cell_data in feature_cells.values()
        )

        columns = ["feature_id"]
        for m in metrics:
            columns.append(m)
            if has_ci:
                columns += [f"{m}_ci_low", f"{m}_ci_high"]
        columns.append("run_id")

        # Build rows: one per defined feature, none dropped.
        rows: list[dict[str, Any]] = []
        for feature in defn["features"]:
            info = per_feature.get(feature) or {}
            row: dict[str, Any] = {"feature_id": feature}
            if feature in errors:
                status, reason = _ST_ERROR, errors[feature]
            elif feature in no_metrics:
                status, reason = _ST_NO_METRICS, no_metrics[feature]
            elif feature in cells:
                status = info.get("status") or (_ST_OK if feature in run_ids else _ST_NO_RUN_ID)
                reason = info.get("reason")
            else:
                status = _ST_NOT_RUN
                reason = info.get("reason") or "no result was recorded for this feature (the run did not reach it)"
            row["status"] = status
            if reason:
                row["reason"] = reason
            if info.get("gauge_id"):
                row["gauge_id"] = info["gauge_id"]
            if status in (_ST_ERROR, _ST_NO_METRICS, _ST_NOT_RUN):
                row["error"] = reason          # the panel renders `error`; the cause is shown, not just a chip
                for m in metrics:
                    row[m] = None
                row["run_id"] = info.get("run_id")
                row["run_id_origin"] = info.get("run_id_origin")
                rows.append(row)
                continue

            feature_cells = cells.get(feature, {})
            for m in metrics:
                cell_data = feature_cells.get(m, {})
                row[m] = cell_data.get("value")
                if has_ci:
                    row[f"{m}_ci_low"] = cell_data.get("ci_low")
                    row[f"{m}_ci_high"] = cell_data.get("ci_high")
            row["run_id"] = run_ids.get(feature)
            row["run_id_origin"] = info.get("run_id_origin")
            if info.get("missing_metrics"):
                row["missing_metrics"] = info["missing_metrics"]
            rows.append(row)

        # Aggregate stats per metric: only rows that have a run id behind them.
        import statistics as _stats
        agg: dict[str, Any] = {}
        agg_rows = [r for r in rows if r["status"] == _ST_OK]
        for m in metrics:
            vals = [r[m] for r in agg_rows if r.get(m) is not None]
            if vals:
                agg[m] = {
                    "mean": round(_stats.mean(vals), 4),
                    "min": round(min(vals), 4),
                    "max": round(max(vals), 4),
                    "n": len(vals),
                }

        n_with_ci = sum(
            1 for feature_cells in cells.values()
            for cell_data in feature_cells.values()
            if "ci_low" in cell_data
        )
        status_counts: dict[str, int] = {}
        for r in rows:
            status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

        return {
            "experiment_id": experiment_id,
            "name": defn["name"],
            "tool": defn["tool"],
            "params": defn["params"],
            "columns": columns,
            "units": {m: _TOOL_UNITS.get(defn["tool"], {}).get(m) for m in metrics},
            "rows": rows,
            "n_rows": len(rows),
            "n_columns": len(columns),
            "n_with_ci": n_with_ci,
            "status_counts": status_counts,
            "aggregate_stats": agg,
            "note": (
                "To cite this experiment in an interpretation, use: "
                f"evidence_spans: [{{source_type: 'run', source_id: '{experiment_id}', "
                "metric_ref: '<metric>'}}]"
            ),
        }
    except Exception as exc:
        return _tool_error_to_dict(exc)
