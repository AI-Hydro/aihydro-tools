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

For ``variable="streamflow"`` served by the USGS observation product it also writes the
session ``streamflow`` slot (the single slot writer, ``streamflow_slot``): the series, per-value
qualification codes (A / P / e / raw qualifier tokens), the quality summary, the gauge
resolution record, upstream provenance and the retained-file digest. The streamflow validators
(record length, unit consistency, USGS qualification codes) then run on this path too.

``gauge_id`` (a USGS site number) names the gauge explicitly. When the session holds a
BasinRef with a verified ``usgs:`` alias for the geometry being fetched, it is passed to
aihydro-data as ``basin_ref``. Precedence is aihydro-data's (explicit id > alias > outlet);
the area-agreement thresholds are NOT exposed here.

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


def _retain(session_id: str, result: dict, variable: str, files_out: list | None = None) -> dict | None:
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
        # ``units`` is the provider-declared unit when the payload declared one;
        # ``units_spec`` / ``units_declared`` are carried as recorded (absent on
        # an aihydro-data that predates them).
        for k in ("units", "units_spec", "units_declared", "timestep", "product", "source",
                  "spatial_support", "aggregation_actual"):
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
        entry = {"path": to_ref(path, workspace), "digest": file_digest, "role": "artifact"}
        if files_out is not None:
            files_out.append(entry)        # the caller declares every retained file at once
        else:
            run_records.declare_lineage(retained_files=[entry])
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


def _fetch_with_identity(**kw: Any) -> dict[str, Any]:
    """``aihydro_data.fetch`` with a gauge identity (``gauge_id`` / ``basin_ref``), serialised
    and error-enveloped exactly like aihydro-data's own ``data_fetch`` implementation."""
    from aihydro_data.mcp import _result_to_dict, _tool_envelope

    @_tool_envelope
    def _go() -> dict[str, Any]:
        from aihydro_data import fetch

        outlet = kw.get("outlet")
        return _result_to_dict(fetch(
            variable=kw["variable"], geometry=kw["geometry"], start=kw["start"], end=kw["end"],
            mode=kw["mode"], product=kw["product"], aggregation=kw["aggregation"],
            cache=kw["cache"], region=kw["region"], outlet=tuple(outlet) if outlet else None,
            gauge_id=kw.get("gauge_id"), basin_ref=kw.get("basin_ref"),
        ))
    return _go()


def _session_basin_ref(session_id: str, geometry: Any) -> dict | None:
    """The session's BasinRef, when it can name the gauge for the geometry being fetched.

    Passed only if (a) the watershed slot holds a BasinRef whose ``usgs:`` aliases are all
    verified, and (b) the requested geometry is that watershed (GeoJSON bounds agree): a
    different basin must not inherit this one's gauge. Anything else returns None and the
    request is resolved by aihydro-data from the geometry alone. Never raises.
    """
    try:
        from ai_hydro.session import HydroSession

        ws = (HydroSession.load(session_id).watershed or {}).get("data") or {}
        ref = ws.get("basin_ref")
        if not isinstance(ref, dict):
            return None
        usgs = [a for a in (ref.get("aliases") or [])
                if isinstance(a, dict) and a.get("scheme") == "usgs" and a.get("relation", "same_as") == "same_as"]
        if not usgs or not all(a.get("verified") for a in usgs):
            return None
        if not isinstance(geometry, dict):
            return None
        from shapely.geometry import shape
        from ai_hydro.mcp.helpers import _get_session_geometry

        want = shape(_get_session_geometry(session_id)).bounds
        got = shape(geometry).bounds
        if all(abs(a - b) <= 1e-6 for a, b in zip(want, got)):
            return ref
    except Exception as exc:
        log.debug("data_fetch: no session BasinRef used (%s)", exc)
    return None


def _write_streamflow_slot(session_id: str, result: dict, start: str, end: str,
                           retained_files: list) -> dict | None:
    """Write the session ``streamflow`` slot from a USGS observation result. Never raises.

    Returns a small description of what was written, or None when the served series is not
    a USGS observation (a modelled product, or no cache entry to read the codes from). In
    both cases the retained-file declarations collected so far are still declared.
    """
    from ai_hydro.session import run_records

    slot = None
    try:
        from aihydro_data.cache import cache_read
        from ai_hydro.mcp.streamflow_slot import persist_streamflow_slot, slot_from_fetch_result

        if result.get("product") == "NWIS_STREAMFLOW" and result.get("site_resolution"):
            fetched = cache_read(result["cache_key"]) if result.get("cache_key") else None
            site = str(result["site_resolution"].get("site") or "")
            gid = site[5:] if site.upper().startswith("USGS-") else site
            d = slot_from_fetch_result(result, fetched, gauge_id=gid, start=start, end=end) if fetched is not None and gid else None
            if d is not None:
                from ai_hydro.session import HydroSession

                session = HydroSession.load(session_id)
                if not session.site_id:
                    session.site_id, session.site_type = gid, "usgs_gauge"
                    session.save()
                saved = persist_streamflow_slot(session_id, d, gid, tool_name="data_fetch",
                                                extra_retained=retained_files)
                retained_files = []                 # declared by persist_streamflow_slot
                n_codes = len(d["meta"].get("qualification_codes") or [])
                slot = {"slot": "streamflow", "gauge_id": gid, "data_file": saved,
                        "n_values": d["data"].get("n_days", len(d["data"].get("q_cms") or [])),
                        "qualification_codes_n": n_codes,
                        "qualifiers_other": d["meta"].get("qualifiers_other", {})}
    except Exception as exc:
        log.warning("data_fetch: could not write the streamflow slot: %s", exc)
    if retained_files:
        run_records.declare_lineage(retained_files=retained_files)
    return slot


