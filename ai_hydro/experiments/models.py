"""
Experiment substrate models for fleet-scale multi-basin runs.

An experiment is a design-matrix run: a fixed tool applied to N features
(basins, gauges) with shared params, tracking a subset of output metrics.
Results are stored per-feature, per-metric with their run_id so every cell
in the experiment table is traceable to a specific Tier-1 tool call.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field


class ExperimentDefinition(BaseModel):
    """Immutable specification of an experiment."""
    experiment_id: str
    name: str
    tool: str
    features: list[str]
    params: dict[str, Any] = {}
    metrics: list[str]
    params_hash: str
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class MetricCell(BaseModel):
    """One metric value in the experiment table, bound to its run_id."""
    value: float | int | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    run_id: str | None = None


class FeatureRow(BaseModel):
    """All tracked metric values for one feature in the experiment."""
    feature_id: str
    cells: dict[str, MetricCell] = {}
    error: str | None = None


class ExperimentResults(BaseModel):
    """Results stored after run_experiment completes."""
    experiment_id: str
    status: Literal["pending", "running", "complete", "error"] = "pending"
    run_ids: dict[str, str] = {}
    rows: list[FeatureRow] = []
    n_success: int = 0
    n_error: int = 0
    completed_at: str | None = None


class ExperimentTable(BaseModel):
    """Flat tabular view returned by get_experiment_table."""
    experiment_id: str
    name: str
    tool: str
    columns: list[str]
    rows: list[dict[str, Any]]
    n_rows: int
    n_columns: int
    n_with_ci: int
    params: dict[str, Any] = {}
