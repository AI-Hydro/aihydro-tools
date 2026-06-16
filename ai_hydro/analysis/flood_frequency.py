"""
Flood Frequency Analysis (FFA) — return periods / design floods.

Pure-compute kernels over an annual-maxima series — no I/O, no session, no
network. The MCP wrapper in tools_analysis.py extracts the daily series from
session.streamflow, derives annual maxima, and calls these functions.

Flood frequency analysis fits a probability distribution to the annual maximum
discharge series and inverts it to estimate the flood magnitude associated with
a given return period T (e.g. the 100-year flood, Q100). It is the single most
common engineering-hydrology output — used for design floods, culvert/levee
sizing, and floodplain mapping.

Distributions supported
-----------------------
- ``gumbel``  — Gumbel / EV1 (extreme value type I). The classic default for
                annual maximum floods. Fitted by MLE (default) or method of
                moments (closed form, textbook-reproducible).
- ``gev``     — Generalised Extreme Value (EV1/EV2/EV3 unified). Fitted by MLE.
- ``lp3``     — Log-Pearson Type III. The US federal standard (Bulletin 17B/17C):
                a Pearson-III distribution fitted to log10 of the annual maxima
                by method of moments (mean, std, skew of the logs).

Return period convention
------------------------
For an annual maximum series, the T-year flood has annual **exceedance**
probability ``p = 1/T``, i.e. **non-exceedance** probability ``1 - 1/T``. The
return level is the distribution quantile at ``1 - 1/T``.

The default distribution is **Gumbel** (most common for AM floods); callers may
override via ``dist=``. Confidence intervals on the return levels are estimated
by non-parametric bootstrap resampling of the annual-maxima series.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

import numpy as np

log = logging.getLogger(__name__)

_EULER_GAMMA = 0.5772156649015329
_DEFAULT_RETURN_PERIODS = (2, 5, 10, 25, 50, 100, 200, 500)


def annual_maxima(
    q: Sequence[float],
    dates: Optional[Sequence] = None,
) -> Dict[str, object]:
    """Extract the annual maximum discharge series.

    Parameters
    ----------
    q : sequence of float
        Daily mean discharge (m³/s).
    dates : sequence of datetime-like, optional
        One date per discharge value. Required to group into calendar years; if
        omitted, the series is chunked into consecutive 365-day blocks (a warning
        is logged).

    Returns
    -------
    dict with keys: annual_max (list[float]), years (list[int] | None),
                    n_years (int).
    """
    arr = np.asarray(q, dtype=float)
    if dates is not None:
        import pandas as pd
        d = pd.to_datetime(pd.Series(list(dates)))
        df = pd.DataFrame({"q": arr, "year": d.dt.year.values}).dropna(subset=["q"])
        grp = df.groupby("year")["q"].max()
        return {"annual_max": [float(x) for x in grp.values],
                "years": [int(y) for y in grp.index.values],
                "n_years": int(grp.size)}
    log.warning("annual_maxima: no dates supplied — chunking into 365-day blocks.")
    arr = arr[~np.isnan(arr)]
    n_full = arr.size // 365
    maxima = [float(arr[i * 365:(i + 1) * 365].max()) for i in range(n_full)]
    return {"annual_max": maxima, "years": None, "n_years": len(maxima)}


def flood_frequency(
    annual_max: Sequence[float],
    dist: str = "gumbel",
    return_periods: Optional[Sequence[float]] = None,
    method: str = "mle",
    n_bootstrap: int = 500,
    ci: float = 0.90,
    random_state: int = 0,
) -> Dict[str, object]:
    """Fit a distribution to annual maxima and estimate return levels.

    Parameters
    ----------
    annual_max : sequence of float
        Annual maximum discharge series (one value per year).
    dist : str
        'gumbel' (default) | 'gev' | 'lp3'.
    return_periods : sequence of float, optional
        Return periods (years). Default (2,5,10,25,50,100,200,500).
    method : str
        'mle' (default; ignored for lp3 which is always method-of-moments on
        logs) | 'mom' (method of moments; implemented for gumbel and lp3).
    n_bootstrap : int
        Bootstrap resamples for CIs (0 disables CIs).
    ci : float
        Confidence level for the interval (default 0.90 → 5th/95th percentiles).

    Returns
    -------
    dict with keys: dist, method, params, return_levels (list of
        {return_period, exceedance_prob, value, ci_low, ci_high}),
        plotting_positions, n_years.
    """
    am = np.asarray(annual_max, dtype=float)
    am = am[~np.isnan(am)]
    n = am.size
    if n < 5:
        raise ValueError(
            f"flood_frequency needs at least 5 annual maxima, got {n}. "
            "Supply ≥10 years of daily data for a meaningful fit."
        )
    if n < 10:
        log.warning("flood_frequency: only %d years — return levels for long T "
                    "(≥100 yr) are extrapolations with wide uncertainty.", n)

    periods = tuple(return_periods) if return_periods is not None else _DEFAULT_RETURN_PERIODS
    dist = dist.lower()

    params = _fit(am, dist, method)
    values = {T: _return_level(params, dist, T) for T in periods}

    # Bootstrap CIs.
    ci_low: Dict[float, float] = {}
    ci_high: Dict[float, float] = {}
    if n_bootstrap and n_bootstrap > 0:
        rng = np.random.default_rng(random_state)
        boot = {T: [] for T in periods}
        for _ in range(n_bootstrap):
            sample = rng.choice(am, size=n, replace=True)
            try:
                p = _fit(sample, dist, method)
                for T in periods:
                    boot[T].append(_return_level(p, dist, T))
            except Exception:
                continue
        lo_q = (1.0 - ci) / 2.0
        hi_q = 1.0 - lo_q
        for T in periods:
            if boot[T]:
                ci_low[T] = float(np.quantile(boot[T], lo_q))
                ci_high[T] = float(np.quantile(boot[T], hi_q))

    return_levels: List[dict] = []
    for T in periods:
        return_levels.append({
            "return_period": float(T),
            "exceedance_prob": round(1.0 / T, 6),
            "value": round(float(values[T]), 4),
            "ci_low": round(ci_low[T], 4) if T in ci_low else None,
            "ci_high": round(ci_high[T], 4) if T in ci_high else None,
        })

    return {
        "dist": dist,
        "method": "mom" if dist == "lp3" else method,
        "params": {k: round(float(v), 6) for k, v in params.items()},
        "return_levels": return_levels,
        "plotting_positions": _plotting_positions(am),
        "n_years": int(n),
        "ci_level": ci,
    }


# ── Fitting ────────────────────────────────────────────────────────────────

def _fit(am: np.ndarray, dist: str, method: str) -> Dict[str, float]:
    """Fit a distribution, returning a param dict keyed by name."""
    if dist == "gumbel":
        if method == "mom":
            std = am.std(ddof=1)
            scale = std * np.sqrt(6.0) / np.pi
            loc = am.mean() - _EULER_GAMMA * scale
            return {"loc": loc, "scale": scale}
        from scipy.stats import gumbel_r
        loc, scale = gumbel_r.fit(am)
        return {"loc": loc, "scale": scale}
    if dist == "gev":
        from scipy.stats import genextreme
        c, loc, scale = genextreme.fit(am)
        return {"c": c, "loc": loc, "scale": scale}
    if dist == "lp3":
        # Log-Pearson III: Pearson-III on log10(am), method of moments (Bulletin 17).
        pos = am[am > 0]
        if pos.size < 5:
            raise ValueError("Log-Pearson III needs ≥5 positive annual maxima.")
        logs = np.log10(pos)
        mean = float(logs.mean())
        std = float(logs.std(ddof=1))
        skew = float(((logs - mean) ** 3).mean() / (std ** 3)) if std > 1e-12 else 0.0
        return {"log_mean": mean, "log_std": std, "log_skew": skew}
    raise ValueError(f"Unknown distribution '{dist}'. Use gumbel | gev | lp3.")


def _return_level(params: Dict[str, float], dist: str, T: float) -> float:
    """Quantile at non-exceedance probability 1 - 1/T."""
    p = 1.0 - 1.0 / T
    if dist == "gumbel":
        # Closed form: loc + scale * (-ln(-ln(p))).
        return params["loc"] + params["scale"] * (-np.log(-np.log(p)))
    if dist == "gev":
        from scipy.stats import genextreme
        return float(genextreme.ppf(p, params["c"], loc=params["loc"], scale=params["scale"]))
    if dist == "lp3":
        from scipy.stats import pearson3
        std = params["log_std"]
        if std < 1e-12:
            return float(10.0 ** params["log_mean"])
        q_log = pearson3.ppf(p, params["log_skew"], loc=params["log_mean"], scale=std)
        return float(10.0 ** q_log)
    raise ValueError(f"Unknown distribution '{dist}'.")


def _plotting_positions(am: np.ndarray, kind: str = "weibull") -> List[dict]:
    """Empirical return periods via plotting positions (for overlaying on the fit)."""
    srt = np.sort(am)[::-1]               # descending: largest = rank 1
    m = srt.size
    ranks = np.arange(1, m + 1)
    if kind == "gringorten":
        exc = (ranks - 0.44) / (m + 0.12)
    else:  # weibull
        exc = ranks / (m + 1)
    return [
        {"value": round(float(v), 4),
         "exceedance_prob": round(float(e), 6),
         "return_period": round(float(1.0 / e), 3)}
        for v, e in zip(srt, exc)
    ]
