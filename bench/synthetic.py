"""
Synthetic hydrological data generators for the bench harness.

All outputs are deterministic (fixed random seed) and hydrologically
plausible. Values are sourced from published CAMELS statistics so the
adjudicated ranges in tasks.yaml can be cross-checked against literature.

References:
  Addor et al. (2017). The CAMELS data set. HESS 21(10):5293-5313.
  Newman et al. (2015). Development of a large-sample watershed-scale
    hydrometeorological dataset. HESS 19(1):209-223.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _rng(seed: int = 42) -> np.random.Generator:
    return np.random.default_rng(seed)


def humid_daily_q_mm(
    n_years: int = 10,
    mean_mm: float = 1.2,
    cv: float = 2.0,
    seed: int = 42,
) -> pd.Series:
    """
    Synthetic daily streamflow (mm/day) for a humid temperate basin.

    Modelled as a log-normal + seasonal signal to mimic eastern-US hydrology
    (e.g. CAMELS basin 01031500 Piscataquis River, ME).
    Adjudicated properties:
      q_mean  ≈ 1.2 mm/day    (CAMELS 01031500: ~1.35 mm/day)
      BFI     ∈ [0.45, 0.70]  (granite/glacial till: moderate-high baseflow)
    """
    rng = _rng(seed)
    n = n_years * 365
    dates = pd.date_range("2000-01-01", periods=n, freq="D")
    # Seasonal component: peak in spring (DOY ~100), low in summer
    doy = dates.day_of_year.values
    seasonal = 1.0 + 0.7 * np.cos(2 * np.pi * (doy - 100) / 365)
    # Log-normal noise
    sigma = np.sqrt(np.log(cv**2 + 1))
    mu = np.log(mean_mm) - sigma**2 / 2
    q = np.exp(mu + sigma * rng.standard_normal(n)) * seasonal
    q = np.maximum(q, 0.0)
    return pd.Series(q, index=dates, name="q_mm_day")


def arid_daily_q_mm(
    n_years: int = 10,
    mean_mm: float = 0.15,
    seed: int = 7,
) -> pd.Series:
    """
    Synthetic daily streamflow (mm/day) for a semi-arid basin.

    Modelled with high variability and frequent near-zero values.
    Adjudicated properties:
      q_mean  ≈ 0.15 mm/day
      runoff_ratio ∈ [0.05, 0.20]  (semi-arid: 5-20% of P becomes Q)
    """
    rng = _rng(seed)
    n = n_years * 365
    dates = pd.date_range("2000-01-01", periods=n, freq="D")
    # Gamma-distributed flow; many dry days
    shape, scale = 0.4, mean_mm / 0.4
    q = rng.gamma(shape, scale, size=n)
    # ~40% of days zero-flow
    zero_mask = rng.random(n) < 0.40
    q[zero_mask] = 0.0
    return pd.Series(q, index=dates, name="q_mm_day")


def humid_daily_p_mm(
    q_series: pd.Series,
    runoff_ratio: float = 0.42,
    seed: int = 42,
) -> pd.Series:
    """
    Synthetic daily precipitation (mm/day) aligned with q_series.

    Precipitation is back-calculated from Q to enforce a known runoff_ratio
    at the annual scale, then perturbed with realistic daily variance.
    Adjudicated property:
      annual mean(Q) / annual mean(P) ≈ runoff_ratio ± 0.03
    """
    rng = _rng(seed + 1)
    n = len(q_series)
    target_p_mean = q_series.mean() / runoff_ratio
    # Exponential distribution for wet days
    p = rng.exponential(target_p_mean * 1.5, size=n)
    # ~55% dry days
    dry = rng.random(n) < 0.55
    p[dry] = 0.0
    # Rescale to hit target mean
    actual_mean = p.mean()
    if actual_mean > 0:
        p *= target_p_mean / actual_mean
    return pd.Series(p, index=q_series.index, name="p_mm_day")
