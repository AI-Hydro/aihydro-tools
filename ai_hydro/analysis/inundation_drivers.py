"""
Discharge drivers for flood inundation mapping.

Resolves peak discharge from explicit Q, return period, design hydrograph,
or session streamflow (observed / GloFAS-via-fetch peak).
"""
from __future__ import annotations

from typing import Any

import numpy as np

__all__ = ["resolve_inundation_discharge"]


def _session_get(session, key: str) -> Any:
    if hasattr(session, "get"):
        return session.get(key)
    return getattr(session, key, None)


def _peak_from_streamflow_slot(sf: Any) -> tuple[float | None, str]:
    if not isinstance(sf, dict):
        return None, ""
    data = sf.get("data", sf)
    if not isinstance(data, dict):
        return None, ""

    for key in ("q_max_cms", "peak_discharge_cms", "peak_discharge_m3s"):
        val = data.get(key)
        if val is not None:
            try:
                return float(val), f"streamflow.{key}"
            except (TypeError, ValueError):
                continue

    q_arr = None
    for key in ("q_cms", "discharge_cms", "flow_cms", "streamflow", "q"):
        v = data.get(key)
        if isinstance(v, (list, tuple)) and len(v) > 1:
            try:
                q_arr = np.asarray(v, dtype=float)
                break
            except (TypeError, ValueError):
                continue

    if q_arr is None:
        data_file = data.get("_data_file") or sf.get("_data_file")
        if data_file:
            try:
                import json

                with open(data_file, encoding="utf-8") as fh:
                    file_data = json.load(fh)
                for key in ("q_cms", "discharge_cms", "flow_cms"):
                    v = file_data.get(key)
                    if isinstance(v, (list, tuple)) and len(v) > 1:
                        q_arr = np.asarray(v, dtype=float)
                        break
            except Exception:
                pass

    if q_arr is not None and len(q_arr):
        finite = q_arr[np.isfinite(q_arr)]
        if len(finite):
            return float(np.max(finite)), "streamflow.peak_from_series"

    return None, ""


def resolve_inundation_discharge(
    session,
    *,
    discharge_m3s: float | None = None,
    return_period: int | None = None,
    use_design_peak: bool = False,
    use_session_peak: bool = False,
) -> tuple[float | None, str | None, dict[str, Any] | None]:
    """
    Resolve peak discharge (m³/s) and source label.

    Returns (q, source_label, error_dict). error_dict is set when q is None.
    """
    if discharge_m3s is not None:
        q = float(discharge_m3s)
        if q <= 0:
            return None, None, {
                "error": True,
                "code": "INVALID_DISCHARGE",
                "message": "discharge_m3s must be positive.",
            }
        return q, "explicit", None

    if return_period is not None:
        ff = _session_get(session, "flood_frequency")
        levels: list | dict = []
        if isinstance(ff, dict):
            levels = ff.get("data", ff).get("return_levels") or []
        if isinstance(levels, list):
            for row in levels:
                if isinstance(row, dict) and int(row.get("return_period", 0)) == int(return_period):
                    q = float(row.get("value") or row.get("discharge_m3s") or 0)
                    if q > 0:
                        return q, f"return_period_{return_period}", None
        return None, None, {
            "error": True,
            "code": "MISSING_DISCHARGE",
            "message": f"No return level for {return_period}-yr in session. Run compute_flood_frequency first.",
            "recovery": f"compute_flood_frequency(session_id) then map_flood_inundation(return_period={return_period})",
            "next_tools": ["compute_flood_frequency", "fetch_streamflow_data"],
        }

    if use_design_peak:
        dh = _session_get(session, "design_hydrograph")
        if isinstance(dh, dict):
            dh_data = dh.get("data", dh)
            peak = dh_data.get("peak_discharge_cms") or dh_data.get("peak_discharge_m3s")
            if peak is not None:
                q = float(peak)
                if q > 0:
                    return q, "design_hydrograph_peak", None
        return None, None, {
            "error": True,
            "code": "MISSING_DISCHARGE",
            "message": "use_design_peak=True but no design_hydrograph in session.",
            "recovery": "compute_design_hydrograph(return_period=100) then map_flood_inundation(use_design_peak=True)",
            "next_tools": ["compute_design_hydrograph"],
        }

    if use_session_peak:
        sf = _session_get(session, "streamflow")
        if sf:
            q, src = _peak_from_streamflow_slot(sf)
            if q is not None and q > 0:
                return q, src or "streamflow_peak", None
        return None, None, {
            "error": True,
            "code": "MISSING_DISCHARGE",
            "message": "use_session_peak=True but no streamflow peak in session.",
            "recovery": (
                "fetch_streamflow_data(gauge_id=...) or "
                "data_fetch(variable='streamflow', ...) then map_flood_inundation(use_session_peak=True)"
            ),
            "next_tools": ["fetch_streamflow_data", "data_fetch"],
        }

    return None, None, {
        "error": True,
        "code": "MISSING_DISCHARGE",
        "message": (
            "Provide discharge_m3s, return_period, use_design_peak=True, or use_session_peak=True."
        ),
        "recovery": "map_flood_inundation(discharge_m3s=500) or return_period=100",
    }
