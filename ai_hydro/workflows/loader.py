"""
Workflow Manifest loader.

Discovers and validates YAML workflow manifests from:
  1. Built-in (ai_hydro/workflows/)
  2. Workspace (.aihydrorules/workflows/)
"""
from __future__ import annotations

import logging
import yaml
from pathlib import Path
from pydantic import ValidationError
from ai_hydro.workflows.models import WorkflowManifest

log = logging.getLogger("ai_hydro.workflows")

_WORKFLOWS_DIR = Path(__file__).parent
_WORKSPACE_SUBPATH = Path(".aihydrorules") / "workflows"


def load_workflow(workflow_id: str, workspace_dir: str | None = None) -> WorkflowManifest | None:
    """Load and validate a workflow manifest by ID."""
    # Check workspace first
    if workspace_dir:
        ws_path = Path(workspace_dir) / _WORKSPACE_SUBPATH / f"{workflow_id}.workflow.yaml"
        if ws_path.exists():
            return _load_file(ws_path)
            
    # Check built-in
    builtin_path = _WORKFLOWS_DIR / f"{workflow_id}.workflow.yaml"
    if builtin_path.exists():
        return _load_file(builtin_path)
        
    return None


def list_workflows(workspace_dir: str | None = None) -> list[dict]:
    """List all available workflows across all sources."""
    workflows: dict[str, dict] = {}
    
    # 1. Built-in
    for p in _WORKFLOWS_DIR.glob("*.workflow.yaml"):
        wf = _load_file(p)
        if wf:
            workflows[wf.id] = {"id": wf.id, "description": wf.description, "source": "built-in"}
            
    # 2. Workspace
    if workspace_dir:
        ws_path = Path(workspace_dir) / _WORKSPACE_SUBPATH
        if ws_path.exists():
            for p in ws_path.glob("*.workflow.yaml"):
                wf = _load_file(p)
                if wf:
                    workflows[wf.id] = {"id": wf.id, "description": wf.description, "source": "workspace"}
                    
    return sorted(list(workflows.values()), key=lambda x: x["id"])


def _load_file(path: Path) -> WorkflowManifest | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return WorkflowManifest(**raw)
    except ValidationError as exc:
        log.warning("Workflow manifest '%s' failed validation: %s", path.name, exc)
    except Exception as exc:
        log.warning("Failed to load workflow manifest '%s': %s", path.name, exc)
    return None
