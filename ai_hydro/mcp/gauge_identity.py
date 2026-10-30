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
    if geometry is None:                      # gauge-first: the location is resolved from the gauge afterwards
        return None
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


# A requested place and the named gauge may differ by at most this much. It is aihydro-data's
# pre-registered gauge-to-outlet distance threshold (DEFAULT_THRESHOLDS["max_distance_m"],
# ADR-003 A2), read from there so the two cannot drift; 5 km if that is unavailable.
def _mismatch_limit_m() -> float:
    try:
        from aihydro_data.sources.usgs_waterdata import DEFAULT_THRESHOLDS

        return float(DEFAULT_THRESHOLDS["max_distance_m"])
    except Exception:
        return 5000.0


def gauge_point(gauge_id: str) -> dict[str, Any]:
    """``{"site", "name", "lat", "lon"}`` of a USGS monitoring location (the Water Data API record).

    Raises aihydro-data's ``SourceUnavailable`` / ``TerminalSourceError`` (``USGS_SITE_INVALID``,
    ``USGS_SITE_NOT_FOUND`` are terminal; transport failures are transient).
    """
    from aihydro_data.sources import usgs_waterdata as uw

    loc = uw.get_monitoring_locations(ids=[gauge_id])[0]
    return {"site": loc.site_id, "name": loc.name, "lat": loc.lat, "lon": loc.lon}


class AntimeridianGeometry(ValueError):
    """A geometry written across +/-180 (planar longitude span > 180 degrees): its distance cannot be computed on lon/lat."""


def _distance_m(geometry: Any, lat: float, lon: float) -> float:
    """Great-circle metres from the point to the nearest part of ``geometry`` (0 when inside it).

    The point's longitude is shifted by 360 degrees to the copy nearest the geometry, so a point at 179.99 and a polygon
    at -179.9 are neighbours, not 359 degrees apart. A geometry whose own longitude span exceeds 180 degrees was written across
    the antimeridian (shapely is planar there); that raises ``AntimeridianGeometry`` instead of returning a wrong distance.
    """
    import math

    from shapely.geometry import Point
    from shapely.ops import nearest_points

    from aihydro_data.geometry import coerce_geometry

    g = coerce_geometry(geometry)
    if g.bounds is None:
        raise ValueError("geometry has no bounds")
    minx, _, maxx, _ = g.bounds
    if maxx - minx > 180:
        exc = AntimeridianGeometry(
            f"the geometry spans longitudes {minx:g} to {maxx:g}, so it crosses the antimeridian (or is not a basin); "
            "distances to it cannot be computed on lon/lat.")
        exc.span = (minx, maxx)         # lets the refusal tell a crossing from a world-wide box
        raise exc
    mid = (minx + maxx) / 2
    while lon - mid > 180:
        lon -= 360
    while lon - mid < -180:
        lon += 360
    p = Point(lon, lat)
    if g.intersects(p):
        return 0.0
    q = nearest_points(g, p)[0]
    la1, lo1, la2, lo2 = map(math.radians, (lat, lon, q.y, q.x))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def _lookup_refusal(exc: Exception, tool: str) -> dict[str, Any]:
    """The envelope for a gauge-location lookup that failed (terminal only for an identity failure)."""
    if hasattr(exc, "to_dict"):
        d = dict(exc.to_dict())
        try:
            from aihydro_data.exceptions import TerminalSourceError

            d["terminal"] = isinstance(exc, TerminalSourceError)
        except Exception:
            pass
        d.setdefault("next_tools", [tool])
        return d
    return {"error": True, "code": "GAUGE_LOCATION_LOOKUP_FAILED", "message": f"could not look up the gauge: {exc}",
            "recovery": "Retry, or pass a geometry for the gauge.", "next_tools": [tool], "terminal": False}


