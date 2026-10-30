"""The one MCP boundary for a ``geometry`` argument (DECISIONS: geometry axis order).

A bare pair of numbers has no axis order. ``[-68.5828, 47.2375]`` is lon,lat to GeoJSON and a
point in the Southern Ocean to a (lat, lon) reader, and both readings are valid coordinates, so
a guess is a silent wrong-place fetch (a modelled product for the wrong spot on Earth, answered
as if it were the right one). The rule is therefore general and has no per-tool part:

* a bare 2-number pair, as a list/tuple, a JSON-array string or a ``"a,b"`` string, is REFUSED
  with a retryable structured error that shows both readings and the exact fix;
* the unambiguous forms pass untouched: ``{"lat":..,"lon":..}``, GeoJSON, a 4-number bbox
  ``[minx, miny, maxx, maxy]`` (checked for range and order), WKT, a USGS gauge id.

The same module range-checks every other coordinate an agent can pass: GeoJSON positions (a projected
``[500000, 5200000]`` is refused), parameters named like a latitude / longitude (``lat``, ``outlet_lat``,
``longitude`` ...) and the ``outlet`` pair. A bare 4-number bbox whose four values are all within +/-90 has the
same ambiguity as a pair (``[47,-69,48,-68]`` is lat-first or lon-first) and is refused; the explicit form is
``{"west","south","east","north"}``, which the boundary converts to a GeoJSON Polygon.

``check_geometry_arg`` is the single implementation. ``GeometryBoundaryMiddleware`` applies it to
every tool call that supplies ``geometry`` or ``geometries`` (any tool, current or future,
including tools registered by other packages), and the retaining ``data_fetch`` wrapper and
``data_fetch_background`` call it too so a direct Python call is held to the same rule.
"""
from __future__ import annotations

import contextvars
import json
import re
from typing import Any, Optional

from fastmcp.server.middleware import Middleware

from ai_hydro.mcp.errors import StructuredToolError
from ai_hydro.mcp.gauge_identity import AntimeridianGeometry, _distance_m

GEOMETRY_PARAMS = ("geometry", "geometries")

#: True while a call runs in which the middleware CONVERTED an ``{"lat","lon"}`` outlet to the ``[lat, lon]`` list the
#: tool signature takes (it is not set merely because the middleware ran). A tool that sees a pair while this is set is
#: looking at that converted value, not a caller's bare pair. A direct Python call has it unset and the
#: wrapper validates and converts itself with the same functions.
BOUNDARY_CHECKED: contextvars.ContextVar[bool] = contextvars.ContextVar("aihydro_boundary_checked", default=False)

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


def _bbox_dict(w: float, so: float, e: float, n: float) -> str:
    return json.dumps({"west": w, "south": so, "east": e, "north": n})


def _check_bbox(v: list, tool: str) -> Optional[dict]:
    minx, miny, maxx, maxy = v
    if all(abs(x) <= 90 for x in v):
        a = (minx, miny, maxx, maxy)           # lon,lat,lon,lat (GeoJSON / shapely order)
        b = (minx, miny, maxx, maxy)           # the same four numbers read as south, west, north, east
        return _envelope(
            "GEOMETRY_BBOX_AXIS_AMBIGUOUS",
            f"bbox {v} has all four values within +/-90, so it is [west, south, east, north] "
            f"(west={a[0]}, south={a[1]}, east={a[2]}, north={a[3]}) or lat-first "
            f"(south={b[0]}, west={b[1]}, north={b[2]}, east={b[3]}); these are different places and no guess was made.",
            "Resend as an explicit dict: " + _bbox_dict(*a) + " if the order is west,south,east,north, or "
            + _bbox_dict(b[1], b[0], b[3], b[2]) + " if it is south,west,north,east.",
            tool,
            interpretations=[{"order": "west_south_east_north", "west": a[0], "south": a[1], "east": a[2], "north": a[3]},
                             {"order": "south_west_north_east", "south": b[0], "west": b[1], "north": b[2], "east": b[3]}],
        )
    if abs(miny) > 90 or abs(maxy) > 90 or abs(minx) > 180 or abs(maxx) > 180 or minx >= maxx or miny >= maxy:
        return _envelope(
            "GEOMETRY_BBOX_INVALID",
            f"bbox {v} is not [minx(lon), miny(lat), maxx(lon), maxy(lat)] in decimal degrees with minx<maxx and miny<maxy "
            "(latitudes within +/-90, longitudes within +/-180).",
            "Resend as [west_lon, south_lat, east_lon, north_lat], or pass a GeoJSON Polygon.",
            tool,
        )
    return None


