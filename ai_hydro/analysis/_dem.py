"""Compatibility shim — implementation moved to aihydro-watershed (Wave A3)."""
from __future__ import annotations

from aihydro_watershed.characterize._dem import *  # noqa: F401, F403
from aihydro_watershed.characterize._dem import _geom_in_conus  # noqa: F401  (private names)
