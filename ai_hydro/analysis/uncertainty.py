"""
Uncertainty quantification for hydrology analysis functions.

Provides generalized bootstrap confidence intervals used across
signatures, metrics, and geomorphic analysis. Generalizes the
bootstrap pattern from flood_frequency.py into a reusable module.

Key exports
-----------
- ``UncertaintyResult``  — standard CI container dict shape
- ``bootstrap_ci``       — IID bootstrap for independent samples
- ``block_bootstrap_ci`` — moving-block bootstrap for autocorrelated series
"""
from __future__ import annotations

import logging
from typing import Any, Callable, TypedDict

import numpy as np

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

class UncertaintyResult(TypedDict):
    """
    Standard uncertainty container returned by all CI functions.

    Fields
    ------
    value    : float — point estimate (fn(data)).
    ci_low   : float — lower confidence bound.
    ci_high  : float — upper confidence bound.
    method   : str   — 'bootstrap_iid' | 'bootstrap_block'.
    n        : int   — number of data points used.
    ci_level : float — confidence level (e.g. 0.90).
    """
    value: float
    ci_low: float
    ci_high: float
    method: str
    n: int
    ci_level: float


def _quantile_bounds(samples: list[float], ci: float) -> tuple[float, float]:
    lo = (1.0 - ci) / 2.0
    hi = 1.0 - lo
    arr = np.asarray(samples, dtype=float)
    return float(np.quantile(arr, lo)), float(np.quantile(arr, hi))


# ---------------------------------------------------------------------------
# IID bootstrap
# ---------------------------------------------------------------------------

def bootstrap_ci(
    fn: Callable[[np.ndarray], float],
    data: np.ndarray,
    *,
    n: int = 500,
    ci: float = 0.90,
    random_state: int = 0,
) -> UncertaintyResult:
    """
    Estimate a confidence interval for fn(data) by IID bootstrap resampling.

    Parameters
    ----------
    fn          : scalar-valued function of a 1-D numpy array.
    data        : 1-D array-like; NaN values are stripped before resampling.
    n           : number of bootstrap replicates (default 500).
    ci          : confidence level (default 0.90 → 5th/95th percentiles).
    random_state: RNG seed for reproducibility.

    Returns
    -------
    UncertaintyResult with method='bootstrap_iid'.
    """
    arr = np.asarray(data, dtype=float)
    arr = arr[~np.isnan(arr)]
    size = arr.size
    if size < 5:
        raise ValueError(
            f"bootstrap_ci needs ≥5 valid data points, got {size}. "
            "Increase the data length or reduce the required minimum."
        )

    point = float(fn(arr))

    rng = np.random.default_rng(random_state)
    samples: list[float] = []
    for _ in range(n):
        resample = rng.choice(arr, size=size, replace=True)
        try:
            samples.append(float(fn(resample)))
        except Exception:
            continue

    if len(samples) < 10:
        log.warning(
            "bootstrap_ci: only %d of %d replicates succeeded; CI may be unreliable.",
            len(samples), n,
        )
        return UncertaintyResult(
            value=point, ci_low=float("nan"), ci_high=float("nan"),
            method="bootstrap_iid", n=size, ci_level=ci,
        )

    lo, hi = _quantile_bounds(samples, ci)
    return UncertaintyResult(value=point, ci_low=lo, ci_high=hi,
                              method="bootstrap_iid", n=size, ci_level=ci)


# ---------------------------------------------------------------------------
# Block bootstrap (for autocorrelated / temporal series)
# ---------------------------------------------------------------------------

