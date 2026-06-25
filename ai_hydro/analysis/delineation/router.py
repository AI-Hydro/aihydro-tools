"""Compatibility shim — implementation moved to aihydro-watershed (Wave A3)."""
from __future__ import annotations

from aihydro_watershed.delineation.router import *  # noqa: F401, F403
from aihydro_watershed.delineation.router import _should_escalate  # noqa: F401  (private names)
