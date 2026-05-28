"""
Global watershed delineation (lat/lon outlets).

Tiered routing: NLDI where authoritative, then MERIT Hydro / MERIT-Basins
global routes with raw DEM only as an explicitly lower-confidence fallback.
"""

from __future__ import annotations

from typing import Any


def delineate_from_point(*args: Any, **kwargs: Any):
    """Lazy import so pysheds/numba load only when delineation runs."""
    from ai_hydro.analysis.delineation.router import delineate_from_point as _impl

    return _impl(*args, **kwargs)


__all__ = ["delineate_from_point"]
