"""Geometry helpers for watershed delineation (ported from HydroCatch, adapted)."""

from __future__ import annotations

from typing import Dict

from affine import Affine
from pyproj import Transformer
from shapely.geometry import Polygon, box

EQUAL_AREA_CRS = "EPSG:6933"


def normalize_affine(tfm) -> Affine:
    if isinstance(tfm, Affine):
        return tfm
    if isinstance(tfm, (tuple, list)) and len(tfm) >= 6:
        return Affine(*tfm[:6])
    raise TypeError(f"Unsupported transform type: {type(tfm)}")


def lonlat_to_utm_epsg(lon: float, lat: float) -> str:
    zone = int((lon + 180) / 6) + 1
    return f"EPSG:{32600 + zone}" if lat >= 0 else f"EPSG:{32700 + zone}"


def meters_to_degrees_bbox(bbox_proj: Polygon, from_crs: str) -> tuple:
    transformer = Transformer.from_crs(from_crs, "EPSG:4326", always_xy=True)
    minx, miny, maxx, maxy = bbox_proj.bounds
    xs = [minx, maxx, maxx, minx]
    ys = [miny, miny, maxy, maxy]
    lonlat = [transformer.transform(x, y) for x, y in zip(xs, ys)]
    lons, lats = zip(*lonlat)
    return (min(lons), min(lats), max(lons), max(lats))


def square_bbox_proj(lat: float, lon: float, half_side_km: float, proj: str) -> Polygon:
    t_to_proj = Transformer.from_crs("EPSG:4326", proj, always_xy=True)
    x0, y0 = t_to_proj.transform(lon, lat)
    d = half_side_km * 1000.0
    return box(x0 - d, y0 - d, x0 + d, y0 + d)


def edge_strips_from_bounds(bounds: tuple, pixel_size: float, n_cells: int) -> Dict[str, Polygon]:
    xmin, ymin, xmax, ymax = bounds
    m = pixel_size * n_cells
    return {
        "left": box(xmin, ymin, xmin + m, ymax),
        "right": box(xmax - m, ymin, xmax, ymax),
        "bottom": box(xmin, ymin, xmax, ymin + m),
        "top": box(xmin, ymax - m, xmax, ymax),
    }
