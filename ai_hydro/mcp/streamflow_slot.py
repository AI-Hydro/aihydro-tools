"""The one writer of the session ``streamflow`` slot (Wave U, D6).

Two tools produce the slot: the deprecated ``fetch_streamflow_data`` and
``data_fetch(variable="streamflow", session_id=...)``. Both build the same
``{"data", "meta"}`` shape here, so the consumers (signatures, series tools, the
validators, capsule replay) cannot tell which tool wrote it.

``data`` holds the series (``dates``, ``q_cms``), the retained-file pointer
(``_data_file``) and scalar descriptors. ``meta`` holds the quality provenance the
validators and the sealed record need:

* ``qualification_codes``  per value, ``A`` / ``P`` (approval) and ``e`` or the raw
  qualifier token (``ICE`` ...) separated by spaces; aligned with ``dates``
* ``qualifiers_other``     raw counts of every qualifier outside A/P/e
* ``quality_summary``      approval/qualifier counts, ``n_negative``, ``n_missing``,
  ``provisional_fraction``, provisional span, gap statistics
* ``site_resolution``      how the gauge was identified (ADR-003 A2 record)
* ``upstream``             request URLs (never a key), response sha256, retrieval time
* ``retained_series``      ``{path, digest}`` of the retained file
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("ai_hydro.mcp.streamflow_slot")

#: Slot ``data`` keys that are provenance, not series: moved to ``meta`` once the file is written.
_QUALITY_KEYS = ("qualification_codes", "quality_summary", "site_resolution", "upstream")


def move_quality_to_meta(d: dict) -> None:
    """Move quality/provenance keys from ``d["data"]`` into ``d["meta"]`` (in place)."""
    data, meta = d.get("data"), d.setdefault("meta", {})
    if not isinstance(data, dict):
        return
    for k in _QUALITY_KEYS:
        if k in data:
            meta[k] = data.pop(k)
    qs = meta.get("quality_summary")
    if isinstance(qs, dict):
        meta.setdefault("qualifiers_other", dict(qs.get("qualifiers_other") or {}))


def response_meta(meta: dict) -> dict:
    """``meta`` for a tool response: the per-value code list is replaced by its length."""
    out = {k: v for k, v in (meta or {}).items() if k != "qualification_codes"}
    if isinstance((meta or {}).get("qualification_codes"), list):
        out["qualification_codes_n"] = len(meta["qualification_codes"])
    return out


def persist_streamflow_slot(
    session_id: str,
    d: dict,
    gauge_id: str,
    *,
    tool_name: str,
    extra_retained: Optional[list] = None,
) -> Optional[str]:
    """Write the retained series file, bind its digest, store the slot. Returns the file path.

    ``d`` is ``{"data": {...series...}, "meta": {...}}`` and is mutated. The retained-file
    digest goes in ``meta.retained_series`` (a later consumer re-digests the file and
    compares) and in this run's record (``extra.retained_files``, together with
    ``extra_retained`` so one declaration carries every file the call retained).
    """
    from ai_hydro.mcp.helpers import _session_store, _workspace_write
    from ai_hydro.session import store as _store

    fname = f"streamflow_{gauge_id}.json"
    saved = _workspace_write(session_id, fname, d["data"])
    if not saved:
        # No workspace: the lean session JSON would keep only q_cms_n and the series
        # would be unrecoverable for the tools that consume it.
        saved = _store.write_session_data_file(session_id, fname, d["data"])
    retained = list(extra_retained or [])
    if saved:
        d["data"]["_data_file"] = saved
        try:
            from aihydro_core.records import digest_bytes as _dbytes
            from ai_hydro.session.refs import to_ref as _to_ref

            fdig = _dbytes(Path(saved).read_bytes())
            d.setdefault("meta", {})["retained_series"] = {"path": saved, "digest": fdig}
            ws = None
            try:
                from ai_hydro.session import HydroSession
                ws = getattr(HydroSession.load(session_id), "workspace_dir", None)
            except Exception:
                pass
            retained.append({"path": _to_ref(saved, ws), "digest": fdig, "role": "artifact"})
        except Exception as exc:
            log.warning("could not digest retained streamflow file: %s", exc)
    if retained:
        try:
            from ai_hydro.session import run_records as _rr
            _rr.declare_lineage(retained_files=retained)
        except Exception as exc:
            log.warning("could not declare retained streamflow files: %s", exc)
    move_quality_to_meta(d)
    _session_store(session_id, "streamflow", d, tool_name=tool_name)
    return saved


def slot_from_fetch_result(result: dict, fetched: Any, *, gauge_id: str, start: str, end: str) -> Optional[dict]:
    """``{"data", "meta"}`` for the streamflow slot from a ``data_fetch`` result.

    ``fetched`` is the FetchResult read back from the cache entry; its frame carries the
    per-value ``approval_code`` / ``qualifier_code`` columns. ``None`` when the served
    series is not a USGS observation (modelled products carry no site resolution).
    """
    import pandas as pd
    from ai_hydro.data.streamflow import slot_quality_meta

    df = getattr(fetched, "data", None)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    if not {"date", "streamflow", "approval_code", "qualifier_code"} <= set(df.columns):
        return None
    df = df.dropna(subset=["streamflow"]).reset_index(drop=True)
    if df.empty:
        return None
    sr = result.get("site_resolution") or {}
    qmeta = slot_quality_meta(df, result.get("quality"), sr, result.get("upstream"))
    dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist()
    data = {
        "dates": dates,
        "q_cms": [float(v) for v in df["streamflow"].to_numpy()],
        "units": "m3/s",
        "n_days": len(df),
        "gauge_id": gauge_id,
        "gauge_name": sr.get("site_name") or "",
        "gauge_lat": None,
        "gauge_lon": None,
        "start_date": start,
        "end_date": end,
        "interval": "daily",
        "qualification_codes": qmeta["qualification_codes"],
        "quality_summary": qmeta["quality_summary"],
        "site_resolution": qmeta.get("site_resolution", {}),
        "upstream": qmeta.get("upstream", {}),
        "_aihydro_data_product": result.get("product"),
        "_aihydro_data_source": result.get("source"),
    }
    loc = (sr.get("location") or {}) if isinstance(sr.get("location"), dict) else {}
    if loc:
        data["gauge_lat"], data["gauge_lon"] = loc.get("lat"), loc.get("lon")
    meta = {
        "tool": "data_fetch",
        "source": f"{result.get('product')} (aihydro-data)",
        "citation": result.get("citation"),
        "cache_key": result.get("cache_key"),
        "params": {"gauge_id": gauge_id, "start_date": start, "end_date": end, "interval": "daily"},
    }
    return {"data": data, "meta": meta}


def mirror_streamflow_source(data: dict, src: Any) -> None:
    """Mirror the watershed result's ``_streamflow_source`` into non-underscore fields.

    The run log and the sealed evidence drop ``_``-prefixed keys, so the underscore block
    cannot carry the streamflow provenance (or the reason there was none) into a claim's
    evidence. These mirrors do: observation kind, product, site, the quality summary, the
    upstream provenance and, when the watershed could not obtain a usable series, the
    refusal (``streamflow_error_code`` / ``_message`` / ``_terminal``) -- so a signatures run
    with observation "none" says why instead of showing NaN. In place; no-op for a non-dict.
    """
    if not isinstance(src, dict) or not isinstance(data, dict):
        return
    data["streamflow_observation"] = src.get("observation")
    data["streamflow_product"] = src.get("product")
    if src.get("site") is not None:
        data["streamflow_site"] = src["site"]
    qs = src.get("quality_summary")
    if isinstance(qs, dict):
        data["streamflow_quality_summary"] = qs
        for k_out, k_in in (("streamflow_provisional_fraction", "provisional_fraction"),
                            ("streamflow_n_negative", "n_negative"),
                            ("streamflow_n_missing", "n_missing")):
            if k_in in qs:
                data[k_out] = qs[k_in]
    if isinstance(src.get("upstream"), dict):
        data["streamflow_upstream"] = src["upstream"]
    err = src.get("error")
    if isinstance(err, dict):
        data["streamflow_error"] = err
        data["streamflow_error_code"] = err.get("code")
        data["streamflow_error_message"] = err.get("message")
        data["streamflow_error_terminal"] = err.get("terminal")


def mirror_gauge_area_check(data: dict) -> dict | None:
    """Seal the watershed's gauge-vs-polygon area check and return a quality flag for a failure.

    aihydro-watershed writes ``gauge_area_check`` (pass | fail | unavailable), ``gauge_area_ratio``,
    ``gauge_area_km2`` (scalars: already sealed) and ``streamflow_warnings`` (a list: dropped by
    the run log and the evidence). This adds the list as ``streamflow_warnings_text`` /
    ``streamflow_warnings_n`` and, from ``_streamflow_source.site_resolution``, the non-underscore
    ``streamflow_site_resolution``. Returns a ``quality_flags`` entry when the check failed
    (the gauge's drainage area disagrees with the polygon), else None.
    """
    if not isinstance(data, dict):
        return None
    warns = data.get("streamflow_warnings")
    if isinstance(warns, list):
        data["streamflow_warnings_n"] = len(warns)
        data["streamflow_warnings_text"] = " | ".join(str(w) for w in warns)
    src = data.get("_streamflow_source")
    if isinstance(src, dict) and isinstance(src.get("site_resolution"), dict):
        data["streamflow_site_resolution"] = src["site_resolution"]
    if data.get("gauge_area_check") != "fail":
        return None
    ratio = data.get("gauge_area_ratio")
    return {
        "validator": "gauge_area_check",
        "status": "warning",
        "severity": "high",
        "message": ("The gauge's drainage area disagrees with the basin polygon "
                    f"(gauge/polygon area ratio {ratio}; gauge area {data.get('gauge_area_km2')} km2). "
                    "The signatures describe the gauge's catchment, which may not be this basin."
                    + (f" {data.get('streamflow_warnings_text')}" if data.get("streamflow_warnings_text") else "")),
        "recommendation": ("Confirm the gauge id is the outlet gauge of this basin, or pass the correct "
                           "gauge_id; do not present these signatures as describing the polygon."),
        "affected_slots": ["signatures"],
        "metadata": {"quality_flag": "gauge_area_mismatch", "gauge_area_ratio": ratio,
                     "gauge_area_km2": data.get("gauge_area_km2")},
    }
