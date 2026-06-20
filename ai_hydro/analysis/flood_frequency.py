"""Compatibility shim — implementation moved to aihydro-watershed (Wave A3)."""
from __future__ import annotations

from aihydro_watershed.signatures.flood_frequency import *  # noqa: F401, F403
from aihydro_watershed.signatures.flood_frequency import _EULER_GAMMA  # noqa: F401  (private names)