def _streamflow_flags(session_id: str) -> list:
    """Streamflow validators on the data_fetch path (same set fetch_streamflow_data fires)."""
    from ai_hydro.mcp.tools_validators import (
        check_record_length, check_unit_consistency, check_usgs_qualification_codes,
    )

    out = []
    for fn, kw in (
        (check_unit_consistency, {"slot": "streamflow", "expected_units": "m3/s"}),
        (check_record_length, {}),
        (check_usgs_qualification_codes, {}),
    ):
        try:
            out.append(fn(session_id=session_id, **kw))
        except Exception as exc:
            out.append({"validator": getattr(fn, "__name__", "?"), "status": "error",
                        "message": str(exc), "severity": None})
    return out


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
    gauge_id: str | None = None,
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
        gauge_id:    streamflow only. USGS site number ('01013500' or 'USGS-01013500')
                     that names the gauge explicitly; it outranks a BasinRef alias and
                     an outlet. Without any identity a polygon is refused (GAUGE_REQUIRED),
                     an ambiguous outlet is refused (GAUGE_AMBIGUOUS): a refusal is final
                     and is never answered with modelled flow.

    Returns:
        On success: result dict with keys variable, product, source, cache_hit,
                    data (head + shape), license, citation, next_steps, plus
                    ``_run_id`` — pass it as ``series`` to summarize_series,
                    detect_threshold_runs, compare_series or bootstrap_statistic.
                    ``data.retained_series`` describes the retained series.
        On failure: structured error envelope with recovery hints.

    Note — slow backends (GloFAS / queued HPC):
        For variables that route to a queued backend (e.g. global streamflow →
        GloFAS EWDS), this tool raises an MCP error (code USE_ASYNC_TOOL,
        next_tools=['data_fetch_background']) rather than blocking the agent loop for minutes.
    """
    sid = None
    try:
        from ai_hydro.mcp.helpers import _resolve_session

        sid = _resolve_session(session_id, None, allow_auto_create=False)
    except Exception as exc:
        log.debug("data_fetch: no session resolved, call is not recorded (%s)", exc)

    if gauge_id is not None and variable != "streamflow":
        return {
            "error": True, "code": "GAUGE_ID_NOT_APPLICABLE",
            "message": f"gauge_id names a USGS gauge and only applies to variable='streamflow' (got {variable!r}).",
            "recovery": "Drop gauge_id, or request variable='streamflow'.",
            "next_tools": ["data_fetch"],
        }
    basin_ref = _session_basin_ref(sid, geometry) if (sid and variable == "streamflow") else None

    if gauge_id is not None or basin_ref is not None:
        result = _fetch_with_identity(
            variable=variable, geometry=geometry, start=start, end=end, mode=mode,
            product=product, aggregation=aggregation, cache=cache, region=region,
            outlet=outlet, gauge_id=gauge_id, basin_ref=basin_ref,
        )
    else:
        from aihydro_data.mcp import _data_fetch

        result = _data_fetch(
            variable=variable, geometry=geometry, start=start, end=end, mode=mode,
            product=product, aggregation=aggregation, cache=cache, region=region, outlet=outlet,
        )
    # ``redirect`` is only returned by older aihydro-data (before U4 the queued redirect was a
    # success-shaped dict); current aihydro-data raises an MCP error instead.
    if not isinstance(result, dict) or result.get("error") or result.get("redirect"):
        return result
    if sid is None:
        return result

    retained_files: list = []
    retained = _retain(sid, result, variable, retained_files)
    data = result.setdefault("data", {})
    if isinstance(data, dict):
        data["series_retained"] = retained is not None
        if retained is not None:
            data["retained_series"] = retained

    flags: list = []
    if variable == "streamflow":
        slot = _write_streamflow_slot(sid, result, start, end, retained_files)
        if slot is not None:
            result["streamflow_slot"] = slot
            flags = _streamflow_flags(sid)
    elif retained_files:
        from ai_hydro.session import run_records
        run_records.declare_lineage(retained_files=retained_files)
    if flags:
        result["quality_flags"] = flags

    from ai_hydro.mcp.enforcement import post_run

    inputs: dict[str, Any] = {
        "variable": variable, "start": start, "end": end, "mode": mode, "product": product,
        "aggregation": aggregation, "cache": cache, "region": region,
    }
    if gauge_id is not None:
        inputs["gauge_id"] = gauge_id
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
