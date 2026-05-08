"""
Workflow Manifest MCP tools.

Expose the static roadmap for complex scientific processes to the agent.
"""
from __future__ import annotations

import logging
from ai_hydro.mcp.app import mcp
from ai_hydro.workflows.loader import list_workflows, load_workflow
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp")


@mcp.tool()
def list_available_workflows(workspace_dir: str | None = None) -> list[dict]:
    """
    List all available scientific workflows (e.g. rainfall-runoff benchmarking).
    """
    return list_workflows(workspace_dir)


@mcp.tool()
def get_workflow_manifest(workflow_id: str, workspace_dir: str | None = None) -> dict:
    """
    Return the detailed steps, tool dependencies, and recommended 
    validators for a specific scientific workflow.
    """
    try:
        wf = load_workflow(workflow_id, workspace_dir)
        if not wf:
            return _tool_error_to_dict(ValueError(f"Workflow '{workflow_id}' not found."))
        return wf.model_dump()
    except Exception as exc:
        return _tool_error_to_dict(exc)