def block_bootstrap_ci(
    fn: Callable[[np.ndarray], float],
    data: np.ndarray,
    *,
    block_size: int | None = None,
    n: int = 500,
    ci: float = 0.90,
    random_state: int = 0,
) -> UncertaintyResult:
    """
    Estimate a CI for fn(data) using a moving-block bootstrap.

    Suitable for streamflow or precipitation series with temporal
    autocorrelation, where IID resampling under-covers the true interval.

    Parameters
    ----------
    fn          : scalar-valued function of a 1-D numpy array.
    data        : 1-D array-like; NaN values are stripped before resampling.
    block_size  : length of each block (default: int(len(data)^(1/3)), min 5).
    n           : number of bootstrap replicates (default 500).
    ci          : confidence level (default 0.90).
    random_state: RNG seed.

    Returns
    -------
    UncertaintyResult with method='bootstrap_block'.
    """
    arr = np.asarray(data, dtype=float)
    arr = arr[~np.isnan(arr)]
    size = arr.size
    if size < 10:
        raise ValueError(
            f"block_bootstrap_ci needs ≥10 valid data points, got {size}."
        )

    bs = block_size
    if bs is None:
        bs = max(5, int(round(size ** (1.0 / 3.0))))
    bs = int(max(1, min(bs, size)))

    point = float(fn(arr))

    rng = np.random.default_rng(random_state)
    # Positions where a block can start (wrap-around / circular bootstrap)
    n_blocks = int(np.ceil(size / bs))
    max_start = size  # circular: allow wrap

    samples: list[float] = []
    for _ in range(n):
        starts = rng.integers(0, max_start, size=n_blocks)
        parts = []
        for s in starts:
            end = s + bs
            if end <= size:
                parts.append(arr[s:end])
            else:
                # wrap around
                parts.append(np.concatenate([arr[s:], arr[: end - size]]))
        resample = np.concatenate(parts)[:size]
        try:
            samples.append(float(fn(resample)))
        except Exception:
            continue

    if len(samples) < 10:
        log.warning(
            "block_bootstrap_ci: only %d of %d replicates succeeded; CI may be unreliable.",
            len(samples), n,
        )
        return UncertaintyResult(
            value=point, ci_low=float("nan"), ci_high=float("nan"),
            method="bootstrap_block", n=size, ci_level=ci,
        )

    lo, hi = _quantile_bounds(samples, ci)
    return UncertaintyResult(value=point, ci_low=lo, ci_high=hi,
                              method="bootstrap_block", n=size, ci_level=ci)


# ---------------------------------------------------------------------------
# Multi-metric helper
# ---------------------------------------------------------------------------

def bootstrap_dict(
    fns: dict[str, Callable[[np.ndarray], float]],
    data: np.ndarray,
    *,
    use_block: bool = False,
    block_size: int | None = None,
    n: int = 500,
    ci: float = 0.90,
    random_state: int = 0,
) -> dict[str, UncertaintyResult]:
    """
    Run bootstrap CI for multiple metrics over the same data in one pass.

    Parameters
    ----------
    fns        : mapping of metric_name → scalar fn(array).
    data       : shared data array.
    use_block  : use block bootstrap instead of IID (for autocorrelated data).
    block_size : only used when use_block=True.

    Returns
    -------
    dict[metric_name, UncertaintyResult]
    """
    arr = np.asarray(data, dtype=float)
    arr = arr[~np.isnan(arr)]
    size = arr.size

    if size < (10 if use_block else 5):
        raise ValueError(
            f"bootstrap_dict: need ≥{'10' if use_block else '5'} points, got {size}."
        )

    bs = block_size
    if use_block and bs is None:
        bs = max(5, int(round(size ** (1.0 / 3.0))))

    rng = np.random.default_rng(random_state)
    n_blocks = int(np.ceil(size / (bs or 1))) if use_block else None
    point_ests: dict[str, float] = {}
    boot_samples: dict[str, list[float]] = {k: [] for k in fns}

    for k, fn in fns.items():
        try:
            point_ests[k] = float(fn(arr))
        except Exception:
            point_ests[k] = float("nan")

    for _ in range(n):
        if use_block:
            starts = rng.integers(0, size, size=n_blocks)
            parts = []
            for s in starts:
                end = s + bs
                if end <= size:
                    parts.append(arr[s:end])
                else:
                    parts.append(np.concatenate([arr[s:], arr[: end - size]]))
            resample = np.concatenate(parts)[:size]
        else:
            resample = rng.choice(arr, size=size, replace=True)

        for k, fn in fns.items():
            try:
                boot_samples[k].append(float(fn(resample)))
            except Exception:
                continue

    method = "bootstrap_block" if use_block else "bootstrap_iid"
    results: dict[str, UncertaintyResult] = {}
    for k in fns:
        samps = boot_samples[k]
        if len(samps) >= 10:
            lo, hi = _quantile_bounds(samps, ci)
        else:
            lo, hi = float("nan"), float("nan")
        results[k] = UncertaintyResult(
            value=point_ests.get(k, float("nan")),
            ci_low=lo, ci_high=hi,
            method=method, n=size, ci_level=ci,
        )
    return results
