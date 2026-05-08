"""
Scientific validator schemas.

Validators produce diagnostic outputs, not just binary pass/fail.
This allows the agent to handle expected scientific anomalies 
(like snowmelt causing runoff > precip) gracefully.
"""
from __future__ import annotations
from typing import Literal, Any
from pydantic import BaseModel


class ValidatorResult(BaseModel):
    """
    Diagnostic output from a scientific validator.
    """
    validator: str
    status: Literal["pass", "warning", "fail", "not_applicable", "insufficient_data"]
    severity: Literal["low", "medium", "high", "critical"] | None = None
    message: str
    recommendation: str | None = None
    affected_slots: list[str] = []
    metadata: dict[str, Any] = {}
