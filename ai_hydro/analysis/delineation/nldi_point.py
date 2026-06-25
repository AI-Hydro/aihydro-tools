"""Compatibility shim — implementation moved to aihydro-watershed (Wave A3)."""
from __future__ import annotations

from aihydro_watershed.delineation.nldi_point import *  # noqa: F401, F403
from aihydro_watershed.delineation.nldi_point import _normalize_nldi_basins  # noqa: F401  (private names)