def _check_bbox_range(v: list, tool: str) -> dict:
    return _envelope(
        "GEOMETRY_BBOX_INVALID",
        f"bbox {v} is not west<east, south<north in decimal degrees (latitudes within +/-90, longitudes within +/-180).",
        "Resend as {\"west\":..,\"south\":..,\"east\":..,\"north\":..}, or pass a GeoJSON Polygon.", tool)


def _check_positions(obj: Any, tool: str, _depth: int = 0) -> Optional[dict]:
    """Range-check every GeoJSON position under ``obj`` as [lon, lat]: a projected or lat-first-and-out-of-range one is refused."""
    if _depth > 12 or not isinstance(obj, (dict, list)):
        return None
    if isinstance(obj, dict):
        for k in ("coordinates", "geometry", "geometries", "features"):
            if k in obj:
                err = _check_positions(obj[k], tool, _depth + 1)
                if err:
                    return err
        return None
    if len(obj) >= 2 and all(_is_num(x) for x in obj):
        lon, lat = obj[0], obj[1]
        if abs(lon) > 180 or abs(lat) > 90:
            return _envelope(
                "GEOMETRY_RANGE_INVALID",
                f"GeoJSON position {list(obj[:2])} is not [lon, lat] in decimal degrees (EPSG:4326): longitude must be within "
                "+/-180 and latitude within +/-90. Projected (metre) or swapped coordinates are not accepted.",
                "Reproject to EPSG:4326 and send [lon, lat] positions.", tool)
        return None
    for x in obj:
        err = _check_positions(x, tool, _depth + 1)
        if err:
            return err
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
        if s.startswith("{"):
            try:
                parsed = json.loads(s)
            except ValueError:
                return None
            return check_geometry_arg(parsed, tool) if isinstance(parsed, dict) else None
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
        if {"west", "south", "east", "north"}.issubset(value):
            nums = [value[k] for k in ("west", "south", "east", "north")]
            if not all(_is_num(x) for x in nums):
                return _envelope("GEOMETRY_BBOX_INVALID", "west/south/east/north must be numbers.",
                                 'Resend as {"west":..,"south":..,"east":..,"north":..} in decimal degrees.', tool)
            w, so, e, n = map(float, nums)
            if abs(so) > 90 or abs(n) > 90 or abs(w) > 180 or abs(e) > 180 or w >= e or so >= n:
                return _check_bbox_range(list((w, so, e, n)), tool)
            return None
        if "type" in value:                       # GeoJSON
            return _check_positions(value, tool)
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


def param_kind(name: str) -> Optional[str]:
    """What a tool parameter holds, from its name: 'geometry' | 'geojson' | 'outlet' | 'lat' | 'lon' | None.

    Name-driven on purpose: a new tool that calls its parameter ``lat``, ``outlet_lon`` or ``watershed_geojson``
    is held to the rule without being listed anywhere.
    """
    n = name.lower()
    if n in GEOMETRY_PARAMS or n == "roi":
        return "geometry"
    if n == "geojson" or n.endswith("_geojson") or n.endswith("geojson"):
        return "geojson"
    if n == "outlet":
        return "outlet"
    if re.fullmatch(r"(?:.*_)?(?:lat|latitude)", n):
        return "lat"
    if re.fullmatch(r"(?:.*_)?(?:lon|lng|longitude)", n):
        return "lon"
    return None


def tools_taking_coordinates(schemas: dict[str, set]) -> dict[str, list[str]]:
    """``{tool: [params]}`` for every tool with a coordinate / geometry parameter (used by the scan test)."""
    out = {n: sorted(p for p in props if param_kind(p)) for n, props in schemas.items()}
    return {n: ps for n, ps in out.items() if ps}


