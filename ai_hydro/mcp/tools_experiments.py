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
  separate_baseflow               — BFI + recession constants

Adding a new tool: add its function to _EXPERIMENT_RUNNERS and its metric
mapping to _TOOL_METRICS below.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ai_hydro.mcp.app import mcp
from ai_hydro.session import HydroSession
from ai_hydro.mcp.helpers import _tool_error_to_dict
from aihydro_core.primitives.hashing import content_hash, param_hash

log = logging.getLogger("ai_hydro.mcp.experiments")

# ── Tool registry ─────────────────────────────────────────────────────────────

def _get_runners() -> dict:
    """Lazy import to avoid circular deps at module load time."""
    from ai_hydro.mcp.tools_analysis import (
        extract_hydrological_signatures,
        extract_geomorphic_parameters,
        separate_baseflow,
    )
    return {
        "extract_hydrological_signatures": extract_hydrological_signatures,
        "extract_geomorphic_parameters": extract_geomorphic_parameters,
        "separate_baseflow": separate_baseflow,
    }


# Known metric keys per tool (used for default metric selection)
_TOOL_METRICS: dict[str, list[str]] = {
    "extract_hydrological_signatures": [
        "q_mean", "runoff_ratio", "baseflow_index",
        "flow_variability", "high_q_freq", "low_q_freq",
        "fdc_slope", "streamflow_elasticity",
    ],
    "extract_geomorphic_parameters": [
        "area_km2", "slope_mean", "elongation_ratio",
        "drainage_density", "relief_km", "circularity_ratio",
    ],
    "separate_baseflow": [
        "baseflow_index", "recession_constant",
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
               extract_geomorphic_parameters, separate_baseflow.
    features : list of feature IDs (gauge IDs, watershed names, or any
               string identifier the tool accepts as the 'feature' param).
    params   : shared keyword params forwarded to every tool call.
               Example: {"start_date": "1980-01-01", "end_date": "2020-12-31"}.
    metrics  : output keys to track per feature. Defaults to the standard
               metrics for the chosen tool.

    Returns:
        experiment_id   — stable ID derived from content hash of the design
        name, tool, n_features, params_hash, metrics, created_at
    """
    try:
        if not features:
            raise ValueError("features must contain at least one entry.")
        if tool not in _SUPPORTED_TOOLS:
            raise ValueError(
                f"Tool '{tool}' is not supported for experiments. "
                f"Supported: {_SUPPORTED_TOOLS}."
            )
        if not name or not name.strip():
            raise ValueError("name must be a non-empty string.")

        params = params or {}
        metrics = metrics or _TOOL_METRICS.get(tool, [])
        if not metrics:
            raise ValueError(f"No default metrics known for tool '{tool}'. Pass metrics explicitly.")

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

    Each feature is run sequentially (HydroSession is not concurrency-safe
    for writes). Results are committed after every feature so that partial
    progress survives if the run is interrupted.

    Returns:
        experiment_id, status, n_features, n_success, n_error,
        run_ids (feature_id → run_id), errors (feature_id → message)
    """
    try:
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

        # Dispatch: tools already support feature: list for batch fan-out.
        # Run one at a time to respect HydroSession write-once-per-load contract.
        run_ids: dict[str, str] = {}
        cells: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}

        for feature in features:
            try:
                result = runner(session_id=session_id, feature=feature, **params)
                if result.get("error"):
                    errors[feature] = str(result.get("message", result.get("error", "error")))
                    continue
                run_id = result.get("_run_id", "")
                if run_id:
                    run_ids[feature] = run_id

                # Extract metric values and CI bounds from result
                data = result.get("data", {})
                unc = data.get("_uncertainty", {})
                feature_cells: dict[str, Any] = {}
                for m in metrics:
                    val = data.get(m)
                    if val is None:
                        continue
                    cell: dict[str, Any] = {"value": val, "run_id": run_id}
                    if isinstance(unc, dict) and m in unc:
                        u = unc[m]
                        if isinstance(u, dict):
                            cell["ci_low"] = u.get("ci_low")
                            cell["ci_high"] = u.get("ci_high")
                    feature_cells[m] = cell

                if feature_cells:
                    cells[feature] = feature_cells

            except Exception as exc:
                errors[feature] = str(exc)

        completed_at = datetime.now(timezone.utc).isoformat()
        status = "complete" if not errors else ("error" if len(errors) == len(features) else "partial")

        results_payload = {
            "status": status,
            "run_ids": run_ids,
            "cells": cells,
            "errors": errors,
            "n_success": len(features) - len(errors),
            "n_error": len(errors),
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
            "n_error": results_payload["n_error"],
            "run_ids": run_ids,
            **({"errors": errors} if errors else {}),
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

    Rows = features, columns = metrics (+ CI bounds where available).
    Every numeric cell carries its run_id so any value can be traced back
    to the tool call that produced it.

    The returned 'rows' list is suitable for display in the ExperimentTable
    panel and for constructing aggregate claims that cite the experiment.
    When writing an interpretation that references aggregate statistics (e.g.
    mean q_mean across 20 basins), cite experiment_id as the source_id in
    an evidence_span: {source_type: "run", source_id: experiment_id}.

    Returns:
        experiment_id, name, tool, params, columns, rows,
        n_rows, n_columns, n_with_ci,
        aggregate_stats (mean/min/max per metric across all features)
    """
    try:
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

        # Build rows
        rows: list[dict[str, Any]] = []
        for feature in defn["features"]:
            if feature not in cells and feature not in results.get("errors", {}):
                continue  # never ran
            row: dict[str, Any] = {"feature_id": feature}
            if feature in results.get("errors", {}):
                row["error"] = results["errors"][feature]
                row["run_id"] = None
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
            rows.append(row)

        # Aggregate stats per metric
        import statistics as _stats
        agg: dict[str, Any] = {}
        for m in metrics:
            vals = [r[m] for r in rows if r.get(m) is not None and not r.get("error")]
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

        return {
            "experiment_id": experiment_id,
            "name": defn["name"],
            "tool": defn["tool"],
            "params": defn["params"],
            "columns": columns,
            "rows": rows,
            "n_rows": len(rows),
            "n_columns": len(columns),
            "n_with_ci": n_with_ci,
            "aggregate_stats": agg,
            "note": (
                "To cite this experiment in an interpretation, use: "
                f"evidence_spans: [{{source_type: 'run', source_id: '{experiment_id}', "
                "metric_ref: '<metric>'}}]"
            ),
        }
    except Exception as exc:
        return _tool_error_to_dict(exc)
