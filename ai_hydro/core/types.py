"""
AI-Hydro Core Types — compatibility shim
========================================

The universal result contract (``HydroResult``, ``HydroMeta``, ``DataSource``,
``HydroTool``) was **promoted into the substrate package** ``aihydro-core``
(``aihydro_core.contracts``) so every ecosystem package — data, watershed, lsh,
modelling — shares one typed return type instead of duplicating it.

This module now **re-exports** those names from ``aihydro_core`` for backward
compatibility. Existing imports keep working unchanged:

>>> from ai_hydro.core import HydroResult, HydroMeta, DataSource, ToolError

New code should import the contract directly from the substrate:

>>> from aihydro_core import HydroResult, HydroMeta, DataSource

This shim will be kept for at least one release; prefer the ``aihydro_core``
import in new code.

NOTE on ``ToolError``: a richer, tool-facing ``ToolError`` (with ``tool`` /
``recovery`` / ``alternatives`` fields) historically lived here and is depended
on across ``aihydro-tools``. It is **kept local** below for now. Unifying it
with ``aihydro_core.primitives.errors.ToolError`` (which uses a ``details`` dict)
is a deliberate follow-up — not done here to avoid a behavior change.
"""

from __future__ import annotations

# Re-export the promoted contract from the substrate.
from aihydro_core.contracts import (  # noqa: F401
    DataSource,
    HydroMeta,
    HydroResult,
    HydroTool,
)


# ---------------------------------------------------------------------------
# Structured error type (kept local — see module docstring NOTE)
# ---------------------------------------------------------------------------

class ToolError(Exception):
    """
    Structured error from an AI-Hydro tool.

    Provides machine-readable error context so agents can reason
    about recovery — try an alternative, ask for clarification, etc.
    """
    def __init__(
        self,
        code: str,
        message: str,
        tool: str,
        recovery: str | None = None,
        alternatives: list[str] | None = None,
    ):
        """
        Parameters
        ----------
        code : str
            Short error code, e.g. 'GAUGE_NOT_FOUND', 'NETWORK_ERROR'
        message : str
            Human-readable error description
        tool : str
            Tool that raised the error
        recovery : str, optional
            Suggested recovery action for the agent
        alternatives : list[str], optional
            Alternative gauge IDs, methods, or approaches to try
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.tool = tool
        self.recovery = recovery
        self.alternatives = alternatives or []

    def to_dict(self) -> dict:
        return {
            "error": True,
            "code": self.code,
            "message": self.message,
            "tool": self.tool,
            "recovery": self.recovery,
            "alternatives": self.alternatives,
        }


__all__ = ["DataSource", "HydroMeta", "HydroResult", "HydroTool", "ToolError"]