def _to_polygon(bb: dict) -> dict:
    w, so, e, n = (float(bb[k]) for k in ("west", "south", "east", "north"))
    return {"type": "Polygon", "coordinates": [[[w, so], [e, so], [e, n], [w, n], [w, so]]]}


def check_coordinate_arg(name: str, value: Any, tool: str = "") -> Optional[dict]:
    """Refusal envelope for one argument by its parameter name, else None (see ``param_kind``)."""
    kind = param_kind(name)
    if kind is None or value is None:
        return None
    if kind in ("geometry", "geojson"):
        return check_geometry_arg(value, tool)
    if kind in ("lat", "lon"):
        try:
            x = float(value)
        except (TypeError, ValueError):
            return None                         # not a number: the tool's own validation reports it
        lim, what = (90.0, "latitude") if kind == "lat" else (180.0, "longitude")
        if not (abs(x) <= lim):                 # also catches NaN
            return _envelope(
                "GEOMETRY_RANGE_INVALID",
                f"{name}={value} is not a valid {what} (within +/-{lim:g} degrees, EPSG:4326).",
                f"Resend {name} in decimal degrees within +/-{lim:g}; check latitude and longitude are not swapped.", tool)
        return None
    # outlet: a bare pair is as ambiguous as a bare geometry pair (aihydro-data reads it as (lat, lon), resolve_gauge as
    # (lon, lat)), so it is refused the same way; the explicit form is {"lat","lon"} (see ``outlet_pair``).
    return _check_outlet_value(value, tool)


def _check_outlet_value(value: Any, tool: str) -> Optional[dict]:
    if isinstance(value, dict):
        if {"lat", "lon"}.issubset(value):
            return _check_dict(value, tool)
        for v in value.values():                 # data_batch_fetch: {label: outlet}
            err = _check_outlet_value(v, tool)
            if err:
                return err
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(_is_num(v) for v in value):
        err = _pair_error(float(value[0]), float(value[1]), tool)
        for k in ("message", "recovery"):
            err[k] = err[k].replace("geometry", "outlet").replace("bare coordinate pair", "bare outlet pair")
        return err
    return None


def outlet_pair(value: Any) -> Any:
    """``{"lat","lon"}`` -> the ``[lat, lon]`` list aihydro-data takes; anything else unchanged. A mapping of labels is converted member-wise."""
    if isinstance(value, dict):
        if {"lat", "lon"}.issubset(value):
            return [float(value["lat"]), float(value["lon"])]
        return {k: outlet_pair(v) for k, v in value.items()}
    return value


def antimeridian_refusal(exc: Exception, tool: str = "") -> dict:
    span = getattr(exc, "span", None)
    if span and span[0] <= -179.9 and span[1] >= 179.9 and getattr(exc, "fill", 0.0) >= 0.9:
        # A box (the geometry fills >= 90% of its bounding box) running from the western to the eastern limit (-180 .. 180) is not a basin and does not "cross" anything: there is no
        # distance from it to an outlet or gauge worth checking.
        return _envelope(
            "GEOMETRY_ANTIMERIDIAN_UNSUPPORTED",
            f"The geometry spans longitudes {span[0]:g} to {span[1]:g}, nearly the whole globe. That is a world-wide "
            "box, not a basin, so it cannot be compared with an outlet or gauge.",
            "Pass the basin's own geometry, or drop the geometry and give only the gauge or outlet so the place is "
            "resolved from it.",
            tool)
    return _envelope(
        "GEOMETRY_ANTIMERIDIAN_UNSUPPORTED", str(exc),
        "Split the geometry at +/-180 and make one call per side, or pass a geometry that does not cross the antimeridian.",
        tool)


def _tolerance_m() -> float:
    from ai_hydro.mcp.gauge_identity import _mismatch_limit_m

    return _mismatch_limit_m()


