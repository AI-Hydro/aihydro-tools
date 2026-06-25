"""Compatibility shim — implementation moved to aihydro-watershed (Wave A3)."""
from __future__ import annotations

from aihydro_watershed.characterize.geomorphic import *  # noqa: F401, F403
from aihydro_watershed.characterize.geomorphic import _slope_horn_kernel, _SLOPE_CHUNK_TRIGGER  # noqa: F401  (private names)
