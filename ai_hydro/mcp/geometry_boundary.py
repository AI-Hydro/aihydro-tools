"""The one MCP boundary for a ``geometry`` argument (DECISIONS: geometry axis order).

A bare pair of numbers has no axis order. ``[-68.5828, 47.2375]`` is lon,lat to GeoJSON and a
point in the Southern Ocean to a (lat, lon) reader, and both readings are valid coordinates, so
a guess is a silent wrong-place fetch (a modelled product for the wrong spot on Earth, answered
as if it were the right one). The rule is therefore general and has no per-tool part:

* a bare 2-number pair, as a list/tuple, a JSON-array string or a ``"a,b"`` string, is REFUSED
  with a retryable structured error that shows both readings and the exact fix;
* the unambiguous forms pass untouched: ``{"lat":..,"lon":..}``, GeoJSON, a 4-number bbox
  ``[minx, miny, maxx, maxy]`` (checked for range and order), WKT, a USGS gauge id.

``check_geometry_arg`` is the single implementation. ``GeometryBoundaryMiddleware`` applies it to
every tool call that supplies ``geometry`` or ``geometries`` (any tool, current or future,
including tools registered by other packages), and the retaining ``data_fetch`` wrapper and
``data_fetch_background`` call it too so a direct Python call is held to the same rule.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from fastmcp.server.middleware import Middleware

from ai_hydro.mcp.errors import StructuredToolError

GEOMETRY_PARAMS = ("geometry", "geometries")

_NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_PAIR_STRING = re.compile(rf"\s*\(?\s*({_NUM})\s*(?:,|\s)\s*({_NUM})\s*\)?\s*")


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fix(lat: float, lon: float) -> str:
    return json.dumps({"lat": lat, "lon": lon})


def _envelope(code: str, message: str, recovery: str, tool: str, **extra: Any) -> dict:
    out = {"error": True, "code": code, "message": message, "recovery": recovery,
           "retryable": True, "terminal": False, "next_tools": [tool] if tool else []}
    out.update(extra)
    return out


def _pair_error(a: float, b: float, tool: str) -> dict:
    """The refusal for a bare pair (a, b)."""
    if abs(a) <= 90 and abs(b) <= 90:
        as_latlon = {"lat": a, "lon": b}
        as_lonlat = {"lat": b, "lon": a}
        return _envelope(
            "GEOMETRY_AXIS_AMBIGUOUS",
            f"[{a}, {b}] is a bare coordinate pair and both values are valid as latitude, so its axis "
            f"order cannot be known: read as (lat, lon) it is lat={a}, lon={b}; read as GeoJSON "
            f"(lon, lat) it is lat={b}, lon={a}. These are different places and no guess was made.",
            "Resend geometry as an explicit dict naming each axis: "
            f"{_fix(a, b)} if the first value is the latitude, or {_fix(b, a)} if the first value is the longitude.",
            tool,
            interpretations=[{"order": "lat_lon", **as_latlon}, {"order": "lon_lat", **as_lonlat}],
            example_geometry=as_lonlat,
        )
    if abs(a) <= 180 and abs(b) <= 90:
        return _envelope(
            "GEOMETRY_AXIS_SWAPPED",
            f"[{a}, {b}] is a bare coordinate pair whose first value ({a}) cannot be a latitude, so it can "
            f"only be (lon, lat): lat={b}, lon={a}. Bare pairs are not accepted; say so explicitly.",
            f"Resend geometry as {_fix(b, a)}.",
            tool,
            interpretations=[{"order": "lon_lat", "lat": b, "lon": a}],
            example_geometry={"lat": b, "lon": a},
        )
    if abs(b) <= 180 and abs(a) <= 90:
        return _envelope(
            "GEOMETRY_AXIS_SWAPPED",
            f"[{a}, {b}] is a bare coordinate pair whose second value ({b}) cannot be a latitude, so it can "
            f"only be (lat, lon): lat={a}, lon={b}. Bare pairs are not accepted; say so explicitly.",
            f"Resend geometry as {_fix(a, b)}.",
            tool,
            interpretations=[{"order": "lat_lon", "lat": a, "lon": b}],
            example_geometry={"lat": a, "lon": b},
        )
    return _envelope(
        "GEOMETRY_RANGE_INVALID",
        f"[{a}, {b}] is not a valid longitude/latitude pair (latitude is within +/-90, longitude within +/-180).",
        'Resend geometry as {"lat": <-90..90>, "lon": <-180..180>} in decimal degrees (EPSG:4326).',
        tool,
    )


def _check_bbox(v: list, tool: str) -> Optional[dict]:
    minx, miny, maxx, maxy = v
    if abs(miny) > 90 or abs(maxy) > 90 or abs(minx) > 180 or abs(maxx) > 180 or minx >= maxx or miny >= maxy:
        return _envelope(
            "GEOMETRY_BBOX_INVALID",
            f"bbox {v} is not [minx(lon), miny(lat), maxx(lon), maxy(lat)] in decimal degrees with minx<maxx and miny<maxy "
            "(latitudes within +/-90, longitudes within +/-180).",
            "Resend as [west_lon, south_lat, east_lon, north_lat], or pass a GeoJSON Polygon.",
            tool,
        )
    return None


def _check_dict(d: dict, tool: str) -> Optional[dict]:
    if {"lat", "lon"}.issubset(d):
        lat, lon = d["lat"], d["lon"]
        if not (_is_num(lat) and _is_num(lon)):
            return _envelope("GEOMETRY_RANGE_INVALID", '"lat" and "lon" must be numbers.',
                             'Resend as {"lat": <number>, "lon": <number>}.', tool)
        if abs(lat) > 90 or abs(lon) > 180:
            return _envelope(
                "GEOMETRY_RANGE_INVALID",
                f"lat={lat}, lon={lon} is out of range (latitude within +/-90, longitude within +/-180).",
                'Resend as {"lat": <-90..90>, "lon": <-180..180>}; check the two values are not swapped.', tool)
    return None


def check_geometry_arg(value: Any, tool: str = "") -> Optional[dict]:
    """A structured refusal envelope when ``value`` is a bare/ambiguous or invalid coordinate form, else None.

    Pure and side-effect free. ``None`` (no geometry), shapely/GeoDataFrame objects, GeoJSON,
    WKT and gauge-id strings pass. A collection (``geometries``) is checked element by element.
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("["):
            try:
                parsed = json.loads(s)
            except ValueError:
                return None
            return check_geometry_arg(parsed, tool) if isinstance(parsed, list) else None
        m = _PAIR_STRING.fullmatch(s)
        if m:
            return _pair_error(float(m.group(1)), float(m.group(2)), tool)
        return None
    if isinstance(value, dict):
        if "type" in value:                       # GeoJSON
            return None
        if {"lat", "lon"}.issubset(value):
            return _check_dict(value, tool)
        # a {label: geometry} mapping (``geometries``): every member is held to the same rule
        for v in value.values():
            err = check_geometry_arg(v, tool)
            if err:
                return err
        return None
    if isinstance(value, (list, tuple)):
        if value and all(_is_num(v) for v in value):
            if len(value) == 2:
                return _pair_error(float(value[0]), float(value[1]), tool)
            if len(value) == 4:
                return _check_bbox([float(v) for v in value], tool)
            return _envelope(
                "GEOMETRY_NUMERIC_LIST_INVALID",
                f"a list of {len(value)} numbers is not a geometry (a bare pair is refused as ambiguous; a bbox has 4 numbers).",
                'Pass {"lat":..,"lon":..}, a [west, south, east, north] bbox, a GeoJSON geometry or WKT.', tool)
        for v in value:
            err = check_geometry_arg(v, tool)
            if err:
                return err
    return None


def tools_taking_geometry(schemas: dict[str, set]) -> list[str]:
    """Names of tools whose input schema has a geometry parameter (used by the scan test)."""
    return sorted(n for n, props in schemas.items() if props & set(GEOMETRY_PARAMS))


class GeometryBoundaryMiddleware(Middleware):
    """FastMCP middleware: hold every tool's ``geometry`` / ``geometries`` argument to ``check_geometry_arg``."""

    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        msg = context.message
        args = getattr(msg, "arguments", None)
        if isinstance(args, dict):
            tool = getattr(msg, "name", "") or ""
            for key in GEOMETRY_PARAMS:
                if key in args:
                    err = check_geometry_arg(args[key], tool)
                    if err:
                        raise StructuredToolError(err)
        return await call_next(context)


def install_geometry_boundary(mcp) -> None:
    """Attach the GeometryBoundaryMiddleware to a FastMCP instance (idempotent)."""
    if getattr(mcp, "_aihydro_geometry_boundary_installed", False):
        return
    mcp.add_middleware(GeometryBoundaryMiddleware())
    mcp._aihydro_geometry_boundary_installed = True
