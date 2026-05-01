"""
Baseflow separation methods.

Provides two algorithms:
  - Lyne-Hollick (recursive digital filter, 1979)
  - UKIH (UK Institute of Hydrology five-day interval method, Gustard et al. 1992)

The MCP tool separate_baseflow writes results to the session's baseflow slot.
extract_hydrological_signatures retains its existing BFI scalar for backward
compatibility — this module provides the full daily baseflow series.
"""
from __future__ import annotations

import numpy as np
from typing import Any


def lyne_hollick(
    streamflow: np.ndarray,
    alpha: float = 0.925,
    n_passes: int = 3,
) -> np.ndarray:
    """
    Lyne-Hollick recursive digital filter for baseflow separation.

    Parameters
    ----------
    streamflow : 1-D numpy array
        Daily streamflow series (any unit; must be non-negative).
    alpha : float
        Filter parameter (0.9-0.95 typical). Higher = more smoothing.
    n_passes : int
        Number of forward/backward passes (1 or 3; 3 recommended).

    Returns
    -------
    baseflow : 1-D numpy array, same length as streamflow.
    """
    q = np.asarray(streamflow, dtype=float)
    q = np.where(q < 0, 0.0, q)  # guard against negative values

    def _single_pass(q_in: np.ndarray, forward: bool) -> np.ndarray:
        data = q_in if forward else q_in[::-1]
        bf = np.empty_like(data)
        bf[0] = data[0]
        for t in range(1, len(data)):
            qf = alpha * bf[t - 1] + (1 - alpha) / 2 * (data[t] + data[t - 1])
            bf[t] = min(qf, data[t])
        return bf if forward else bf[::-1]

    bf = q.copy()
    for i in range(n_passes):
        forward = (i % 2 == 0)
        bf = _single_pass(bf if i == 0 else q - (q - bf), forward)
    # Ensure baseflow <= total flow and >= 0
    bf = np.clip(bf, 0.0, q)
    return bf


def ukih(streamflow: np.ndarray) -> np.ndarray:
    """
    UKIH (UK Institute of Hydrology) five-day interval baseflow separation.

    Based on Gustard et al. (1992). Uses the minimum of each 5-day block
    as control points and connects them with straight lines.

    Parameters
    ----------
    streamflow : 1-D numpy array
        Daily streamflow series (any unit; must be non-negative).

    Returns
    -------
    baseflow : 1-D numpy array, same length as streamflow.
    """
    q = np.asarray(streamflow, dtype=float)
    q = np.where(q < 0, 0.0, q)
    n = len(q)
    block_size = 5
    n_blocks = n // block_size
    if n_blocks < 2:
        # Fallback: not enough data for UKIH; return zeros
        return np.zeros(n)

    # Find minimum in each block
    mins = []
    mins_idx = []
    for b in range(n_blocks):
        start = b * block_size
        end = start + block_size
        local_min_offset = int(np.argmin(q[start:end]))
        mins.append(q[start + local_min_offset])
        mins_idx.append(start + local_min_offset)

    # Keep only turning points (each min < 0.9 * adjacent mins)
    turning_q = [mins[0]]
    turning_idx = [mins_idx[0]]
    for i in range(1, len(mins) - 1):
        if 0.9 * mins[i] < mins[i - 1] and 0.9 * mins[i] < mins[i + 1]:
            turning_q.append(mins[i])
            turning_idx.append(mins_idx[i])
    turning_q.append(mins[-1])
    turning_idx.append(mins_idx[-1])

    # Interpolate between turning points
    bf = np.interp(np.arange(n), turning_idx, turning_q)
    bf = np.clip(bf, 0.0, q)
    return bf


def compute_bfi(streamflow: np.ndarray, baseflow: np.ndarray) -> float:
    """Baseflow Index = sum(baseflow) / sum(streamflow), clipped to [0, 1]."""
    total = np.nansum(streamflow)
    if total <= 0:
        return float("nan")
    return float(np.clip(np.nansum(baseflow) / total, 0.0, 1.0))
