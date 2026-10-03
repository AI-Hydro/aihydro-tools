"""
Deterministic series and geometry tools (platform analysis kernel).

Five general tools, identical in every evaluation arm:

    summarize_series       descriptive statistics + calendar block of a retained series
    detect_threshold_runs  runs of consecutive dates above/below a threshold
    compare_series         paired agreement statistics of two retained series
    bootstrap_statistic    percentile bootstrap CI of a named statistic
    measure_feature        geodesic area and perimeter of a registered geometry

Each ``series`` argument is the ``run_id`` of a prior run that retained a series
(``data_fetch``, ``fetch_streamflow_data``). The producer is declared as a
parent of the call, so lineage and input digests are sealed in the new record,
and every success returns ``_run_id``.

Neutrality contract (P1 design ruling): these tools compute what is asked on
what is given. They echo recorded metadata in neutral fields and emit no
warnings, recommendations, ``next_steps`` or adequacy judgements. They refuse
only malformed input, with a plain ``{"error": true, "code", "message"}``.
The maths lives in ``ai_hydro/analysis/series_ops.py``, which wraps the existing
bootstrap, KGE, run-length and geodesic helpers.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.enforcement import post_run as _post_run
from ai_hydro.mcp.helpers import _resolve_session

log = logging.getLogger("ai_hydro.mcp.series")


def _plain_error(code: str, message: str) -> dict:
    """A plain input error: no recovery text, no suggested tools."""
    return {"error": True, "code": code, "message": message}


def _loaded(session_id: str | None, run_id: str):
    """``(session_id, DailySeries | None, RunSeries)`` for a series run id."""
    from ai_hydro.analysis.series_ops import make_series
    from ai_hydro.session.series import load_run_series

    sid = _resolve_session(session_id, None, allow_auto_create=False)
    rs = load_run_series(sid, run_id)
    if not rs.values_available:
        return sid, None, rs
    return sid, make_series(rs.dates, rs.values, rs.meta), rs


def _source_echo(rs, prefix: str = "") -> dict:
    """Recorded facts about the input run, in neutral fields."""
    return {
        f"{prefix}series_run_id": rs.run_id,
        f"{prefix}series_source_tool": rs.tool,
        f"{prefix}series_file_digest": rs.file_digest,
        f"{prefix}units": rs.meta.get("units"),
        f"{prefix}product": rs.meta.get("product"),
        f"{prefix}variable": rs.meta.get("variable"),
        f"{prefix}timestep": rs.meta.get("timestep"),
        f"{prefix}source": rs.meta.get("source"),
    }


def _seal(tool: str, session_id: str, data: dict, inputs: dict) -> dict:
    result = {"data": data, "meta": {"tool": tool, "params": inputs}}
    return _post_run(tool, session_id, result, inputs=inputs)


def _fail(tool: str, exc: Exception) -> dict:
    from ai_hydro.analysis.series_ops import SeriesInputError
    from ai_hydro.session.series import SeriesLoadError

    if isinstance(exc, SeriesLoadError):
        return _plain_error(exc.code, str(exc))
    if isinstance(exc, SeriesInputError):
        return _plain_error("INVALID_INPUT", str(exc))
    from ai_hydro.mcp.helpers import _tool_error_to_dict

    log.error("%s failed: %s", tool, exc)
    return _tool_error_to_dict(exc)


# ---------------------------------------------------------------------------
# summarize_series
# ---------------------------------------------------------------------------

@mcp.tool()
def summarize_series(
    series: str,
    start: str | None = None,
    end: str | None = None,
    quantiles: list[float] | None = None,
    session_id: str | None = None,
) -> dict:
    """
    Descriptive statistics of a retained daily series, with its calendar block.

    Reports n (dates present), n_finite, mean, min, max, median, quantiles (the
    method is named in the result), first and last date present, the expected
    calendar days over the span, how many are missing and which, and the units
    and product label exactly as the producing run recorded them. Nothing is
    filled, re-indexed or judged.

    If the run recorded no daily values (an aggregate-only record), the result
    is its recorded statistics with ``values_available: false``.

    Parameters
    ----------
    series : str
        run_id of a prior run that retained a series (e.g. a data_fetch call).
    start, end : str | None
        Inclusive ISO dates restricting the window. The expected calendar span
        is then start..end.
    quantiles : list[float] | None
        Probabilities in [0, 1] (non-exceedance). Default 0.05, 0.25, 0.5, 0.75, 0.95.
    session_id : str | None
        Session holding the run. Auto-resolved when omitted.
    """
    try:
        from ai_hydro.analysis.series_ops import summarize, window

        sid, s, rs = _loaded(session_id, series)
        inputs = {"series": series, "start": start, "end": end}
        if s is None:
            data = {"values_available": False, "recorded_statistics": rs.recorded_statistics,
                    **_source_echo(rs)}
            return _seal("summarize_series", sid, data, inputs)
        windowed, lo, hi = window(s, start, end)
        data = summarize(windowed, quantiles, lo, hi)
        data.update(_source_echo(rs))
        data["window_start"] = start
        data["window_end"] = end
        return _seal("summarize_series", sid, data, inputs)
    except Exception as exc:
        return _fail("summarize_series", exc)


# ---------------------------------------------------------------------------
# detect_threshold_runs
# ---------------------------------------------------------------------------

@mcp.tool()
def detect_threshold_runs(
    series: str,
    threshold: float | None = None,
    threshold_relative: dict | None = None,
    comparison: str = "gt",
    gap_policy: str = "break",
    session_id: str | None = None,
) -> dict:
    """
    Runs of consecutive dates on which a retained series passes a threshold.

    Works on the dates actually present: the series is not re-indexed and gaps
    are not dropped silently. Returns each run (start, end, length in
    observations, calendar days spanned), the count, the longest run, the
    calendar block (expected, present and missing dates) and the gap policy used.

    Give exactly one of ``threshold`` (absolute) or ``threshold_relative``
    (``{"stat": "median", "factor": 9}`` means factor times the stat of the
    finite values of the whole series; stats: mean, median, std, min, max, sum,
    pNN).

    Parameters
    ----------
    series : str
        run_id of a prior run that retained a series.
    threshold : float | None
        Absolute threshold in the series' recorded units.
    threshold_relative : dict | None
        ``{"stat": <name>, "factor": <number>}``.
    comparison : str
        "gt" (default), "ge", "lt" or "le" applied as value <comparison> threshold.
    gap_policy : str
        "break" (default): a run ends at a missing calendar date or a date with no
        value. "skip": dates with no value are set aside and calendar gaps ignored.
    session_id : str | None
        Session holding the run. Auto-resolved when omitted.
    """
    try:
        from ai_hydro.analysis.series_ops import SeriesInputError, detect_runs

        sid, s, rs = _loaded(session_id, series)
        if s is None:
            raise SeriesInputError(f"run {series!r} recorded no daily values (aggregate-only record)")
        data = detect_runs(s, threshold=threshold, threshold_relative=threshold_relative,
                           comparison=comparison, gap_policy=gap_policy)
        data.update(_source_echo(rs))
        inputs = {"series": series, "threshold": threshold, "comparison": comparison,
                  "gap_policy": gap_policy}
        if threshold_relative:
            inputs["threshold_stat"] = str(threshold_relative.get("stat"))
            inputs["threshold_factor"] = threshold_relative.get("factor")
        return _seal("detect_threshold_runs", sid, data, inputs)
    except Exception as exc:
        return _fail("detect_threshold_runs", exc)


# ---------------------------------------------------------------------------
# compare_series
# ---------------------------------------------------------------------------

@mcp.tool()
def compare_series(
    series_a: str,
    series_b: str,
    convert_to: str | None = None,
    session_id: str | None = None,
) -> dict:
    """
    Agreement statistics of two retained series, paired by calendar date.

    Reports the number of pairs, the two means over the pairs, percent bias
    (100 * (mean_b - mean_a) / mean_a), Pearson r, the sd ratio (sd_b / sd_a),
    and KGE (Gupta et al. 2009) with the sd convention named (population sd,
    series_a is the reference). With zero variance in either series (or fewer
    than two pairs), r, sd_ratio and the KGE are None. Also echoes each input's units and period as
    recorded.

    No unit conversion happens unless ``convert_to`` is given. Mismatched
    recorded units are never refused, only echoed. With ``convert_to`` set to a
    supported unit (m3/s, cfs, l/s, mm, cm, m, in, mm/day, degC, K, degF), both
    series are converted from their recorded units to it.

    Parameters
    ----------
    series_a : str
        run_id of the reference series.
    series_b : str
        run_id of the compared series.
    convert_to : str | None
        Target unit for both series; omit for no conversion.
    session_id : str | None
        Session holding the runs. Auto-resolved when omitted.
    """
    try:
        from ai_hydro.analysis.series_ops import SeriesInputError, compare

        sid, a, rsa = _loaded(session_id, series_a)
        _, b, rsb = _loaded(sid, series_b)
        for label, s, run in (("series_a", a, series_a), ("series_b", b, series_b)):
            if s is None:
                raise SeriesInputError(f"{label} ({run!r}) recorded no daily values (aggregate-only record)")
        data = compare(a, b, convert_to)
        data.update(_source_echo(rsa, "a_"))
        data.update(_source_echo(rsb, "b_"))
        inputs = {"series_a": series_a, "series_b": series_b, "convert_to": convert_to}
        return _seal("compare_series", sid, data, inputs)
    except Exception as exc:
        return _fail("compare_series", exc)


# ---------------------------------------------------------------------------
# bootstrap_statistic
# ---------------------------------------------------------------------------

@mcp.tool()
def bootstrap_statistic(
    series: str,
    statistic: str,
    method: str,
    n_resamples: int = 1000,
    level: float = 0.95,
    seed: int = 0,
    block_length: int | None = None,
    session_id: str | None = None,
) -> dict:
    """
    Percentile bootstrap confidence interval of a statistic of a retained series.

    ``method`` has no default: choose "iid" (observations resampled
    independently) or "block" (moving blocks of ``block_length`` observations;
    when omitted the library default max(5, round(n^(1/3))) is used and
    reported). Missing values are removed before resampling by either method.
    Returns the estimate, the bounds, the method, the number of resamples, the
    block length used and the seed. The uncertainty is also stored under
    ``_uncertainty.estimate`` so a claim can cite it.

    A run that recorded no daily values is a plain input error.

    Parameters
    ----------
    series : str
        run_id of a prior run that retained a series.
    statistic : str
        mean, median, std (sample sd), min, max, sum, or pNN (e.g. p95).
    method : str
        "iid" or "block". Required.
    n_resamples : int
        Number of bootstrap resamples (default 1000).
    level : float
        Confidence level in (0, 1) (default 0.95).
    seed : int
        RNG seed (default 0); echoed in the result.
    block_length : int | None
        Block length for method="block".
    session_id : str | None
        Session holding the run. Auto-resolved when omitted.
    """
    try:
        from ai_hydro.analysis.series_ops import SeriesInputError, bootstrap

        sid, s, rs = _loaded(session_id, series)
        if s is None:
            raise SeriesInputError(f"run {series!r} recorded no daily values (aggregate-only record)")
        data = bootstrap(s, statistic=statistic, method=method, block_length=block_length,
                         n_resamples=n_resamples, level=level, seed=seed)
        data.update(_source_echo(rs))
        inputs = {"series": series, "statistic": statistic, "method": method,
                  "n_resamples": n_resamples, "level": level, "seed": seed,
                  "block_length": block_length}
        return _seal("bootstrap_statistic", sid, data, inputs)
    except Exception as exc:
        return _fail("bootstrap_statistic", exc)


# ---------------------------------------------------------------------------
# measure_feature
# ---------------------------------------------------------------------------

def _geometry_of(session_id: str, feature: str) -> tuple[dict, dict]:
    """``(geometry dict, descriptor)`` for a registered feature id/name or inline GeoJSON.

    Reads only; never registers anything (unlike FeatureRegistry.resolve, which
    registers unknown inline GeoJSON on the fly).
    """
    from ai_hydro.analysis.series_ops import SeriesInputError
    from ai_hydro.session import HydroSession

    if not isinstance(feature, str) or not feature.strip():
        raise SeriesInputError("feature must be a registered feature id or name, or a GeoJSON string")
    session = HydroSession.load(session_id)
    feats = session.list_features()
    for f in feats:
        if f.feature_id == feature or (f.name and f.name == feature):
            return f.geometry_dict(), {"feature_id": f.feature_id, "name": f.name,
                                       "source": f.source, "from": "registered_feature"}
    try:
        parsed = json.loads(feature)
    except RecursionError:
        raise SeriesInputError("inline GeoJSON nests too deeply to parse") from None
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        geom = parsed.get("geometry", parsed) if parsed.get("type") == "Feature" else parsed
        return geom, {"feature_id": None, "name": "", "source": "inline_geojson", "from": "inline_geojson"}
    raise SeriesInputError(
        f"feature {feature!r} is not a registered feature id or name in session {session_id!r}, "
        "and is not a GeoJSON string")


@mcp.tool()
def measure_feature(feature: str, session_id: str | None = None) -> dict:
    """
    Geodesic area and perimeter of a registered geometry.

    Measured on the WGS84 ellipsoid (pyproj geodesic; the area uses
    aihydro-data's ``geodesic_area_km2``), with coordinates read as lon/lat
    degrees. The method is named in the result. Does not modify the feature
    registry (``register_feature`` is unchanged and does not fill ``area_km2``;
    this tool works on any registered feature or inline GeoJSON and leaves its
    own sealed record). Polygons and multipolygons give area and perimeter;
    lines give length only; points give neither.

    Parameters
    ----------
    feature : str
        A registered feature id or name, or a GeoJSON geometry/Feature string.
    session_id : str | None
        Session holding the feature registry. Auto-resolved when omitted.
    """
    try:
        from ai_hydro.analysis.series_ops import measure_geometry

        sid = _resolve_session(session_id, None, allow_auto_create=False)
        geom, desc = _geometry_of(sid, feature)
        data = measure_geometry(geom)
        data.update({"feature_id": desc["feature_id"], "feature_name": desc["name"],
                     "feature_source": desc["source"], "feature_resolved_from": desc["from"]})
        inputs = {"feature": feature if len(feature) <= 128 else f"<{len(feature)} chars>"}
        return _seal("measure_feature", sid, data, inputs)
    except Exception as exc:
        return _fail("measure_feature", exc)
