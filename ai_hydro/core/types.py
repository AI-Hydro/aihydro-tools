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

NOTE on ``ToolError``: unified into ``aihydro_core.primitives.errors.ToolError``
as a superset of both the core (code/message/details) and the tool-facing
(code/message/tool/recovery/alternatives) signatures. Re-exported here.
"""

from __future__ import annotations

# Re-export the promoted contract from the substrate.
from aihydro_core.contracts import (  # noqa: F401
    DataSource,
    HydroMeta,
    HydroResult,
    HydroTool,
)

# ToolError unified into core — re-export for backward compatibility.
from aihydro_core.primitives.errors import ToolError  # noqa: F401


__all__ = ["DataSource", "HydroMeta", "HydroResult", "HydroTool", "ToolError"]