def check_outlet_matches_geometry(geometry: Any, outlet: Any, tool: str = "") -> Optional[dict]:
    """``GEOMETRY_OUTLET_MISMATCH`` when ``outlet`` ([lat, lon]) is farther than the A2 5 km tolerance from ``geometry``
    (0 m when inside a polygon). The outlet drives GEOGLOWS / Open-Meteo snapping ahead of the polygon, so an outlet that is
    a different place from the geometry would fetch modelled flow for it.

    Not compared: a per-label mapping of geometries, and a station-id string (it routes to an observation product, not to
    outlet snapping). Anything that cannot be coerced is left to the fetch's own error."""
    if geometry is None or outlet is None:
        return None
    if isinstance(geometry, str) and not geometry.strip().upper().startswith(("POINT", "POLY", "MULTI", "LINE", "GEOMETRYC")):
        return None                                  # a gauge id / JSON string: no geometry to compare with here
    if isinstance(outlet, dict) and not {"lat", "lon"}.issubset(outlet):
        return None                                  # a {label: outlet} mapping (data_batch_fetch): no per-label comparison
    pair = outlet_pair(outlet)
    if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and all(_is_num(v) for v in pair)):
        return None
    try:
        d = _distance_m(geometry, float(pair[0]), float(pair[1]))
    except AntimeridianGeometry as exc:
        return antimeridian_refusal(exc, tool)
    except Exception:
        return None
    tol = _tolerance_m()
    if d <= tol:
        return None
    try:
        from aihydro_data.geometry import coerce_geometry

        c = coerce_geometry(geometry).centroid
        where = {"lat": c.y, "lon": c.x}
    except Exception:
        where = None
    return _envelope(
        "GEOMETRY_OUTLET_MISMATCH",
        f"outlet lat={pair[0]}, lon={pair[1]} is {d / 1000:.0f} km from the requested geometry"
        + (f" (centroid lat={where['lat']:.5f}, lon={where['lon']:.5f})" if where else "")
        + f"; the limit is {tol / 1000:.0f} km (inside the polygon, or within that distance of it). They are not the same place, "
        "so nothing was fetched: modelled streamflow would have been snapped to the outlet.",
        'Resend outlet as {"lat": ..., "lon": ...} at the basin outlet, or drop outlet. A swapped latitude/longitude is the usual cause.',
        tool, distance_m=round(d), geometry_centroid=where, outlet={"lat": pair[0], "lon": pair[1]},
    )


def check_arguments(args: dict, tool: str = "") -> tuple[dict, Optional[dict]]:
    """``(arguments, refusal)`` for a whole call: every coordinate-like argument is checked, and an
    explicit ``{"west","south","east","north"}`` geometry is converted to a GeoJSON Polygon."""
    out = dict(args)
    for key, val in args.items():
        err = check_coordinate_arg(key, val, tool)
        if err:
            return args, err
        if param_kind(key) == "geometry" and isinstance(val, dict) and {"west", "south", "east", "north"}.issubset(val):
            out[key] = _to_polygon(val)
        elif param_kind(key) == "outlet" and val is not None:
            out[key] = outlet_pair(val)
    outlet = out.get("outlet")
    geom = out.get("geometry", out.get("geometries"))
    if outlet is not None and geom is not None:
        err = check_outlet_matches_geometry(geom, outlet, tool)
        if err:
            return args, err
    return out, None


class GeometryBoundaryMiddleware(Middleware):
    """FastMCP middleware: hold every coordinate / geometry argument of every tool to ``check_arguments``."""

    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        msg = context.message
        args = getattr(msg, "arguments", None)
        if isinstance(args, dict):
            fixed, err = check_arguments(args, getattr(msg, "name", "") or "")
            if err:
                raise StructuredToolError(err)
            if fixed != args:
                msg.arguments = fixed
            converted = any(param_kind(k) == "outlet" and v is not None and fixed.get(k) != v
                            for k, v in args.items())
            if converted:
                token = BOUNDARY_CHECKED.set(True)
                try:
                    return await call_next(context)
                finally:
                    BOUNDARY_CHECKED.reset(token)
        return await call_next(context)


def install_geometry_boundary(mcp) -> None:
    """Attach the GeometryBoundaryMiddleware to a FastMCP instance (idempotent)."""
    if getattr(mcp, "_aihydro_geometry_boundary_installed", False):
        return
    mcp.add_middleware(GeometryBoundaryMiddleware())
    mcp._aihydro_geometry_boundary_installed = True
