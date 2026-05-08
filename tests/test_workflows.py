import pytest
from pathlib import Path
from ai_hydro.workflows.loader import load_workflow, list_workflows

def test_workflow_load():
    wf = load_workflow("workflow.rainfall_runoff_benchmark")
    assert wf is not None
    assert wf.id == "workflow.rainfall_runoff_benchmark"
    assert len(wf.steps) == 5
    assert "check_temporal_alignment" in wf.recommended_validators["after_fetch"]

def test_workflow_list():
    wfs = list_workflows()
    assert len(wfs) >= 1
    ids = [w["id"] for w in wfs]
    assert "workflow.rainfall_runoff_benchmark" in ids

def test_workspace_workflow(tmp_path):
    ws_dir = tmp_path / "workspace"
    ws_workflows = ws_dir / ".aihydrorules" / "workflows"
    ws_workflows.mkdir(parents=True)
    
    (ws_workflows / "custom.workflow.workflow.yaml").write_text("""
id: custom.workflow
description: "Custom workflow"
steps:
  - id: step1
    tool: test_tool
""", encoding="utf-8")
    
    wf = load_workflow("custom.workflow", workspace_dir=str(ws_dir))
    assert wf is not None
    assert wf.id == "custom.workflow"
    
    wfs = list_workflows(workspace_dir=str(ws_dir))
    ids = [w["id"] for w in wfs]
    assert "custom.workflow" in ids
    # Should find the workspace one
    assert next(w for w in wfs if w["id"] == "custom.workflow")["source"] == "workspace"
