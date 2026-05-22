"""DEM preparation helpers for pysheds routing."""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

_MAX_NODATA_WARN_FRAC = 0.10


def prepare_dem_array(dem_arr: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
    """
    Mask bad elevations and fill nodata for hydrologic conditioning.

    Uses median fill (required for continuous surface before pit/dep filling).
    Large nodata fractions are logged because they often indicate tile seams or
    ocean masks that distort local flow directions.
    """
    arr = dem_arr.astype(np.float32)
    arr = np.where(np.isfinite(arr), arr, np.nan)
    arr = np.where(arr < -500, np.nan, arr)

    valid = np.isfinite(arr)
    nan_frac = float(1.0 - valid.mean()) if arr.size else 0.0
    if nan_frac > _MAX_NODATA_WARN_FRAC:
        log.warning(
            "DEM has %.1f%% nodata/invalid cells; routing accuracy may degrade near gaps",
            nan_frac * 100,
        )

    if not valid.any():
        raise RuntimeError("DEM has no valid elevation cells.")

    elev_range = float(np.nanmax(arr) - np.nanmin(arr))
    if elev_range < 1.0:
        raise RuntimeError("DEM has insufficient elevation contrast for routing.")

    fill = float(np.nanmedian(arr))
    arr = np.where(valid, arr, fill)

    return arr, {"nan_frac": nan_frac, "elev_range_m": elev_range, "fill_elev_m": fill}
