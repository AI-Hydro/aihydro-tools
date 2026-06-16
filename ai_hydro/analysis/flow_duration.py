"""
Flow-Duration Curve (FDC), percentile flows, and low-flow design statistics.

Pure-compute kernels over a daily streamflow series — no I/O, no session, no
network. The MCP wrapper in tools_analysis.py supplies the array from
session.streamflow.

A flow-duration curve plots discharge against the fraction of time it is equalled
or exceeded. It is one of the most informative single summaries of a flow regime:
its high-exceedance tail (Q90–Q99) characterises droughts/baseflow, its
low-exceedance tail (Q1–Q10) characterises floods, and its mid-section slope
indicates flashiness (steep = flashy, flat = damped/groundwater-fed).

Conventions
-----------
Percentile flow ``Qxx`` is the discharge **equalled or exceeded xx% of the time**.
So Q95 is a low flow (exceeded 95% of days → small) and Q5 is a high flow
(exceeded only 5% of days → large). This is the standard hydrologic convention
(WMO-168), and is the inverse of a statistical "95th percentile".

The mid-section ``slope_fdc`` matches the existing convention in
``signatures.compute_slope_fdc_camels`` (Sawicz et al., 2011): the slope of the
log-discharge FDC between the 33% and 66% exceedance points.

``7Q10`` is the classic design low flow: the annual minimum 7-day mean discharge
with a 10-year recurrence interval (i.e. a 10% annual non-exceedance probability).
Widely used for water-quality permitting and minimum-flow regulation.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Sequence

import numpy as np

log = logging.getLogger(__name__)

# Standard percentile-flow exceedance points (percent of time exceeded).
_STANDARD_EXCEEDANCE = (5, 10, 25, 50, 75, 90, 95)


def flow_duration_curve(
    q: Sequence[float],
    exceedance_points: Optional[Sequence[float]] = None,
) -> Dict[str, object]:
    """Compute the flow-duration curve and standard percentile flows.

    Parameters
    ----------
    q : sequence of float
        Daily mean discharge (any consistent unit, typically m³/s). NaNs and
        negatives are dropped before computation.
    exceedance_points : sequence of float, optional
        Exceedance percentages to report as percentile flows. Defaults to
        (5, 10, 25, 50, 75, 90, 95).

    Returns
    -------
    dict with keys:
        exceedance_prob   : list[float]  — exceedance probability (0–1) per
                            sorted (descending) flow, via Weibull plotting position
                            rank / (n + 1).
        sorted_flows      : list[float]  — flows sorted descending (the FDC y-axis).
        percentile_flows  : dict[str, float]  — {"Q5": ..., "Q50": ..., "Q95": ...}.
        slope_fdc         : float  — log-FDC slope between 33% and 66% exceedance.
        n_days            : int
    """
    arr = np.asarray(q, dtype=float)
    arr = arr[~np.isnan(arr)]
    arr = arr[arr >= 0]
    n = arr.size
    if n < 2:
        raise ValueError(
            f"flow_duration_curve needs at least 2 valid discharge values, got {n}."
        )

    points = tuple(exceedance_points) if exceedance_points is not None else _STANDARD_EXCEEDANCE

    # FDC y-axis: flows sorted descending; x-axis: Weibull exceedance prob.
    sorted_desc = np.sort(arr)[::-1]
    ranks = np.arange(1, n + 1)
    exceedance = ranks / (n + 1)

    # Percentile flow Qxx = flow exceeded xx% of the time = quantile at (1 - xx/100).
    percentile_flows: Dict[str, float] = {}
    for p in points:
        qval = float(np.quantile(arr, 1.0 - p / 100.0))
        percentile_flows[f"Q{int(p)}"] = round(qval, 6)

    # Mid-section slope (matches signatures.compute_slope_fdc_camels convention).
    slope = _fdc_slope(arr)

    return {
        "exceedance_prob": exceedance.tolist(),
        "sorted_flows": sorted_desc.tolist(),
        "percentile_flows": percentile_flows,
        "slope_fdc": slope,
        "n_days": int(n),
    }


def _fdc_slope(arr: np.ndarray) -> float:
    """Log-FDC slope between 33% and 66% exceedance (Sawicz et al., 2011)."""
    pos = arr[arr > 0]
    if pos.size < 100:
        return float("nan")
    q33 = np.quantile(pos, 0.67)   # 33% exceedance
    q66 = np.quantile(pos, 0.34)   # 66% exceedance
    if q33 <= 0 or q66 <= 0:
        return float("nan")
    return float((np.log(q33) - np.log(q66)) / (0.66 - 0.33))


def seven_q10(
    q: Sequence[float],
    dates: Optional[Sequence] = None,
    min_years: int = 10,
) -> Dict[str, object]:
    """Compute the 7Q10 design low flow (annual 7-day min, 10-yr recurrence).

    Parameters
    ----------
    q : sequence of float
        Daily mean discharge (m³/s).
    dates : sequence of datetime-like, optional
        One date per discharge value. Required to group into water years. If
        omitted, the series is chunked into consecutive 365-day blocks as a
        fallback (less accurate; a warning is logged).
    min_years : int
        Minimum number of annual minima required to fit (default 10). Below this,
        7Q10 is returned as None with a reason.

    Returns
    -------
    dict with keys:
        seven_q10        : float | None  — the 7-day low flow with 10-yr recurrence.
        n_years          : int
        annual_7day_min  : list[float]
        method           : str
        note             : str | None
    """
    arr = np.asarray(q, dtype=float)
    if arr.size < 7:
        return {"seven_q10": None, "n_years": 0, "annual_7day_min": [],
                "method": "log_pearson3", "note": "Fewer than 7 days of data."}

    # 7-day trailing mean (centred would leak across year boundaries).
    kernel = np.ones(7) / 7.0
    rolling7 = np.convolve(arr, kernel, mode="valid")   # length n-6
    # Align rolling window end-date with the 7th day of each window.
    aligned_dates = None
    if dates is not None:
        import pandas as pd
        d = pd.to_datetime(pd.Series(list(dates)))
        aligned_dates = d.iloc[6:].reset_index(drop=True)

    # Group into years → annual 7-day minima.
    if aligned_dates is not None:
        import pandas as pd
        df = pd.DataFrame({"r7": rolling7, "year": aligned_dates.dt.year.values})
        annual_min = df.groupby("year")["r7"].min().values
    else:
        log.warning("seven_q10: no dates supplied — chunking into 365-day blocks.")
        n_full = rolling7.size // 365
        if n_full == 0:
            return {"seven_q10": None, "n_years": 0, "annual_7day_min": [],
                    "method": "log_pearson3", "note": "Less than one year of data."}
        annual_min = np.array([
            rolling7[i * 365:(i + 1) * 365].min() for i in range(n_full)
        ])

    annual_min = annual_min[~np.isnan(annual_min)]
    n_years = annual_min.size
    if n_years < min_years:
        return {
            "seven_q10": None, "n_years": int(n_years),
            "annual_7day_min": [round(float(x), 6) for x in annual_min],
            "method": "log_pearson3",
            "note": f"Need ≥{min_years} years of annual minima for 7Q10; have {n_years}.",
        }

    val = _log_pearson3_low(annual_min, non_exceed_prob=0.10)
    return {
        "seven_q10": round(float(val), 6) if val is not None else None,
        "n_years": int(n_years),
        "annual_7day_min": [round(float(x), 6) for x in annual_min],
        "method": "log_pearson3",
        "note": None,
    }


def _log_pearson3_low(annual_min: np.ndarray, non_exceed_prob: float = 0.10) -> Optional[float]:
    """Log-Pearson III estimate of the low flow at a given non-exceedance prob.

    Fits a Pearson-III distribution to log10(annual minima) and returns the flow
    whose annual non-exceedance probability is ``non_exceed_prob`` (0.10 → 10-yr
    low-flow recurrence). Falls back to a Weibull plotting-position empirical
    quantile if the series has zeros (log undefined) or the fit is degenerate.
    """
    pos = annual_min[annual_min > 0]
    if pos.size < 2 or pos.size < annual_min.size:
        # Zeros present or too few positives → empirical Weibull quantile.
        srt = np.sort(annual_min)
        m = srt.size
        pp = np.arange(1, m + 1) / (m + 1)            # non-exceedance plotting pos
        return float(np.interp(non_exceed_prob, pp, srt))
    try:
        from scipy.stats import pearson3
        logs = np.log10(pos)
        log_std = logs.std(ddof=1)
        # Degenerate (near-)constant series: variance ~0 → skew undefined and the
        # distribution collapses to a point. The low flow is just that constant.
        if log_std < 1e-9:
            return float(10.0 ** logs.mean())
        skew = float(((logs - logs.mean()) ** 3).mean() / (log_std ** 3))
        # pearson3 is parameterised by skew; loc/scale from mean/std of logs.
        q_log = pearson3.ppf(non_exceed_prob, skew, loc=logs.mean(), scale=log_std)
        if not np.isfinite(q_log):
            raise ValueError("non-finite pearson3 quantile")
        return float(10.0 ** q_log)
    except Exception as exc:
        log.warning("Log-Pearson III fit failed (%s); using empirical quantile.", exc)
        srt = np.sort(pos)
        m = srt.size
        pp = np.arange(1, m + 1) / (m + 1)
        return float(np.interp(non_exceed_prob, pp, srt))
