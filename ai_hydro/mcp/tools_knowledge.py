"""
Knowledge Registry MCP tools.

Expose the scientific contract layer (variables, metrics, datasets, equations)
to the agent. These tools allow the agent to understand what scientific
objects exist, how they are defined, and what constraints apply.
"""
from __future__ import annotations

import logging
from ai_hydro.mcp.app import mcp
from ai_hydro.knowledge.loader import get_registry
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp")


@mcp.tool()
def get_variable_definition(variable_id: str, workspace_dir: str | None = None) -> dict:
    """
    Return the canonical definition of a hydrological variable.
    
    Use this to understand units, symbols, and standard names for variables 
    like streamflow, precipitation, or PET.
    """
    try:
        registry = get_registry(workspace_dir)
        var = registry.variables.get(variable_id)
        if not var:
            return _tool_error_to_dict(ValueError(f"Variable '{variable_id}' not found in registry."))
        return var.model_dump()
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def list_known_variables(workspace_dir: str | None = None) -> list[dict]:
    """Return a summary of all known hydrological variables."""
    try:
        registry = get_registry(workspace_dir)
        return [
            {
                "id": v.id,
                "name": v.names[0] if v.names else v.id,
                "standard_units": v.standard_units,
                "description": v.description
            }
            for v in registry.variables.values()
        ]
    except Exception as exc:
        log.error("Failed to list variables: %s", exc)
        return []


@mcp.tool()
def get_metric_definition(metric_id: str, workspace_dir: str | None = None) -> dict:
    """
    Return the structured definition of a model evaluation metric (e.g. KGE, NSE).
    
    Includes formula, required inputs, expected output ranges, and validation rules.
    """
    try:
        registry = get_registry(workspace_dir)
        metric = registry.metrics.get(metric_id)
        if not metric:
            return _tool_error_to_dict(ValueError(f"Metric '{metric_id}' not found in registry."))
        return metric.model_dump()
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def list_known_metrics(domain: str | None = None, workspace_dir: str | None = None) -> list[dict]:
    """Return all known metrics, optionally filtered by domain."""
    try:
        registry = get_registry(workspace_dir)
        metrics = list(registry.metrics.values())
        if domain:
            metrics = [m for m in metrics if m.domain == domain]
        
        return [
            {
                "id": m.id,
                "name": m.name,
                "domain": m.domain,
                "category": m.category,
                "description": m.description
            }
            for m in metrics
        ]
    except Exception as exc:
        log.error("Failed to list metrics: %s", exc)
        return []


@mcp.tool()
def get_dataset_info(dataset_id: str, workspace_dir: str | None = None) -> dict:
    """
    Return metadata and limitations for a hydrological dataset (e.g. USGS NWIS, gridMET).
    
    Use this to understand spatial/temporal coverage and citation requirements.
    """
    try:
        registry = get_registry(workspace_dir)
        ds = registry.datasets.get(dataset_id)
        if not ds:
            return _tool_error_to_dict(ValueError(f"Dataset '{dataset_id}' not found in registry."))
        return ds.model_dump()
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def list_known_datasets(domain: str | None = None, workspace_dir: str | None = None) -> list[dict]:
    """Return all known datasets, optionally filtered by domain."""
    try:
        registry = get_registry(workspace_dir)
        datasets = list(registry.datasets.values())
        if domain:
            datasets = [d for d in datasets if d.domain == domain]
        
        return [
            {
                "id": d.id,
                "name": d.name,
                "domain": d.domain,
                "category": d.category,
                "description": d.description
            }
            for d in datasets
        ]
    except Exception as exc:
        log.error("Failed to list datasets: %s", exc)
        return []


@mcp.tool()
def get_equation_definition(equation_id: str, workspace_dir: str | None = None) -> dict:
    """Return the definition, formula, and assumptions for a scientific equation."""
    try:
        registry = get_registry(workspace_dir)
        eq = registry.equations.get(equation_id)
        if not eq:
            return _tool_error_to_dict(ValueError(f"Equation '{equation_id}' not found in registry."))
        return eq.model_dump()
    except Exception as exc:
        return _tool_error_to_dict(exc)
