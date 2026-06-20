"""Compatibility shim — implementation moved to aihydro-watershed (Wave A3)."""
from __future__ import annotations

from aihydro_watershed.signatures.signatures import *  # noqa: F401, F403
from aihydro_watershed.signatures.signatures import _fetch_precipitation_data_bygeom, _lyne_hollick_baseflow  # noqa: F401  (private names)
