"""
Streamflow Data Retrieval
=========================

Fetch USGS streamflow through the aihydro-data Water Data API adapter
(``aihydro_data.sources.usgs_waterdata``) and provide unit conversion utilities.

The former USGS NWIS web-service path is gone (decommissioned 2027-02-22): there is no
second fetcher in this module. Discharge units are converted
once, inside the adapter, from the unit the API states. Identity and integrity
refusals (``TerminalSourceError``: GAUGE_NOT_FOUND, USGS_NO_DATA, ...) are raised as a
``ToolError`` flagged ``terminal`` and are never answered by another source.

Public Functions
----------------
fetch_streamflow_data(gauge_id, start_date, end_date) -> HydroResult
    Download USGS streamflow as JSON-serializable time series plus per-value
    approval/qualifier codes, a quality summary and upstream provenance

Helpers
-------
qualification_codes    — per-value USGS code strings (A / P / e / raw qualifier tokens)
slot_quality_meta      — the session-slot ``meta`` fields describing data quality
_to_mm_per_day         — unit conversion (m³/s → mm/day)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence
import logging
import warnings

import pandas as pd

from ai_hydro.core import DataSource, HydroMeta, HydroResult, ToolError

_SOURCES_NWIS = [
    DataSource(
        name="USGS Water Data API",
        url="https://api.waterdata.usgs.gov/ogcapi/v1",
        citation=(
            "@misc{USGSWaterDataAPI,\n"
            "  title={USGS Water Data APIs (OGC API - Features)},\n"
            "  author={{U.S. Geological Survey}},\n"
            "  year={2026},\n"
            "  url={https://api.waterdata.usgs.gov/docs/ogcapi/}\n"
            "}"
        ),
    )
]

_TOOL_PATH_STREAMFLOW = "ai_hydro.data.streamflow.fetch_streamflow_data"

log = logging.getLogger(__name__)
warnings.filterwarnings('ignore')


def _get_version() -> str:
    try:
        from importlib.metadata import version
        return version("aihydro-tools")
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Quality helpers (shared by fetch_streamflow_data and the data_fetch slot writer)
# ---------------------------------------------------------------------------

def _tokens(cell: Any) -> list[str]:
    """Split one code cell ("A", "e", "ICE,e", "") into tokens (comma or whitespace)."""
    if cell is None or (isinstance(cell, float) and cell != cell):
        return []
    return [t for t in str(cell).replace(",", " ").split() if t]


def qualification_codes(approval_code: Sequence[Any], qualifier_code: Sequence[Any]) -> list[str]:
    """Per-value USGS code strings: the approval code (A / P) followed by the qualifier
    tokens (``e`` for ESTIMATED; every other qualifier stays as the raw token the API sent).
    Tokens are space separated, one string per value."""
    return [" ".join(_tokens(a) + _tokens(q)) for a, q in zip(approval_code, qualifier_code)]


def slot_quality_meta(
    frame: pd.DataFrame,
    quality: Optional[dict],
    site_resolution: Optional[dict],
    upstream: Optional[dict],
) -> dict:
    """Session-slot ``meta`` fields for a served USGS frame.

    ``frame`` needs ``approval_code`` and ``qualifier_code`` columns (the adapter's).
    ``qualification_codes`` is per value, aligned with the served rows. The summary
    carries approval/qualifier counts, ``qualifiers_other`` (raw counts of every
    qualifier the legacy A/P/e vocabulary does not cover), ``n_negative``,
    ``n_missing`` and ``provisional_fraction`` as the adapter computed them.
    """
    q = dict(quality or {})
    meta: dict = {
        "qualification_codes": qualification_codes(frame["approval_code"], frame["qualifier_code"]),
        "quality_summary": q,
        "qualifiers_other": dict(q.get("qualifiers_other") or {}),
    }
    if site_resolution:
        meta["site_resolution"] = site_resolution
    if upstream:
        meta["upstream"] = upstream
    return meta


def refusal_from(exc: BaseException) -> ToolError:
    """ToolError for an adapter exception, keeping code, recovery and the terminal flag."""
    from aihydro_data.exceptions import TerminalSourceError

    details = dict(getattr(exc, "details", None) or {})
    details["terminal"] = isinstance(exc, TerminalSourceError)
    return ToolError(
        code=getattr(exc, "code", None) or "NETWORK_ERROR",
        message=getattr(exc, "message", None) or str(exc),
        details=details,
        tool=_TOOL_PATH_STREAMFLOW,
        recovery=getattr(exc, "recovery", None) or "Check network connection and verify gauge ID.",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_streamflow_data(
    gauge_id: str,
    start_date: str,
    end_date: str,
    interval: str = "daily",
) -> HydroResult:
    """
    Fetch daily or hourly (instantaneous) streamflow from the USGS Water Data API.

    Parameters
    ----------
    gauge_id : str
        USGS gauge identifier (8-digit code, or ``USGS-`` prefixed)
    start_date : str
        Start date in YYYY-MM-DD format
    end_date : str
        End date in YYYY-MM-DD format
    interval : str, optional
        "daily" (default) or "hourly" (instantaneous values, UTC)

    Returns
    -------
    HydroResult
        result.data keys: dates, q_cms, units, n_days, gauge_id, gauge_name,
        gauge_lat, gauge_lon, start_date, end_date, interval, qualification_codes,
        quality_summary, site_resolution, upstream

    Raises
    ------
    ToolError
        DEPENDENCY_ERROR; the adapter's own codes (details["terminal"] is True for
        identity/integrity refusals: GAUGE_NOT_FOUND, USGS_NO_DATA, USGS_MULTIPLE_SERIES,
        ...; False for transport failures such as USGS_RATE_LIMITED); NO_VALID_DATA
    """
    try:
        from aihydro_data.exceptions import AihydroDataError
        from aihydro_data.sources import usgs_waterdata as uw
    except ImportError as e:
        raise ToolError(
            code="DEPENDENCY_ERROR",
            message=str(e),
            tool=_TOOL_PATH_STREAMFLOW,
            recovery="pip install 'aihydro-tools[data]'",
        ) from e

    try:
        log.info("Fetching USGS streamflow for %s (%s)", gauge_id, interval)
        res = uw.resolve_gauge(gauge_id=gauge_id)
        if interval == "daily":
            ser = uw.get_daily(res.site, start_date, end_date)
        else:
            ser = uw.get_continuous(res.site, start_date, end_date)
    except AihydroDataError as e:
        raise refusal_from(e) from e

    try:
        # Rows whose value the API reports as null stay in the series as None so dates,
        # values and per-value codes stay aligned (n_missing / n_null_values count them).
        df = ser.frame.copy().reset_index(drop=True)
        n_null = int(df["value_native"].isna().sum())
        if n_null == len(df):
            raise ToolError(
                code="NO_VALID_DATA",
                message=f"No valid streamflow values for gauge {gauge_id} ({start_date} to {end_date}).",
                tool=_TOOL_PATH_STREAMFLOW,
                recovery="Try a different date range or check gauge status.",
            )
        times = pd.DatetimeIndex(df["time"])
        if times.tz is not None:                 # continuous: UTC instants -> naive UTC
            times = times.tz_convert("UTC").tz_localize(None)

        summary = uw.summarize_frame(df, null_dropped=n_null)
        summary.update({k: v for k, v in ser.quality_summary.items()
                        if k in ("n_expected", "n_missing", "median_step_s", "max_gap_s", "n_gaps_gt_2x_median")})
        summary.update({"units_native": ser.units_native, "time_series_id": ser.time_series_id,
                        "site_id": ser.site_id, "backend": "usgs_waterdata"})
        site_resolution = res.to_dict()
        qmeta = slot_quality_meta(df, summary, site_resolution, dict(ser.upstream))
        loc = res.location

        data = {
            "dates": [t.isoformat() for t in times.to_pydatetime()],
            "q_cms": [None if v != v else float(v) for v in df["value_m3s"].to_numpy()],
            "units": "m3/s",
            "n_days": len(df),
            "gauge_id": gauge_id,
            "gauge_name": (loc.name or "") if loc else "",
            "gauge_lat": loc.lat if loc else None,
            "gauge_lon": loc.lon if loc else None,
            "start_date": start_date,
            "end_date": end_date,
            "interval": interval,
            "source_units": ser.units_native,
            "_aihydro_data_product": "NWIS_STREAMFLOW",
            "_aihydro_data_source": "direct_api",
            "qualification_codes": qmeta["qualification_codes"],
            "quality_summary": qmeta["quality_summary"],
            "site_resolution": site_resolution,
            "upstream": qmeta["upstream"],
        }
        log.info("Retrieved %d streamflow points for %s", len(df), gauge_id)
        return HydroResult(
            data=data,
            meta=HydroMeta(
                tool=_TOOL_PATH_STREAMFLOW,
                version=_get_version(),
                gauge_id=gauge_id,
                sources=_SOURCES_NWIS,
                params={"gauge_id": gauge_id, "start_date": start_date,
                        "end_date": end_date, "interval": interval},
            ),
        )
    except ToolError:
        raise
    except Exception as e:
        log.error("fetch_streamflow_data failed: %s", e)
        raise ToolError(
            code="NETWORK_ERROR",
            message=f"Failed to process streamflow for gauge {gauge_id}: {e}",
            tool=_TOOL_PATH_STREAMFLOW,
            recovery="Check the gauge ID and date range.",
        ) from e


# ---------------------------------------------------------------------------
# Internal helpers (used by analysis.signatures)
# ---------------------------------------------------------------------------

def _to_mm_per_day(discharge_cms: pd.Series, area_km2: float) -> pd.Series:
    """
    Convert discharge from m³/s to basin-depth mm/day.

    Formula: q_mm_day = q_cms * 86.4 / area_km2
    """
    if discharge_cms is None or len(discharge_cms) == 0 or area_km2 <= 0:
        return pd.Series(dtype=float)

    q_mm_day = discharge_cms * (86.4 / area_km2)
    q_mm_day.index = pd.to_datetime(q_mm_day.index).tz_localize(None)
    return q_mm_day
