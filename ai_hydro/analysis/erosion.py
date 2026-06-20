"""Compatibility shim — implementation moved to aihydro-watershed (Wave A3)."""
from __future__ import annotations

from aihydro_watershed.terrain.erosion import *  # noqa: F401, F403
from aihydro_watershed.terrain.erosion import _DEFAULT_C_FACTORS  # noqa: F401  (private names)
