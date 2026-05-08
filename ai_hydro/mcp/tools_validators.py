"""
Physics-based scientific validators.

Validators reduce invalid scientific composition at the workflow level.
They are agent-callable tools that produce diagnostic outputs (ValidatorResult).
"""
from __future__ import annotations

import logging
from typing import Literal
from ai_hydro.mcp.app import mcp
from ai_hydro.session import HydroSession
from ai_hydro.validators.models import ValidatorResult
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp")


@mcp.tool()
def check_water_balance_consistency(session_id: str) -> dict:
    """
    Check if annual runoff is less than or equal to annual precipitation.
    
    Runoff ratio > 1.0 indicates a potential error or significant 
    non-precipitation water source (snowmelt, groundwater).
    """
    try:
        session = HydroSession.load(session_id)
        sigs = session.get("signatures")
        if not sigs:
            return ValidatorResult(
                validator="water_balance_consistency",
                status="insufficient_data",
                message="Hydrological signatures (runoff_ratio) not found in session.",
                affected_slots=["signatures"],
                recommendation="Run extract_hydrological_signatures first."
            ).model_dump()

        rr = sigs.get("data", {}).get("runoff_ratio")
        if rr is None:
            return ValidatorResult(
                validator="water_balance_consistency",
                status="insufficient_data",
                message="Runoff ratio missing from signatures data.",
                affected_slots=["signatures"]
            ).model_dump()

        if rr > 1.0:
            severity = "medium" if rr < 1.1 else "high"
            return ValidatorResult(
                validator="water_balance_consistency",
                status="warning",
                severity=severity,
                message=f"Annual runoff ratio is {rr:.2f} (> 1.0).",
                recommendation="Investigate if snowmelt, groundwater release, or gauging errors are present.",
                affected_slots=["signatures"]
            ).model_dump()

        return ValidatorResult(
            validator="water_balance_consistency",
            status="pass",
            message=f"Water balance is consistent (Runoff Ratio: {rr:.2f}).",
            affected_slots=["signatures"]
        ).model_dump()
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def check_temporal_alignment(session_id: str, slot_a: str, slot_b: str) -> dict:
    """
    Check if two time-series slots share the same temporal range.
    """
    try:
        session = HydroSession.load(session_id)
        a = session.get(slot_a)
        b = session.get(slot_b)

        if not a or not b:
            return ValidatorResult(
                validator="temporal_alignment",
                status="insufficient_data",
                message=f"One or both slots ({slot_a}, {slot_b}) are missing.",
                affected_slots=[slot_a, slot_b]
            ).model_dump()

        ma = a.get("meta", {}).get("params", {})
        mb = b.get("meta", {}).get("params", {})
        
        start_a, end_a = ma.get("start_date"), ma.get("end_date")
        start_b, end_b = mb.get("start_date"), mb.get("end_date")

        if not all([start_a, end_a, start_b, end_b]):
            return ValidatorResult(
                validator="temporal_alignment",
                status="warning",
                severity="low",
                message="Missing temporal metadata (start/end dates). Cannot verify alignment.",
                affected_slots=[slot_a, slot_b]
            ).model_dump()

        if start_a == start_b and end_a == end_b:
            return ValidatorResult(
                validator="temporal_alignment",
                status="pass",
                message=f"Slots aligned: {start_a} to {end_a}.",
                affected_slots=[slot_a, slot_b]
            ).model_dump()

        return ValidatorResult(
            validator="temporal_alignment",
            status="fail",
            severity="medium",
            message=f"Temporal mismatch: {slot_a} ({start_a} to {end_a}) vs {slot_b} ({start_b} to {end_b}).",
            recommendation="Re-fetch or truncate data to ensure consistent temporal overlap.",
            affected_slots=[slot_a, slot_b]
        ).model_dump()

    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def check_unit_consistency(session_id: str, slot: str, expected_units: str) -> dict:
    """
    Check if a session slot uses the expected scientific units.
    """
    try:
        session = HydroSession.load(session_id)
        s = session.get(slot)
        if not s:
            return ValidatorResult(
                validator="unit_consistency",
                status="insufficient_data",
                message=f"Slot '{slot}' not found.",
                affected_slots=[slot]
            ).model_dump()

        # Check 'data.units' or 'meta.units'
        actual_units = s.get("data", {}).get("units") or s.get("meta", {}).get("units")
        
        if not actual_units:
            return ValidatorResult(
                validator="unit_consistency",
                status="warning",
                severity="low",
                message=f"Units not explicitly declared for slot '{slot}'.",
                affected_slots=[slot]
            ).model_dump()

        if actual_units == expected_units:
            return ValidatorResult(
                validator="unit_consistency",
                status="pass",
                message=f"Units consistent: {actual_units}.",
                affected_slots=[slot]
            ).model_dump()

        return ValidatorResult(
            validator="unit_consistency",
            status="fail",
            severity="high",
            message=f"Unit mismatch for '{slot}': Found '{actual_units}', expected '{expected_units}'.",
            recommendation=f"Apply unit conversion to {expected_units} before proceeding.",
            affected_slots=[slot]
        ).model_dump()

    except Exception as exc:
        return _tool_error_to_dict(exc)
