"""
Knowledge registry Pydantic schemas — the scientific contract layer.

Every knowledge object (variable, metric, dataset, equation) must answer:
  1. What does it mean?
  2. What units does it use?
  3. Which tool uses or produces it?
  4. What validation rules apply?
  5. What assumptions does it depend on?
  6. What evidence supports it?
  7. Under what scope is it valid?
  8. How can it be reproduced?
"""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, field_validator


class VariableSpec(BaseModel):
    """Input specification for a metric computation."""
    units: str
    temporal_alignment_required: bool = False
    description: str = ""


class OutputSpec(BaseModel):
    """Output specification for a metric computation."""
    type: str
    range: list[float | str] | None = None
    perfect: float | None = None
    description: str = ""


class VariableDefinition(BaseModel):
    """
    Canonical definition of a hydrological variable.

    This is the foundational vocabulary layer. It prevents unit confusion —
    the #1 failure mode in hydrology AI workflows.

    verified=True entries are protected: workspace overrides require
    'scientific_justification' in addition to 'overrides'+'override_reason'.
    """
    id: str                           # "variable.streamflow"
    symbols: list[str]                # ["Q"]
    names: list[str]                  # ["streamflow", "discharge"]
    standard_units: str               # "m3/s"
    common_units: list[str]           # ["ft3/s", "mm/day"]
    unit_conversions: list[str] = []  # ["ft3/s_to_m3/s"]
    temporal_support: list[str]       # ["instantaneous", "daily", "monthly"]
    spatial_support: list[str]        # ["gauge", "reach", "basin"]
    description: str = ""
    related_datasets: list[str] = []
    verified: bool = False            # True = peer-reviewed; escalated override guard

    @field_validator("id")
    @classmethod
    def id_must_have_prefix(cls, v: str) -> str:
        if not v.startswith("variable."):
            raise ValueError(f"Variable id must start with 'variable.', got: {v!r}")
        return v


class MetricDefinition(BaseModel):
    """
    Definition of a hydrological model evaluation metric.

    Distinct from equations: metrics evaluate model performance against
    observations. Equations describe physical or mathematical relationships.
    """
    id: str                           # "metric.kge"
    name: str
    domain: str
    category: str
    formula: str | None = None
    inputs: dict[str, VariableSpec]
    outputs: dict[str, OutputSpec]
    validation: list[str]             # human-readable rules
    citations: list[str]
    related_tools: list[str]
    description: str = ""
    verified: bool = False            # True = peer-reviewed; escalated override guard

    @field_validator("id")
    @classmethod
    def id_must_have_prefix(cls, v: str) -> str:
        if not v.startswith("metric."):
            raise ValueError(f"Metric id must start with 'metric.', got: {v!r}")
        return v


class DatasetDefinition(BaseModel):
    """
    Registry entry for a hydrological dataset.

    Captures access pattern, known limitations, and the canonical
    AI-Hydro tool that fetches this data.
    """
    id: str                           # "dataset.usgs_nwis"
    name: str
    domain: str
    category: str
    spatial_coverage: str
    temporal_range: dict[str, str]    # {"start": "...", "end": "present"}
    native_resolution: str
    units: str
    access_tool: str
    limitations: list[str]
    citations: list[str]
    description: str = ""
    verified: bool = False            # True = peer-reviewed; escalated override guard

    @field_validator("id")
    @classmethod
    def id_must_have_prefix(cls, v: str) -> str:
        if not v.startswith("dataset."):
            raise ValueError(f"Dataset id must start with 'dataset.', got: {v!r}")
        return v


class EquationDefinition(BaseModel):
    """
    Definition of a physical or mathematical equation used in hydrology.

    Equations describe processes (ET, routing, infiltration).
    Metrics evaluate model performance. Both live in separate files.
    """
    id: str                           # "equation.penman_monteith"
    name: str
    domain: str
    category: str                     # routing | infiltration | ET | loss | index
    formula: str
    variables: list[str]              # variable ids from registry
    assumptions: list[str]
    citations: list[str]
    description: str = ""

    @field_validator("id")
    @classmethod
    def id_must_have_prefix(cls, v: str) -> str:
        if not v.startswith("equation."):
            raise ValueError(f"Equation id must start with 'equation.', got: {v!r}")
        return v


class WorkspaceOverride(BaseModel):
    """Metadata required when a workspace knowledge object overrides a built-in."""
    overrides: str          # id of the built-in being overridden
    override_reason: str    # scientific justification


class KnowledgeRegistry(BaseModel):
    """Aggregated registry loaded from all tiers."""
    variables: dict[str, VariableDefinition] = {}
    metrics: dict[str, MetricDefinition] = {}
    datasets: dict[str, DatasetDefinition] = {}
    equations: dict[str, EquationDefinition] = {}