def resolve_request(variable: str, geometry: Any, product: Optional[str], gauge_id: Optional[str],
                    session_id: Optional[str], tool: str) -> dict[str, Any]:
    """Gauge-first resolution of a fetch request's place (ADR-003 A2, ``geometry`` optional).

    Returns ``{"refusal": envelope}`` or ``{"geometry", "gauge_id", "basin_ref", "hint", "notes"}``.

    * ``gauge_id`` given, no geometry: the place IS the gauge, located from the USGS monitoring-location
      record (a point). No coordinates are needed and none are invented.
    * ``gauge_id`` and a geometry that are not the same place (farther apart than the pre-registered
      5 km gauge distance): refused, naming both locations. A failed lookup skips this check only (the
      pinned fetch still returns that gauge's series or a refusal, never another place's data).
    * neither: the session's verified BasinRef (its watershed geometry) or, for a session whose
      ``site_id`` is a USGS gauge, that gauge. Nothing to resolve -> ``GEOMETRY_REQUIRED``.
    """
    notes: dict[str, Any] = {}
    basin_ref = hint = None

    if geometry is None and gauge_id is None:
        if variable != "streamflow" or product not in (None, NWIS_PRODUCT) or not session_id:
            return {"refusal": _geometry_required(tool, variable, bool(session_id))}
        try:
            from ai_hydro.mcp.helpers import _get_session_geometry
            from ai_hydro.session import HydroSession

            session = HydroSession.load(session_id)
            ws_geom = None
            try:
                ws_geom = _get_session_geometry(session_id)
            except Exception:
                pass
            if ws_geom is not None:
                basin_ref, hint = session_basin_ref(session_id, ws_geom)
                if basin_ref is not None:
                    notes["location_source"] = "session BasinRef (verified usgs: alias), session watershed geometry"
                    notes["session_note"] = ("No geometry or gauge_id was given: the session's verified BasinRef (its watershed and "
                                             "usgs: alias) named the place. Pass gauge_id or geometry explicitly for a different one.")
                    return {"geometry": ws_geom, "gauge_id": None, "basin_ref": basin_ref, "hint": hint, "notes": notes}
            if session.site_type == "usgs_gauge" and session.site_id:
                gauge_id = str(session.site_id)
                notes["gauge_source"] = f"session.site_id={gauge_id}"
                notes["session_note"] = (f"No geometry or gauge_id was given: the session's site_id (USGS gauge {gauge_id}) named the "
                                         "place. Pass gauge_id or geometry explicitly to fetch a different one.")
        except Exception as exc:
            log.debug("gauge-first: no session gauge (%s)", exc)
        if gauge_id is None:
            return {"refusal": _geometry_required(tool, variable, True, hint)}

    if gauge_id is not None:
        refusal = identity_refusal(variable, None, product, tool)      # variable / product conflicts first
        if refusal:
            return {"refusal": refusal}
        if geometry is None:
            try:
                pt = gauge_point(gauge_id)
            except Exception as exc:
                return {"refusal": _lookup_refusal(exc, tool)}
            geometry = {"lat": pt["lat"], "lon": pt["lon"]}
            notes["resolved_location"] = {
                "from": "gauge_id", "gauge": pt["site"], "name": pt["name"], "lat": pt["lat"], "lon": pt["lon"],
                "source": "USGS Water Data API monitoring-locations record"}
        else:
            try:
                pt = gauge_point(gauge_id)
                dist = _distance_m(geometry, pt["lat"], pt["lon"])
            except AntimeridianGeometry as exc:
                from ai_hydro.mcp.geometry_boundary import antimeridian_refusal

                return {"refusal": antimeridian_refusal(exc, tool)}
            except Exception as exc:                 # "could not look": skip this check, never guess
                log.debug("gauge-first: location check skipped (%s)", exc)
            else:
                limit = _mismatch_limit_m()
                if dist > limit:
                    from aihydro_data.geometry import coerce_geometry

                    c = coerce_geometry(geometry).centroid
                    return {"refusal": {
                        "error": True, "code": "GAUGE_LOCATION_MISMATCH", "terminal": True,
                        "message": (f"gauge_id {pt['site']} ({pt['name']}) is at lat={pt['lat']:.5f}, lon={pt['lon']:.5f}, "
                                    f"but the requested geometry (centroid lat={c.y:.5f}, lon={c.x:.5f}) is "
                                    f"{dist / 1000:.0f} km from it (limit {limit / 1000:.0f} km). They are not the same place, "
                                    "so nothing was fetched."),
                        "recovery": ("Drop geometry (the gauge locates itself), or correct it. If you passed a coordinate, "
                                     "check the axis order and send {\"lat\": ..., \"lon\": ...}."),
                        "gauge": {"site": pt["site"], "name": pt["name"], "lat": pt["lat"], "lon": pt["lon"]},
                        "geometry_centroid": {"lat": c.y, "lon": c.x}, "distance_m": round(dist),
                        "next_tools": [tool]}}
        refusal = identity_refusal(variable, geometry, product, tool)
        if refusal:
            return {"refusal": refusal}
        return {"geometry": geometry, "gauge_id": gauge_id, "basin_ref": None, "hint": None, "notes": notes}

    # geometry given, no gauge_id: the existing session-BasinRef path
    if session_id and variable == "streamflow" and product in (None, NWIS_PRODUCT):
        basin_ref, hint = session_basin_ref(session_id, geometry)
        if basin_ref is not None and identity_refusal(variable, geometry, product, tool):
            basin_ref = None                     # a session ref never forces a refusal on the request
    return {"geometry": geometry, "gauge_id": None, "basin_ref": basin_ref, "hint": hint, "notes": notes}


def _geometry_required(tool: str, variable: str, has_session: bool, hint: Optional[str] = None) -> dict[str, Any]:
    msg = (f"{tool} needs a place: no geometry was given and none could be resolved"
           + (" from a gauge_id or the session." if variable == "streamflow" else
              " (gauge_id only locates a USGS streamflow gauge)."))
    out = {"error": True, "code": "GEOMETRY_REQUIRED", "message": msg, "retryable": True, "terminal": False,
           "recovery": ('Pass geometry as {"lat": ..., "lon": ...}, a GeoJSON geometry, a [west, south, east, north] bbox '
                        'or WKT' + ("; for a USGS gauge pass gauge_id='01013500' alone (no coordinates needed), or "
                                    "delineate/select a watershed in the session first." if variable == "streamflow" else ".")),
           "next_tools": [tool]}
    if hint:
        out["hint"] = hint
    return out


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
