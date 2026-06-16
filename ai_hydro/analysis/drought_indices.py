"""
Standardised drought indices — SPI and SPEI.

Pure-compute kernels over a daily climate series (precipitation for SPI;
precipitation and PET for SPEI) — no I/O, no session, no network. The MCP
wrapper extracts the columns from session.forcing.

SPI (Standardised Precipitation Index, McKee et al. 1993) and SPEI
(Standardised Precipitation-Evapotranspiration Index, Vicente-Serrano et al.
2010) express how anomalously wet or dry an accumulation period is, on a unit
normal scale: 0 = median, negative = drier than normal, positive = wetter.
Conventional severity bands: |index| 1–1.5 moderate, 1.5–2 severe, >2 extreme.

Method
------
1. Aggregate the daily series to **monthly** totals (SPI: precip; SPEI: the
   monthly climatic water balance D = precip − PET).
2. Accumulate over the requested `scale_months` window (1/3/6/12-month SPI/SPEI).
3. For each **calendar month** separately (to remove seasonality), fit a
   distribution to the accumulated values across all years, evaluate each value's
   cumulative probability, and map it through the inverse standard-normal CDF
   (probability integral transform → ~N(0,1)).
   - SPI: a gamma distribution with an explicit zero-precipitation mass
     (Lloyd-Hughes & Saunders 2002) — the textbook choice for strictly-positive
     precipitation totals.
   - SPEI: a log-logistic (Fisk) distribution, which admits the negative values
     of the water balance (Vicente-Serrano et al. 2010).
   If a parametric fit fails or is degenerate, the code falls back to a
   non-parametric Gringorten plotting-position standardisation (Farahmand &
   AghaKouchak 2015), which is robust and method-consistent.

The probability integral transform guarantees that, evaluated on its own
calibration data, the index is approximately standard normal — this is the
defining property the known-answer tests assert.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Sequence

import numpy as np

log = logging.getLogger(__name__)

# Clip to p ∈ [0.001, 0.999] → index ∈ [-3.09, +3.09], standard SPI practice.
_P_FLOOR, _P_CEIL = 0.001, 0.999


def spi(
    precip: Sequence[float],
    dates: Sequence,
    scale_months: int = 3,
) -> Dict[str, object]:
    """Standardised Precipitation Index.

    Parameters
    ----------
    precip : sequence of float
        Daily precipitation (mm). NaNs treated as 0 for monthly totals.
    dates : sequence of datetime-like
        One date per value (required — the index is monthly).
    scale_months : int
        Accumulation window in months (1, 3, 6, 12, …).

    Returns
    -------
    dict: index, index_months (YYYY-MM), scale_months, index_name, mean, std.
    """
    monthly = _to_monthly(precip, dates, how="sum")
    return _standardise(monthly, scale_months, "SPI", kind="gamma")


def spei(
    precip: Sequence[float],
    pet: Sequence[float],
    dates: Sequence,
    scale_months: int = 3,
) -> Dict[str, object]:
    """Standardised Precipitation-Evapotranspiration Index.

    Parameters
    ----------
    precip, pet : sequence of float
        Daily precipitation and potential evapotranspiration (mm).
    dates : sequence of datetime-like
        One date per value.
    scale_months : int
        Accumulation window in months.
    """
    import pandas as pd
    p = pd.Series(np.asarray(precip, dtype=float))
    e = pd.Series(np.asarray(pet, dtype=float))
    if len(p) != len(e):
        raise ValueError("precip and pet must be the same length for SPEI.")
    balance = (p.fillna(0.0) - e.fillna(0.0)).values
    monthly = _to_monthly(balance, dates, how="sum")
    return _standardise(monthly, scale_months, "SPEI", kind="loglogistic")


# ── Internals ───────────────────────────────────────────────────────────────

def _to_monthly(values: Sequence[float], dates: Sequence, how: str = "sum"):
    """Aggregate a daily series to monthly. Returns a pandas Series indexed by
    month-end Timestamp."""
    import pandas as pd
    s = pd.Series(np.asarray(values, dtype=float),
                  index=pd.to_datetime(pd.Series(list(dates)).values))
    s = s.fillna(0.0)
    return s.resample("ME").sum() if how == "sum" else s.resample("ME").mean()


def _standardise(monthly, scale_months: int, name: str, kind: str) -> Dict[str, object]:
    import pandas as pd
    if monthly.size < scale_months + 12:
        raise ValueError(
            f"{name}-{scale_months} needs more data: have {monthly.size} months, "
            f"need at least {scale_months + 12}."
        )
    acc = monthly.rolling(scale_months).sum().dropna()
    months = acc.index.month.values
    vals = acc.values.astype(float)

    out = np.full(vals.size, np.nan)
    for m in range(1, 13):
        mask = months == m
        if mask.sum() < 5:
            continue
        out[mask] = _to_standard_normal(vals[mask], kind)

    valid = out[~np.isnan(out)]
    return {
        "index": [round(float(x), 4) if np.isfinite(x) else None for x in out],
        "index_months": [d.strftime("%Y-%m") for d in acc.index],
        "scale_months": int(scale_months),
        "index_name": name,
        "n_months": int(out.size),
        "mean": round(float(valid.mean()), 4) if valid.size else None,
        "std": round(float(valid.std(ddof=0)), 4) if valid.size else None,
    }


def _to_standard_normal(x: np.ndarray, kind: str) -> np.ndarray:
    """Probability integral transform of one calendar month's values → N(0,1)."""
    from scipy.stats import norm
    try:
        if kind == "gamma":
            p = _gamma_cdf_with_zeros(x)
        else:
            p = _loglogistic_cdf(x)
        if p is None:
            raise ValueError("parametric fit degenerate")
    except Exception as exc:
        log.debug("%s fit fell back to non-parametric (%s).", kind, exc)
        p = _gringorten_cdf(x)
    p = np.clip(p, _P_FLOOR, _P_CEIL)
    return norm.ppf(p)


def _gamma_cdf_with_zeros(x: np.ndarray) -> Optional[np.ndarray]:
    """Mixed gamma CDF: zero-mass q + (1-q)·Gamma (Lloyd-Hughes & Saunders 2002)."""
    from scipy.stats import gamma
    n = x.size
    zeros = int((x <= 0).sum())
    q = zeros / n
    pos = x[x > 0]
    if pos.size < 4 or np.allclose(pos, pos[0]):
        return None
    a, loc, scale = gamma.fit(pos, floc=0)
    if not np.isfinite(a) or scale <= 0:
        return None
    g = gamma.cdf(x, a, loc=0, scale=scale)
    return q + (1.0 - q) * g


def _loglogistic_cdf(x: np.ndarray) -> Optional[np.ndarray]:
    """Log-logistic (Fisk) CDF after a location shift so values are positive."""
    from scipy.stats import fisk
    if np.allclose(x, x[0]):
        return None
    shift = 0.0
    xs = x
    if x.min() <= 0:
        shift = -x.min() + 1.0
        xs = x + shift
    c, loc, scale = fisk.fit(xs, floc=0)
    if not np.isfinite(c) or scale <= 0:
        return None
    return fisk.cdf(xs, c, loc=0, scale=scale)


def _gringorten_cdf(x: np.ndarray) -> np.ndarray:
    """Non-parametric empirical CDF via Gringorten plotting positions."""
    order = np.argsort(np.argsort(x))      # ascending ranks 0..n-1
    ranks = order + 1
    n = x.size
    return (ranks - 0.44) / (n + 0.12)
