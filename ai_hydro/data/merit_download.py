"""Compatibility shim — implementation moved to aihydro-watershed (Wave A3).

Note (2.8.9): the three functions re-exported here are deprecated in aihydro-watershed 0.4.0 (they stage the
pinned, SHA-256-verified ``megabasins.db`` / ``basins##.db`` / ``rivers##.db`` instead of the removed Google
Drive shapefiles) and emit a ``DeprecationWarning`` when *called*, not on import. Nothing in this package calls
them; use ``MeritDataManager.ensure_basins_region``. ``tests/test_merit_download_shim.py`` pins that the import
is warning-free and that a call warns rather than breaks.
"""
from __future__ import annotations

from aihydro_watershed.merit.merit_download import *  # noqa: F401, F403
