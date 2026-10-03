"""Deterministic operations on retained daily series (summaries, runs, comparison, bootstrap, geometry).

Pure functions: no session, no MCP, no I/O. The MCP tools in
``ai_hydro/mcp/tools_series.py`` load a retained series and call these.

Neutrality contract (docs/vision-2040 P1 design ruling, "Keeping tools only vs
gate clean"): every function here computes what it is asked on what it is
given. None of them judges adequacy, warns, or recommends. The only refusals
are ``SeriesInputError`` for malformed input (an unknown statistic name, a
duplicated calendar date, contradictory arguments).

What is wrapped, not re-implemented:

- block / iid bootstrap: ``aihydro_core.science._bootstrap``
- KGE: ``aihydro_modelling.search.comparison._kge``
- run lengths: ``aihydro_watershed.signatures.signatures._consecutive_event_lengths``
- polygon area: ``aihydro_data.geometry.measures.geodesic_area_km2``

Dates are calendar days (``datetime64[D]``). A series is never re-indexed to a
date range: gaps stay gaps, and every function reports the dates that are
present.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

import numpy as np


class SeriesInputError(ValueError):
    """Malformed input to a series operation (never an adequacy judgement)."""


# --------------------------------------------------------------------------- #
# The series container
# --------------------------------------------------------------------------- #

@dataclass
class DailySeries:
    """Dates actually present (ascending, unique) and their values (NaN = no value)."""

    dates: np.ndarray                      # datetime64[D]
    values: np.ndarray                     # float64
    meta: dict = field(default_factory=dict)   # units, product, variable, ... as recorded

    def __len__(self) -> int:
        return int(self.dates.size)


def make_series(dates: Sequence[Any], values: Sequence[Any], meta: Optional[dict] = None) -> DailySeries:
    """Build a ``DailySeries`` from parallel date strings / values.

    Dates are floored to calendar days and sorted ascending. A repeated calendar
    date is refused (the series would not be daily); nothing is dropped or
    re-indexed. ``None`` / non-numeric values become NaN.
    """
    if len(dates) != len(values):
        raise SeriesInputError(
            f"dates and values differ in length ({len(dates)} vs {len(values)})")
    try:
        d = np.array([np.datetime64(str(x)[:10], "D") for x in dates], dtype="datetime64[D]")
    except (ValueError, TypeError) as exc:
        raise SeriesInputError(f"unparseable date in series: {exc}") from exc
    v = np.array([_to_float(x) for x in values], dtype=float)
    order = np.argsort(d, kind="stable")
    d, v = d[order], v[order]
    if d.size > 1 and np.any(d[1:] == d[:-1]):
        dup = d[1:][d[1:] == d[:-1]]
        raise SeriesInputError(
            f"series has {dup.size} repeated calendar date(s), e.g. {str(dup[0])}; "
            "these tools operate on one value per calendar day")
    return DailySeries(dates=d, values=v, meta=dict(meta or {}))


def _to_float(x: Any) -> float:
    if x is None or isinstance(x, bool):
        return float("nan")
    try:
        f = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return f if math.isfinite(f) else float("nan")


def jsonable(x: Any) -> Any:
    """NaN / inf -> None, numpy scalars -> Python, so results seal and serialise."""
    if isinstance(x, (np.floating, float)):
        f = float(x)
        return f if math.isfinite(f) else None
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    if isinstance(x, np.datetime64):
        return str(x)
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    return x


def parse_day(text: Optional[str], label: str) -> Optional[np.datetime64]:
    if text is None or text == "":
        return None
    try:
        return np.datetime64(str(text)[:10], "D")
    except (ValueError, TypeError) as exc:
        raise SeriesInputError(f"{label} is not an ISO date: {text!r}") from exc


def window(series: DailySeries, start: Optional[str], end: Optional[str]) -> tuple[DailySeries, Optional[np.datetime64], Optional[np.datetime64]]:
    """Restrict to ``start <= date <= end`` (inclusive). Dates are not added."""
    s, e = parse_day(start, "start"), parse_day(end, "end")
    if s is not None and e is not None and e < s:
        raise SeriesInputError(f"end {end} is before start {start}")
    keep = np.ones(len(series), dtype=bool)
    if s is not None:
        keep &= series.dates >= s
    if e is not None:
        keep &= series.dates <= e
    return DailySeries(series.dates[keep], series.values[keep], dict(series.meta)), s, e


# --------------------------------------------------------------------------- #
# Calendar block
# --------------------------------------------------------------------------- #

#: A listed ``missing_dates`` array is a convenience; ``missing_ranges`` is always complete.
MAX_LISTED_MISSING_DATES = 400


def calendar_block(series: DailySeries, start: Optional[np.datetime64] = None,
                   end: Optional[np.datetime64] = None) -> dict:
    """Expected vs present calendar days. Reports; never fills or judges.

    The expected span is ``start..end`` when given, else the first to the last
    date present. Dates present with no value are still *present* (see
    ``n_finite`` in the summary).
    """
    if len(series) == 0 and (start is None or end is None):
        return {"expected_days": 0, "present_days": 0, "missing_days": 0,
                "span_start": None, "span_end": None,
                "missing_ranges": [], "missing_dates": [], "missing_dates_listed": True}
    lo = start if start is not None else series.dates[0]
    hi = end if end is not None else series.dates[-1]
    expected = int((hi - lo).astype(int)) + 1 if hi >= lo else 0
    all_days = np.arange(lo, hi + np.timedelta64(1, "D"), dtype="datetime64[D]") if expected else \
        np.array([], dtype="datetime64[D]")
    present_mask = np.isin(all_days, series.dates)
    missing = all_days[~present_mask]
    ranges = _ranges(missing)
    listed = missing.size <= MAX_LISTED_MISSING_DATES
    return {
        "expected_days": expected,
        "present_days": int(present_mask.sum()),
        "missing_days": int(missing.size),
        "span_start": str(lo),
        "span_end": str(hi),
        "missing_ranges": ranges,
        "missing_dates": [str(m) for m in missing] if listed else None,
        "missing_dates_listed": bool(listed),
    }


def _ranges(days: np.ndarray) -> list[dict]:
    out: list[dict] = []
    if days.size == 0:
        return out
    start = prev = days[0]
    for d in days[1:]:
        if d - prev == np.timedelta64(1, "D"):
            prev = d
            continue
        out.append({"start": str(start), "end": str(prev), "days": int((prev - start).astype(int)) + 1})
        start = prev = d
    out.append({"start": str(start), "end": str(prev), "days": int((prev - start).astype(int)) + 1})
    return out


# --------------------------------------------------------------------------- #
# Named statistics (shared by summaries and the bootstrap)
# --------------------------------------------------------------------------- #

_P_STAT = re.compile(r"^p(\d+(?:\.\d+)?)$")
STATISTIC_NAMES = ("mean", "median", "std", "min", "max", "sum", "pNN")


def statistic_fn(name: str) -> Callable[[np.ndarray], float]:
    """Function of a finite 1-D array for a statistic name.

    ``std`` is the sample sd (ddof=1). ``pNN`` is the NN-th percentile
    (non-exceedance probability NN/100, linear interpolation).
    """
    if not isinstance(name, str) or not name:
        raise SeriesInputError("statistic must be a non-empty string")
    n = name.strip().lower()
    fixed: dict[str, Callable[[np.ndarray], float]] = {
        "mean": lambda a: float(np.mean(a)),
        "median": lambda a: float(np.median(a)),
        "std": lambda a: float(np.std(a, ddof=1)) if a.size > 1 else float("nan"),
        "min": lambda a: float(np.min(a)),
        "max": lambda a: float(np.max(a)),
        "sum": lambda a: float(np.sum(a)),
    }
    if n in fixed:
        return fixed[n]
    m = _P_STAT.match(n)
    if m:
        p = float(m.group(1))
        if not 0.0 <= p <= 100.0:
            raise SeriesInputError(f"percentile statistic {name!r} must be between p0 and p100")
        return lambda a, p=p: float(np.quantile(a, p / 100.0))
    raise SeriesInputError(f"unknown statistic {name!r}; supported: {', '.join(STATISTIC_NAMES)}")


QUANTILE_METHOD = "linear (numpy default; Hyndman-Fan type 7)"
DEFAULT_QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


def _finite(series: DailySeries) -> np.ndarray:
    return series.values[np.isfinite(series.values)]


def quantile_key(p: float) -> str:
    return "quantile_p" + f"{p * 100:g}".replace(".", "_")


def summarize(series: DailySeries, quantiles: Optional[Iterable[float]] = None,
              start: Optional[np.datetime64] = None, end: Optional[np.datetime64] = None) -> dict:
    """Descriptive statistics of the values present, plus the calendar block."""
    qs = list(DEFAULT_QUANTILES if quantiles is None else quantiles)
    for q in qs:
        if not isinstance(q, (int, float)) or isinstance(q, bool) or not 0.0 <= float(q) <= 1.0:
            raise SeriesInputError(f"quantile probabilities must be numbers in [0, 1], got {q!r}")
    fin = _finite(series)
    cal = calendar_block(series, start, end)
    out: dict[str, Any] = {
        "values_available": True,
        "n": int(len(series)),
        "n_finite": int(fin.size),
        "mean": float(np.mean(fin)) if fin.size else None,
        "min": float(np.min(fin)) if fin.size else None,
        "max": float(np.max(fin)) if fin.size else None,
        "median": float(np.median(fin)) if fin.size else None,
        "quantile_method": QUANTILE_METHOD,
        "quantile_convention": "probability is non-exceedance (p=0.05 is the 5th percentile of the values)",
        "quantiles": [],
        "first_date": str(series.dates[0]) if len(series) else None,
        "last_date": str(series.dates[-1]) if len(series) else None,
        "expected_calendar_days": cal["expected_days"],
        "n_present_dates": cal["present_days"],
        "n_missing_dates": cal["missing_days"],
        "missing_ranges": cal["missing_ranges"],
        "missing_dates": cal["missing_dates"],
        "missing_dates_listed": cal["missing_dates_listed"],
    }
    for q in qs:
        v = float(np.quantile(fin, float(q))) if fin.size else None
        out["quantiles"].append({"probability": float(q), "value": v})
        out[quantile_key(float(q))] = v
    return jsonable(out)


# --------------------------------------------------------------------------- #
# Threshold runs
# --------------------------------------------------------------------------- #

COMPARISONS = {
    "gt": lambda a, t: a > t,
    "ge": lambda a, t: a >= t,
    "lt": lambda a, t: a < t,
    "le": lambda a, t: a <= t,
}
GAP_POLICIES = ("break", "skip")


def detect_runs(series: DailySeries, *, threshold: Optional[float] = None,
                threshold_relative: Optional[dict] = None, comparison: str = "gt",
                gap_policy: str = "break") -> dict:
    """Maximal runs of consecutive observations whose value passes the comparison.

    Operates on the dates actually present. ``gap_policy``:

    - ``"break"``: a run ends at a missing calendar date or a date with no value.
    - ``"skip"``: observations with no value are set aside and calendar gaps are
      ignored, so the remaining observations are treated as consecutive. This is
      what dropping missing values before counting does; it is offered only as an
      explicit choice and is echoed in the result.
    """
    from aihydro_watershed.signatures.signatures import _consecutive_event_lengths

    if comparison not in COMPARISONS:
        raise SeriesInputError(f"comparison must be one of {sorted(COMPARISONS)}, got {comparison!r}")
    if gap_policy not in GAP_POLICIES:
        raise SeriesInputError(f"gap_policy must be one of {list(GAP_POLICIES)}, got {gap_policy!r}")
    if (threshold is None) == (threshold_relative is None):
        raise SeriesInputError("give exactly one of threshold and threshold_relative")

    fin = _finite(series)
    basis: dict[str, Any]
    if threshold is not None:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
            raise SeriesInputError(f"threshold must be a finite number, got {threshold!r}")
        thr = float(threshold)
        basis = {"kind": "absolute"}
    else:
        if not isinstance(threshold_relative, dict) or set(threshold_relative) != {"stat", "factor"}:
            raise SeriesInputError("threshold_relative must be {'stat': <name>, 'factor': <number>}")
        factor = threshold_relative["factor"]
        if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not math.isfinite(factor):
            raise SeriesInputError(f"threshold_relative.factor must be a finite number, got {factor!r}")
        fn = statistic_fn(str(threshold_relative["stat"]))
        if fin.size == 0:
            raise SeriesInputError("no finite values to compute threshold_relative.stat from")
        stat_value = fn(fin)
        thr = float(factor) * stat_value
        basis = {"kind": "relative", "stat": str(threshold_relative["stat"]).lower(),
                 "stat_value": stat_value, "factor": float(factor),
                 "stat_computed_over": "all finite values of the series as given"}

    dates, vals = series.dates, series.values
    n_nonfinite = int((~np.isfinite(vals)).sum())
    if gap_policy == "skip":
        keep = np.isfinite(vals)
        dates, vals = dates[keep], vals[keep]

    with np.errstate(invalid="ignore"):
        hit = COMPARISONS[comparison](vals, thr) & np.isfinite(vals)

    # A run may not span a missing calendar date ("break"). Insert a False
    # separator after each observation whose successor is not the next day, and
    # let the shared helper measure the runs of the separated mask.
    day = np.timedelta64(1, "D")
    if gap_policy == "break" and dates.size > 1:
        gap_after = (dates[1:] - dates[:-1]) != day
    else:
        gap_after = np.zeros(max(dates.size - 1, 0), dtype=bool)
    sep_mask: list[bool] = []
    index_of: list[int] = []            # observation index per mask slot, -1 for a separator
    for i in range(dates.size):
        sep_mask.append(bool(hit[i]))
        index_of.append(i)
        if i < gap_after.size and gap_after[i]:
            sep_mask.append(False)
            index_of.append(-1)
    lengths = _consecutive_event_lengths(np.array(sep_mask, dtype=bool))

    runs: list[dict] = []
    pos = 0
    n_slots = len(sep_mask)
    k = 0
    while pos < n_slots and k < len(lengths):
        if sep_mask[pos]:
            length = lengths[k]
            first, last = index_of[pos], index_of[pos + length - 1]
            runs.append({
                "start": str(dates[first]), "end": str(dates[last]), "length": int(length),
                "calendar_days": int((dates[last] - dates[first]).astype(int)) + 1,
            })
            pos += length
            k += 1
        else:
            pos += 1
    if k != len(lengths):                                      # pragma: no cover - internal consistency
        raise RuntimeError("run bookkeeping disagrees with the shared run-length helper")

    longest = max(runs, key=lambda r: r["length"]) if runs else None
    cal = calendar_block(series)
    return jsonable({
        "threshold_value": thr,
        "threshold_basis": basis,
        "comparison": comparison,
        "gap_policy": gap_policy,
        "gap_policy_meaning": {
            "break": "a run ends at a missing calendar date or a date with no value",
            "skip": "dates with no value are set aside and calendar gaps are ignored",
        }[gap_policy],
        "n_runs": len(runs),
        "longest_run_length": longest["length"] if longest else 0,
        "longest_run_start": longest["start"] if longest else None,
        "longest_run_end": longest["end"] if longest else None,
        "n_observations_passing": int(hit.sum()),
        "n_dates_present": int(len(series)),
        "n_dates_without_value": n_nonfinite,
        "runs": runs,
        "expected_calendar_days": cal["expected_days"],
        "n_present_dates": cal["present_days"],
        "n_missing_dates": cal["missing_days"],
        "missing_ranges": cal["missing_ranges"],
        "missing_dates": cal["missing_dates"],
        "missing_dates_listed": cal["missing_dates_listed"],
        "first_date": str(series.dates[0]) if len(series) else None,
        "last_date": str(series.dates[-1]) if len(series) else None,
    })


# --------------------------------------------------------------------------- #
# Units (only used when the caller names convert_to)
# --------------------------------------------------------------------------- #

# unit -> (dimension, scale to the dimension's base unit, offset in the base unit)
# base = scale * x + offset. Only what a daily hydrology series is normally in.
_UNITS: dict[str, tuple[str, float, float]] = {
    "m3/s": ("discharge", 1.0, 0.0), "m^3/s": ("discharge", 1.0, 0.0), "cms": ("discharge", 1.0, 0.0),
    "cfs": ("discharge", 0.028316846592, 0.0), "ft3/s": ("discharge", 0.028316846592, 0.0),
    "ft^3/s": ("discharge", 0.028316846592, 0.0), "l/s": ("discharge", 0.001, 0.0),
    "mm": ("depth", 1.0, 0.0), "cm": ("depth", 10.0, 0.0), "m": ("depth", 1000.0, 0.0),
    "in": ("depth", 25.4, 0.0), "inch": ("depth", 25.4, 0.0),
    "mm/day": ("depth_rate", 1.0, 0.0), "mm/d": ("depth_rate", 1.0, 0.0),
    "in/day": ("depth_rate", 25.4, 0.0),
    "degc": ("temperature", 1.0, 0.0), "c": ("temperature", 1.0, 0.0),
    "k": ("temperature", 1.0, -273.15), "kelvin": ("temperature", 1.0, -273.15),
    "degf": ("temperature", 5.0 / 9.0, -160.0 / 9.0), "f": ("temperature", 5.0 / 9.0, -160.0 / 9.0),
}


def _unit_key(u: Optional[str]) -> Optional[str]:
    if not (isinstance(u, str) and u.strip()):
        return None
    k = u.strip().lower().replace(" ", "").replace("\u00b3", "3").replace("^", "").replace("**", "")
    return {"m3s-1": "m3/s", "ft3s-1": "ft3/s", "m3/sec": "m3/s", "ft3/sec": "ft3/s"}.get(k, k)


def convert_values(values: np.ndarray, from_units: Optional[str], to_units: str) -> tuple[np.ndarray, dict]:
    """Convert between linear units in the table above, or refuse.

    Called only when the caller passed ``convert_to``; an impossible requested
    conversion is a malformed request, not a scientific judgement.
    """
    fk, tk = _unit_key(from_units), _unit_key(to_units)
    if tk not in _UNITS:
        raise SeriesInputError(f"convert_to {to_units!r} is not a supported unit "
                               f"({', '.join(sorted(set(_UNITS)))})")
    if fk is None:
        raise SeriesInputError("convert_to given but the input records no units")
    if fk not in _UNITS:
        raise SeriesInputError(f"recorded units {from_units!r} are not a supported source unit")
    fdim, fscale, foff = _UNITS[fk]
    tdim, tscale, toff = _UNITS[tk]
    if fdim != tdim:
        raise SeriesInputError(f"cannot convert {from_units!r} ({fdim}) to {to_units!r} ({tdim})")
    out = ((values * fscale + foff) - toff) / tscale
    return out, {"from": from_units, "to": to_units, "dimension": fdim}


# --------------------------------------------------------------------------- #
# Comparison of two series
# --------------------------------------------------------------------------- #

KGE_CONVENTION = (
    "Gupta et al. (2009): KGE = 1 - sqrt((r-1)^2 + (alpha-1)^2 + (beta-1)^2), "
    "alpha = sd_b/sd_a with the population sd (ddof=0), beta = mean_b/mean_a, "
    "series_a is the reference and series_b the compared series"
)
PERCENT_BIAS_CONVENTION = "100 * (mean_b - mean_a) / mean_a over the paired dates (positive: b larger than a)"


def compare(a: DailySeries, b: DailySeries, convert_to: Optional[str] = None) -> dict:
    """Pair two series by calendar date and report agreement statistics.

    No unit conversion unless ``convert_to`` is given. Mismatched recorded units
    are echoed, never refused.
    """
    from aihydro_modelling.search.comparison import _kge

    va, vb = a.values, b.values
    conv_a = conv_b = None
    if convert_to is not None:
        va, conv_a = convert_values(va, a.meta.get("units"), convert_to)
        vb, conv_b = convert_values(vb, b.meta.get("units"), convert_to)

    common, ia, ib = np.intersect1d(a.dates, b.dates, return_indices=True)
    xa, xb = va[ia], vb[ib]
    ok = np.isfinite(xa) & np.isfinite(xb)
    pa, pb = xa[ok], xb[ok]
    n = int(pa.size)

    mean_a = float(np.mean(pa)) if n else None
    mean_b = float(np.mean(pb)) if n else None
    sd_a = float(np.std(pa)) if n else None
    sd_b = float(np.std(pb)) if n else None
    pbias = 100.0 * (mean_b - mean_a) / mean_a if n and mean_a not in (None, 0.0) else None
    r = None
    if n >= 2 and sd_a and sd_b:
        r = float(np.corrcoef(pa, pb)[0, 1])
    sd_ratio = (sd_b / sd_a) if n and sd_a else None
    kge = float(_kge(pa, pb)) if n else None
    beta = (mean_b / mean_a) if n and mean_a else None

    def period(s: DailySeries) -> dict:
        return {"first_date": str(s.dates[0]) if len(s) else None,
                "last_date": str(s.dates[-1]) if len(s) else None,
                "n_dates": int(len(s)), "n_finite": int(np.isfinite(s.values).sum())}

    return jsonable({
        "n_pairs": n,
        "n_common_dates": int(common.size),
        "mean_a": mean_a,
        "mean_b": mean_b,
        "percent_bias": pbias,
        "percent_bias_convention": PERCENT_BIAS_CONVENTION,
        "r": r,
        "sd_a": sd_a,
        "sd_b": sd_b,
        "sd_ratio": sd_ratio,
        "sd_convention": "population sd (ddof=0)",
        "kge": kge,
        "kge_components": {"r": r, "alpha": sd_ratio, "beta": beta},
        "kge_convention": KGE_CONVENTION,
        "pairing": "inner join on calendar date; pairs where either value is missing are excluded",
        "units_a": a.meta.get("units"),
        "units_b": b.meta.get("units"),
        "period_a": period(a),
        "period_b": period(b),
        "paired_period": {"first_date": str(common[ok][0]) if n else None,
                          "last_date": str(common[ok][-1]) if n else None},
        "convert_to": convert_to,
        "conversion_a": conv_a,
        "conversion_b": conv_b,
    })


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #

BOOTSTRAP_METHODS = ("iid", "block")


def bootstrap(series: DailySeries, *, statistic: str, method: str,
              block_length: Optional[int], n_resamples: int, level: float, seed: int) -> dict:
    """Percentile bootstrap CI of a named statistic of the finite values.

    ``method`` is required: ``iid`` resamples observations independently,
    ``block`` resamples moving blocks. NaN values are removed before resampling
    by the wrapped library, so calendar gaps are not preserved in either method.
    """
    from aihydro_core.science._bootstrap import (
        _default_block_size, block_bootstrap_ci, bootstrap_ci,
    )

    if method not in BOOTSTRAP_METHODS:
        raise SeriesInputError(f"method must be one of {list(BOOTSTRAP_METHODS)} (no default), got {method!r}")
    fn = statistic_fn(statistic)
    if isinstance(n_resamples, bool) or not isinstance(n_resamples, int) or n_resamples < 1:
        raise SeriesInputError(f"n_resamples must be a positive integer, got {n_resamples!r}")
    if isinstance(level, bool) or not isinstance(level, (int, float)) or not 0.0 < float(level) < 1.0:
        raise SeriesInputError(f"level must be a number strictly between 0 and 1, got {level!r}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise SeriesInputError(f"seed must be an integer, got {seed!r}")
    if method == "iid" and block_length is not None:
        raise SeriesInputError("block_length applies only to method='block'")
    if block_length is not None and (isinstance(block_length, bool) or not isinstance(block_length, int)
                                     or block_length < 1):
        raise SeriesInputError(f"block_length must be a positive integer, got {block_length!r}")

    fin = _finite(series)
    if fin.size == 0:
        raise SeriesInputError("series has no finite values")
    try:
        if method == "iid":
            res = bootstrap_ci(fn, fin, n=n_resamples, ci=float(level), random_state=seed)
            used_block, block_source = None, None
        else:
            used_block = block_length if block_length is not None else _default_block_size(int(fin.size))
            used_block = int(max(1, min(used_block, fin.size)))
            block_source = "caller" if block_length is not None else "library default max(5, round(n^(1/3)))"
            res = block_bootstrap_ci(fn, fin, block_size=used_block, n=n_resamples,
                                     ci=float(level), random_state=seed)
    except ValueError as exc:                       # the library's own minimum-length refusal
        raise SeriesInputError(str(exc)) from exc

    est = float(res["value"])
    lo, hi = float(res["ci_low"]), float(res["ci_high"])
    return jsonable({
        "statistic": statistic.strip().lower(),
        "estimate": est,
        "ci_low": lo,
        "ci_high": hi,
        "level": float(level),
        "method": "bootstrap_iid" if method == "iid" else "bootstrap_block",
        "resampling": method,
        "n_resamples": int(n_resamples),
        "block_length": used_block,
        "block_length_source": block_source,
        "seed": int(seed),
        "n_finite": int(fin.size),
        "n_records": int(len(series)),
        "interval": "percentile",
        "resampling_unit": "finite observations; missing values removed before resampling, "
                           "calendar gaps not preserved",
        "_uncertainty": {"estimate": {
            "value": est, "ci_low": lo, "ci_high": hi, "ci_level": float(level),
            "method": "bootstrap_iid" if method == "iid" else "bootstrap_block",
            "n": int(fin.size),
        }},
    })


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

MAX_GEOJSON_VERTICES = 5_000_000
MAX_GEOJSON_DEPTH = 12


def _geojson_size_problem(geom: Any) -> Optional[str]:
    """A plain description if a GeoJSON geometry is too deep or too large, else None.

    Iterative (no recursion), so a hostile nesting depth cannot raise RecursionError.
    """
    if not isinstance(geom, dict):
        return "geometry must be a GeoJSON object"
    stack = [(geom.get("coordinates", geom.get("geometries")), 1)]
    vertices = 0
    while stack:
        node, depth = stack.pop()
        if depth > MAX_GEOJSON_DEPTH:
            return f"geometry coordinates nest deeper than {MAX_GEOJSON_DEPTH} levels"
        if isinstance(node, dict):
            stack.append((node.get("coordinates", node.get("geometries")), depth + 1))
        elif isinstance(node, (list, tuple)):
            if node and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in node):
                vertices += 1
                if vertices > MAX_GEOJSON_VERTICES:
                    return f"geometry has more than {MAX_GEOJSON_VERTICES} vertices"
            else:
                stack.extend((v, depth + 1) for v in node)
    return None


def measure_geometry(geometry_dict: dict) -> dict:
    """Geodesic area and perimeter (WGS84 ellipsoid) of a GeoJSON geometry.

    Coordinates are read as lon/lat degrees. Area is the wrapped
    ``geodesic_area_km2``; the perimeter comes from the same pyproj ``Geod``
    object (the lower-layer helper returns area only).
    """
    from shapely.geometry import shape

    err = _geojson_size_problem(geometry_dict)
    if err:
        raise SeriesInputError(err)
    try:
        geom = shape(geometry_dict)
    except Exception as exc:
        raise SeriesInputError(f"not a valid GeoJSON geometry: {exc}") from exc
    gtype = geom.geom_type
    out: dict[str, Any] = {
        "geometry_type": gtype,
        "area_km2": None,
        "perimeter_km": None,
        "length_km": None,
        "method": ("geodesic on the WGS84 ellipsoid (pyproj.Geod.geometry_area_perimeter; area via "
                   "aihydro_data.geometry.measures.geodesic_area_km2); coordinates read as lon/lat degrees"),
        "crs_assumed": "EPSG:4326",
        "n_exterior_vertices": None,
    }
    try:
        from pyproj import Geod
        geod = Geod(ellps="WGS84")
    except Exception as exc:
        raise SeriesInputError(f"pyproj is required to measure geometry: {exc}") from exc
    if gtype in ("Polygon", "MultiPolygon"):
        from aihydro_data.geometry.measures import geodesic_area_km2

        area = geodesic_area_km2(geom)
        _, perim_m = geod.geometry_area_perimeter(geom)
        out["area_km2"] = area
        out["perimeter_km"] = abs(perim_m) / 1000.0
        polys = [geom] if gtype == "Polygon" else list(geom.geoms)
        out["n_exterior_vertices"] = int(sum(len(p.exterior.coords) for p in polys))
    elif gtype in ("LineString", "MultiLineString"):
        out["length_km"] = float(geod.geometry_length(geom)) / 1000.0
    return jsonable(out)
