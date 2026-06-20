"""Compatibility shim — implementation moved to aihydro-watershed (Wave A3)."""
from __future__ import annotations

from aihydro_watershed.terrain.curve_number import *  # noqa: F401, F403
from aihydro_watershed.terrain.curve_number import _build_joint_cn_lookup, _create_cn_lookup_table, _create_cn_grid_from_data, _classify_soil_hydrologic_group, _vectorised_cn_lookup, _CN_CHUNK_TRIGGER  # noqa: F401  (private names)
