"""Compatibility shim — implementation moved to aihydro-watershed (Wave A3)."""
from __future__ import annotations

from aihydro_watershed.merit.wbd_layers import *  # noqa: F401, F403
from aihydro_watershed.merit.wbd_layers import _huc_code_from_row  # noqa: F401  (private names)
