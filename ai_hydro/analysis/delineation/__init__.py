"""Compatibility shim — delineation engine moved to aihydro-watershed (Wave A3)."""
from __future__ import annotations

from typing import Any


def delineate_from_point(*args: Any, **kwargs: Any):
    """Lazy import so pysheds/numba load only when delineation runs."""
    from aihydro_watershed.delineation.router import delineate_from_point as _impl
    return _impl(*args, **kwargs)


__all__ = ["delineate_from_point"]
