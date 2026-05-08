"""
Workflow Manifest models.

Workflows are static maps of steps, tools, and recommended validators.
They guide the agent through complex scientific tasks without 
requiring a full execution engine.
"""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel


class WorkflowStep(BaseModel):
    """A single step in a scientific workflow."""
    id: str
    tool: str
    depends_on: list[str] = []
    description: str | None = None


class WorkflowManifest(BaseModel):
    """
    A static map of a scientific process.
    """
    id: str
    skill_ref: str | None = None      # Optional link to a SKILL.md
    description: str
    steps: list[WorkflowStep]
    recommended_validators: dict[str, list[str]] = {
        "after_fetch": [],
        "after_analysis": [],
        "after_evaluation": []
    }
    assumptions_to_declare: list[str] = []
