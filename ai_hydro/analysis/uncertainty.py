"""
Uncertainty quantification — compatibility shim.

The bootstrap implementations (``bootstrap_ci``, ``block_bootstrap_ci``,
``bootstrap_dict``) were **promoted into the substrate package**
``aihydro-core`` (``aihydro_core.science.uncertainty``) so watershed,
lsh, and other domain packages can use them without pulling in aihydro-tools.

This module re-exports those names for backward compatibility.  Existing imports
keep working unchanged:

>>> from ai_hydro.analysis.uncertainty import bootstrap_dict, bootstrap_ci

New code should import from the substrate directly:

>>> from aihydro_core.science.uncertainty import bootstrap_ci, bootstrap_dict
"""
from __future__ import annotations

from aihydro_core.science.uncertainty import (  # noqa: F401
    UncertaintyResult,
    bootstrap_ci,
    block_bootstrap_ci,
    bootstrap_dict,
    estimate_has_required_keys,
    estimate_is_valid,
    null_estimate,
)

__all__ = [
    "UncertaintyResult",
    "bootstrap_ci",
    "block_bootstrap_ci",
    "bootstrap_dict",
    "estimate_has_required_keys",
    "estimate_is_valid",
    "null_estimate",
]
