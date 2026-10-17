"""Gauge identity for the agent-facing fetch tools (Wave U, ADR-003 A2).

A named gauge (``gauge_id``) or a session BasinRef with a verified ``usgs:`` alias is an
OBSERVATION request: the answer is that gauge's USGS series or a refusal. It is never
answered by another product. So an identity-bearing request is *pinned*:

    mode="manual", product="NWIS_STREAMFLOW", fallback=[]

which turns a transient NWIS failure (429, timeout) into a transient error for the caller
instead of GEOGLOWS / Open-Meteo / GloFAS flow, and keeps a pinned queued backend from
bypassing the queued-backend preflight (a conflicting ``product`` is refused here).

The A2 area-agreement thresholds are deliberately not a parameter of any agent-facing tool.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("ai_hydro.mcp.gauge_identity")

NWIS_PRODUCT = "NWIS_STREAMFLOW"
#: kwargs for ``aihydro_data.fetch`` that make a request strict.
PIN: dict[str, Any] = {"mode": "manual", "product": NWIS_PRODUCT, "fallback": []}

IOU_MIN = 0.99          # geometry reuse: intersection-over-union of the two polygons ...
AREA_RATIO_TOL = 0.01   # ... and their areas within 1 %
NEAR_MISS_IOU = 0.80    # below IOU_MIN but this close: decline WITH a hint


def not_applicable(message: str, tool: str, recovery: str) -> dict[str, Any]:
    return {"error": True, "code": "GAUGE_ID_NOT_APPLICABLE", "message": message,
            "recovery": recovery, "next_tools": [tool], "terminal": True}


def identity_refusal(variable: str, geometry: Any, product: Optional[str], tool: str) -> Optional[dict]:
    """A refusal envelope when a gauge identity cannot be honoured for this request, else None.

    Refused: a non-streamflow variable; a ``product`` other than the USGS observation product
    (the identity would be silently ignored or would bypass the queued-backend preflight);
    a geometry outside CONUS (USGS gauges are CONUS-only here, so the identity would be ignored).
    """
    if variable != "streamflow":
        return not_applicable(
            f"gauge_id names a USGS gauge and only applies to variable='streamflow' (got {variable!r}).",
            tool, "Drop gauge_id, or request variable='streamflow'.")
    if product not in (None, NWIS_PRODUCT):
        return not_applicable(
            f"gauge_id pins the USGS observation product {NWIS_PRODUCT}; product={product!r} conflicts.",
            tool, f"Drop product=, or drop gauge_id to fetch {product} for the geometry.")
    try:
        from aihydro_data.geometry import coerce_geometry
        from aihydro_data.routing.detect import detect_region

        g = coerce_geometry(geometry)
        region = detect_region(g)
    except Exception as exc:       # an uncoercible geometry is fetch()'s own, better-worded error
        log.debug("gauge identity: region check skipped (%s)", exc)
        return None
    if region != "CONUS":
        return not_applicable(
            f"gauge_id applies to USGS gauges (CONUS) but the geometry is in region {region!r}; the gauge "
            "would be ignored and another product served.",
            tool, "Fetch this geometry without gauge_id (modelled products), or give a CONUS geometry.")
    return None


def _same_geometry(ref: dict, want: Any, have: Any) -> tuple[bool, Optional[str]]:
    """(same, near_miss_hint). Equal geometry digest, else IoU >= 0.99 with areas within 1 %."""
    from aihydro_data.geometry import geometry_id

    digest = ref.get("geometry_digest")
    if digest and ref.get("geometry_algorithm") == "aihydro.geom/1":
        try:
            if geometry_id(want) == digest:
                return True, None
        except Exception:
            pass
    if want.is_empty or have.is_empty or want.area <= 0:
        return False, None
    inter = want.intersection(have).area
    union = want.union(have).area
    iou = inter / union if union > 0 else 0.0
    ratio = have.area / want.area
    if iou >= IOU_MIN and abs(ratio - 1.0) <= AREA_RATIO_TOL:
        return True, None
    if iou >= NEAR_MISS_IOU:
        return False, (f"the session BasinRef was not used: the requested polygon overlaps the session "
                       f"watershed with IoU {iou:.3f} (need >= {IOU_MIN}) and area ratio {ratio:.3f}. "
                       "Pass gauge_id explicitly, or fetch with the session watershed geometry.")
    return False, None


def session_basin_ref(session_id: str, geometry: Any) -> tuple[Optional[dict], Optional[str]]:
    """``(basin_ref, hint)``: the session's BasinRef when it can name the gauge for ``geometry``.

    Used only if the ref verifies (``verify_basin_ref_dict``), all its ``usgs:`` aliases are
    verified, and the requested geometry IS the session watershed (geometry-digest equality,
    else IoU >= 0.99 with areas within 1 %): a different basin must not inherit this one's
    gauge. A near miss returns ``(None, hint)``. Never raises.
    """
    try:
        from aihydro_core.records import verify_basin_ref_dict
        from ai_hydro.identity import session_basin_ref as _held_ref, usgs_ids_of_ref
        from ai_hydro.session import HydroSession
        from ai_hydro.mcp.helpers import _get_session_geometry
        from aihydro_data.geometry import coerce_geometry

        session = HydroSession.load(session_id)
        ref = _held_ref(session)
        if not ref:
            return None, None
        ref = dict(ref)
        usgs = [a for a in (ref.get("aliases") or [])
                if isinstance(a, dict) and a.get("scheme") == "usgs" and a.get("relation", "same_as") == "same_as"]
        if not usgs_ids_of_ref(ref) or not all(a.get("verified") for a in usgs):
            return None, None
        if not verify_basin_ref_dict(ref):
            return None, None
        if isinstance(geometry, str):          # a gauge id / WKT string names its own place
            return None, None
        have = coerce_geometry(_get_session_geometry(session_id))
        want = coerce_geometry(geometry)
        same, hint = _same_geometry(ref, want, have)
        return (ref, None) if same else (None, hint)
    except Exception as exc:
        log.debug("data_fetch: no session BasinRef used (%s)", exc)
        return None, None
