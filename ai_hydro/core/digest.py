from __future__ import annotations
from typing import Literal
from pydantic import BaseModel

class UnitOutcome(BaseModel):
    """Outcome for a single unit of work (e.g. one USGS gauge)."""
    id: str
    status: Literal["complete", "failed"]
    message: str | None = None
    results: dict | None = None

class SubAgentDigest(BaseModel):
    """
    Standardized return contract for sub-agent delegation.
    
    Orchestrator models expect this shape to maintain consistent reasoning
    across different parallelized tasks.
    """
    status: Literal["complete", "partial", "failed"]
    summary: str
    per_unit_outcomes: list[UnitOutcome] | None = None
    recommended_next_action: str | None = None
    context_size_used: int | None = None
