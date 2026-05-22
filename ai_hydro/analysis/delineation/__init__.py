"""
Global watershed delineation (lat/lon outlets).

Tiered routing: MERIT vector snap → cloud DEM pysheds (fast) → upstream-delineator (accurate).
"""

from __future__ import annotations

from typing import Any


def delineate_from_point(*args: Any, **kwargs: Any):
    """Lazy import so pysheds/numba load only when delineation runs."""
    from ai_hydro.analysis.delineation.router import delineate_from_point as _impl

    return _impl(*args, **kwargs)


__all__ = ["delineate_from_point"]
