"""
``data_fetch`` with a sealed, addressable series.

``data_fetch`` is aihydro-data's tool, registered through the ``aihydro.tools``
entry point. Called as shipped it leaves no usable record for a time series:

1. it takes no ``session_id`` (a caller passing one fails argument validation),
   so a call with no chat or study context resolves no session and the
   recording middleware counts it ``no_session`` and writes nothing;
2. even when a session resolves, the middleware only seals a minimal row, and
   the result carries no ``_run_id``, so the agent has nothing to cite;
3. the result keeps only a 5-row head of the DataFrame, so the series itself
   is retained nowhere a later tool can address it.

This module registers a wrapper under the same tool name. It calls aihydro-data's
own implementation unchanged, then (when a session resolves) writes the full
series next to the session, binds the file digest to the sealed record
(``extra.retained_files``) and returns ``_run_id`` -- the address the series
tools take as ``series``. The series is read back from aihydro-data's own cache
entry for the call (``cache_key``), so it is exactly what the fetch served; with
``cache=False`` there is no entry to read, nothing is retained, and the record
is aggregate-only (``series_retained: false``).

The result is otherwise aihydro-data's, plus: ``_run_id``, ``quality_flags``
(empty) and, under ``data.retained_series``, the recorded description of the
retained file. An optional ``session_id`` is accepted like on every other tool.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("ai_hydro.mcp.data_fetch")


def _series_payload(fetched: Any, variable: str) -> dict | None:
    """``aihydro.series/1`` payload from a FetchResult holding a time-series DataFrame."""
    import numpy as np
    import pandas as pd

    from ai_hydro.session.series import SERIES_SCHEMA

    df = getattr(fetched, "data", None)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    if "date" in df.columns:
        dates = pd.to_datetime(df["date"], errors="coerce")
        body = df.drop(columns=["date"])
    elif isinstance(df.index, pd.DatetimeIndex):
        dates = pd.Series(df.index)
        body = df.reset_index(drop=True)
    else:
        return None
    numeric = [c for c in body.columns if pd.api.types.is_numeric_dtype(body[c])]
    if not numeric or dates.isna().all():
        return None
    primary = variable if variable in numeric else numeric[0]
    keep = ~dates.isna()
    day = [d.strftime("%Y-%m-%d") for d in dates[keep]]

    def col(name: str) -> list:
        return [None if not np.isfinite(v) else float(v) for v in body.loc[keep.values, name].astype(float)]

    payload: dict[str, Any] = {
        "schema": SERIES_SCHEMA,
        "dates": day,
        "values": col(primary),
        "value_column": primary,
        "variable": getattr(fetched, "variable", variable),
        "product": getattr(fetched, "product", None),
        "source": getattr(fetched, "source", None),
        "units": getattr(fetched, "units", "") or None,
        "timestep": getattr(fetched, "timestep", "") or None,
        "spatial_support": getattr(fetched, "spatial_support", None),
        "aggregation_actual": getattr(fetched, "aggregation_actual", None) or None,
        "cache_key": getattr(fetched, "cache_key", None),
    }
    others = [c for c in numeric if c != primary]
    if others:
        payload["other_columns"] = {c: col(c) for c in others}
    return payload


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:40] or "series"


def _retain(session_id: str, result: dict, variable: str) -> dict | None:
    """Write the served series and declare it on the in-flight record. Never raises."""
    try:
        from aihydro_data.cache import cache_read
        from ai_hydro.session import run_records
        from ai_hydro.session.refs import to_ref
        from ai_hydro.session.series import retain_series

        key = result.get("cache_key")
        fetched = cache_read(key) if key else None
        payload = _series_payload(fetched, variable) if fetched is not None else None
        if payload is None:
            return None
        # Descriptors as the caller saw them in this call's result take precedence
        # over what the cache entry reconstructs.
        for k in ("units", "timestep", "product", "source", "spatial_support", "aggregation_actual"):
            if result.get(k) not in (None, ""):
                payload[k] = result[k]
        from aihydro_core.records import digest as _digest

        name = f"series_{_safe(variable)}_{_digest(payload)[7:19]}.json"
        saved = retain_series(session_id, name, payload)
        if saved is None:
            return None
        path, file_digest = saved
        workspace = None
        try:
            from ai_hydro.session import HydroSession

            workspace = HydroSession.load(session_id).workspace_dir
        except Exception:
            pass
        run_records.declare_lineage(retained_files=[{
            "path": to_ref(path, workspace), "digest": file_digest, "role": "artifact"}])
        finite = [v for v in payload["values"] if v is not None]
        return {
            "value_column": payload["value_column"],
            "n": len(payload["dates"]),
            "n_finite": len(finite),
            "first_date": payload["dates"][0],
            "last_date": payload["dates"][-1],
            "file_digest": file_digest,
            "format": payload["schema"],
        }
    except Exception as exc:
        log.warning("data_fetch: could not retain the served series: %s", exc)
        return None


def data_fetch(
    variable: str,
    geometry: Any,
    start: str,
    end: str,
    mode: str = "auto",
    product: str | None = None,
    aggregation: str = "basin_mean",
    cache: bool = True,
    region: str | None = None,
    outlet: tuple[float, float] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """
    Fetch a single hydrology variable for one geometry / time window.

    Args:
        variable:    Canonical variable name ('precipitation', 'tmax', 'et', …).
                     Call data_list_products() with no args to see all variables.
        geometry:    One of: (lat, lon) tuple, [minx, miny, maxx, maxy] bbox,
                     GeoJSON dict, WKT string, or a dict with 'type'+'coordinates'.
        start:       ISO-8601 start date, e.g. '2015-01-01'.
        end:         ISO-8601 end date (inclusive), e.g. '2015-12-31'.
        mode:        'auto' (router picks best product for the region) or
                     'manual' (must also supply product=).
        product:     Product ID, e.g. 'CHIRPS', 'GRIDMET_PRECIP'. Required if
                     mode='manual'; ignored otherwise.
        aggregation: How to aggregate the raster over the geometry.
                     'basin_mean' (default) → 1-D time series.
                     'raw_raster' → full clipped xarray.DataArray.
        cache:       True (default) — read from / write to disk cache. The
                     series is retained from the cache entry; with False
                     nothing is retained (the record is aggregate-only).
        session_id:  Session to record the call in. Auto-resolved from the
                     chat/study context when omitted.

    Returns:
        On success: result dict with keys variable, product, source, cache_hit,
                    data (head + shape), license, citation, next_steps, plus
                    ``_run_id`` — pass it as ``series`` to summarize_series,
                    detect_threshold_runs, compare_series or bootstrap_statistic.
                    ``data.retained_series`` describes the retained series.
        On failure: structured error envelope with recovery hints.

    Note — slow backends (GloFAS / queued HPC):
        For variables that route to a queued backend (e.g. global streamflow →
        GloFAS EWDS), this tool returns an immediate redirect to
        ``data_fetch_background`` rather than blocking the agent loop for minutes.
    """
    from aihydro_data.mcp import _data_fetch

    result = _data_fetch(
        variable=variable, geometry=geometry, start=start, end=end, mode=mode,
        product=product, aggregation=aggregation, cache=cache, region=region, outlet=outlet,
    )
    if not isinstance(result, dict) or result.get("error") or result.get("redirect"):
        return result

    try:
        from ai_hydro.mcp.helpers import _resolve_session

        sid = _resolve_session(session_id, None, allow_auto_create=False)
    except Exception as exc:
        log.debug("data_fetch: no session resolved, call is not recorded (%s)", exc)
        return result

    retained = _retain(sid, result, variable)
    data = result.setdefault("data", {})
    if isinstance(data, dict):
        data["series_retained"] = retained is not None
        if retained is not None:
            data["retained_series"] = retained

    from ai_hydro.mcp.enforcement import post_run

    inputs: dict[str, Any] = {
        "variable": variable, "start": start, "end": end, "mode": mode, "product": product,
        "aggregation": aggregation, "cache": cache, "region": region,
    }
    if isinstance(geometry, str):
        inputs["geometry"] = geometry
    return post_run("data_fetch", sid, result, inputs=inputs)


def register_data_fetch(mcp: Any) -> bool:
    """Replace aihydro-data's ``data_fetch`` registration with the retaining wrapper.

    Called once, after the ``aihydro.tools`` registrars ran. Returns False (and
    leaves whatever is registered) when aihydro-data's implementation is not
    importable.
    """
    try:
        import aihydro_data.mcp as _adm

        if not hasattr(_adm, "_data_fetch"):
            return False
    except Exception as exc:
        log.warning("data_fetch retention not installed: %s", exc)
        return False
    try:
        mcp.remove_tool("data_fetch")
    except Exception:
        pass
    mcp.tool(name="data_fetch")(data_fetch)
    return True
