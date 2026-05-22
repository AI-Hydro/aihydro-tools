"""
Build clipped WBD (HUC) GeoJSON map layers for CONUS via pygeohydro.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import geopandas as gpd
from shapely.geometry import Point, box

log = logging.getLogger(__name__)

# Approximate CONUS extent (same policy as AI-Hydro map guards).
_CONUS = box(-125.0, 24.0, -66.0, 50.0)

_VALID_HUC_LEVELS = (2, 4, 6, 8, 10, 12)
_DEFAULT_HUC_LEVEL = 8
_MAX_POLYGON_FEATURES = 800


def is_conus_point(lat: float, lon: float) -> bool:
    return bool(_CONUS.contains(Point(lon, lat)))


def is_conus_bbox(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> bool:
    view = box(min_lon, min_lat, max_lon, max_lat)
    return bool(_CONUS.intersects(view))


def _normalize_huc_level(huc_level: int | None) -> int:
    if huc_level in _VALID_HUC_LEVELS:
        return int(huc_level)
    return _DEFAULT_HUC_LEVEL


def _gdf_to_feature_collection(gdf: gpd.GeoDataFrame) -> dict[str, Any]:
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(4326)
    if len(gdf) > _MAX_POLYGON_FEATURES:
        gdf = gdf.iloc[:_MAX_POLYGON_FEATURES].copy()
        log.warning("WBD layer truncated to %s features", _MAX_POLYGON_FEATURES)
    try:
        gdf["geometry"] = gdf.geometry.simplify(tolerance=0.001, preserve_topology=True)
    except Exception:
        pass
    return json.loads(gdf.to_json())


def _huc_code_from_row(row: Any, huc_level: int) -> str:
    cols = row.index if hasattr(row, "index") else []
    for key in (f"huc{huc_level}", f"HUC{huc_level}", "huc8", "HUC8", "huc_cd"):
        if key in cols:
            val = row.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    return ""


def _huc_name_from_row(row: Any) -> str:
    cols = row.index if hasattr(row, "index") else []
    for key in ("name", "NAME", "huc_name", "HU_NAME"):
        if key in cols:
            val = row.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    return ""


def wbd_map_layers_for_view(
    *,
    lat: float,
    lon: float,
    min_lon: float | None = None,
    min_lat: float | None = None,
    max_lon: float | None = None,
    max_lat: float | None = None,
    huc_level: int | None = None,
) -> list[dict[str, Any]]:
    """
    Return layer specs for MapView / push_layer (WBD polygons clipped to view).
    """
    if not is_conus_point(lat, lon):
        return []

    if min_lon is None or min_lat is None or max_lon is None or max_lat is None:
        min_lon, min_lat, max_lon, max_lat = lon - 0.5, lat - 0.5, lon + 0.5, lat + 0.5

    if not is_conus_bbox(min_lon, min_lat, max_lon, max_lat):
        return []

    level = _normalize_huc_level(huc_level)
    layer_key = f"huc{level}"

    try:
        from pygeohydro import WBD
    except ImportError as e:
        raise ImportError("pygeohydro is required for WBD layers. Install with: pip install pygeohydro") from e

    wbd = WBD(layer_key)
    bbox = (min_lon, min_lat, max_lon, max_lat)
    try:
        gdf = wbd.bygeom(bbox, geo_crs=4326)
    except Exception as e:
        log.warning("WBD query failed for %s: %s", bbox, e)
        return []

    if gdf is None or gdf.empty:
        return []

    return [
        {
            "id": f"wbd-{layer_key}-view",
            "name": f"WBD hydrologic units (HUC{level})",
            "layer_type": "polygon",
            "geojson": _gdf_to_feature_collection(gdf),
            "style_preset": "huc",
            "metadata": {
                "source": "wbd",
                "huc_level": str(level),
                "wbd_layer": layer_key,
            },
        }
    ]


def huc_at_point(
    lat: float,
    lon: float,
    *,
    huc_level: int | None = None,
) -> dict[str, Any] | None:
    """Return containing HUC code and name for a point (CONUS only)."""
    if not is_conus_point(lat, lon):
        return None

    level = _normalize_huc_level(huc_level)
    layer_key = f"huc{level}"

    try:
        from pygeohydro import WBD
    except ImportError:
        return None

    wbd = WBD(layer_key)
    try:
        gdf = wbd.bygeom(Point(lon, lat), geo_crs=4326, predicate="intersects")
    except Exception as e:
        log.warning("huc_at_point failed: %s", e)
        return None

    if gdf is None or gdf.empty:
        return None

    # Prefer the smallest polygon when the lookup bbox catches neighbors.
    if "areasqkm" in gdf.columns:
        row = gdf.loc[gdf["areasqkm"].astype(float).idxmin()]
    else:
        row = gdf.iloc[0]
    code = _huc_code_from_row(row, level)
    name = _huc_name_from_row(row)
    label = f"HUC{level} {code}" + (f" — {name}" if name else "")
    return {
        "huc_level": level,
        "huc_code": code,
        "huc_name": name,
        "label": label.strip(),
    }
