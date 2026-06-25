"""Compatibility shim — implementation moved to aihydro-watershed (Wave A3)."""
from __future__ import annotations

from aihydro_watershed.characterize.twi import *  # noqa: F401, F403
from aihydro_watershed.characterize.twi import compute_twi_result  # noqa: F401  (private names)
