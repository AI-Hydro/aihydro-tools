"""
Build clipped GeoJSON map layers from local MERIT vector shapefiles.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import geopandas as gpd
from shapely.geometry import box

from ai_hydro.data.merit_manager import MeritDataManager

log = logging.getLogger(__name__)

_MAX_LINE_FEATURES = 25_000


def _clip_bbox(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    *,
    buffer_deg: float = 0.5,
) -> tuple[float, float, float, float]:
    return (
        min_lon - buffer_deg,
        min_lat - buffer_deg,
        max_lon + buffer_deg,
        max_lat + buffer_deg,
    )


def _gdf_to_feature_collection(gdf: gpd.GeoDataFrame) -> dict[str, Any]:
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(4326)
    if len(gdf) > _MAX_LINE_FEATURES:
        gdf = gdf.iloc[:_MAX_LINE_FEATURES].copy()
        log.warning("MERIT layer truncated to %s features", _MAX_LINE_FEATURES)
    try:
        gdf["geometry"] = gdf.geometry.simplify(tolerance=0.0008, preserve_topology=True)
    except Exception:
        pass
    return json.loads(gdf.to_json())


def merit_map_layers_for_view(
    *,
    lat: float,
    lon: float,
    min_lon: float | None = None,
    min_lat: float | None = None,
    max_lon: float | None = None,
    max_lat: float | None = None,
    include_level2: bool = False,
    include_rivers: bool = True,
    include_catchments: bool = False,
    pfaf_codes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Return layer specs ``{id, name, layer_type, geojson, style_preset, metadata}``
    for MapView / push_layer.
    """
    mgr = MeritDataManager()
    if min_lon is None or min_lat is None or max_lon is None or max_lat is None:
        min_lon, min_lat, max_lon, max_lat = _clip_bbox(lon - 0.5, lat - 0.5, lon + 0.5, lat + 0.5)
    else:
        min_lon, min_lat, max_lon, max_lat = _clip_bbox(min_lon, min_lat, max_lon, max_lat)

    clip = box(min_lon, min_lat, max_lon, max_lat)
    layers: list[dict[str, Any]] = []

    if include_level2:
        l2 = mgr.level2_shapefile_path()
        if l2.exists():
            gdf = gpd.read_file(l2)
            if gdf.crs is None:
                gdf = gdf.set_crs(4326)
            gdf = gdf.to_crs(4326)
            clipped = gdf[gdf.geometry.intersects(clip)]
            if not clipped.empty:
                layers.append(
                    {
                        "id": "merit-level2-view",
                        "name": "MERIT Pfaf basins (level 2)",
                        "layer_type": "polygon",
                        "geojson": _gdf_to_feature_collection(clipped),
                        "style_preset": "default",
                        "metadata": {
                            "source": "merit",
                            "merit_layer": "level2",
                        },
                    }
                )

    codes = pfaf_codes or []
    if not codes:
        try:
            codes = [mgr.resolve_pfaf_code(lat, lon)]
        except Exception:
            codes = []

    for pfaf in codes:
        pfaf = str(pfaf).zfill(2)
        if include_rivers:
            riv = mgr.river_shapefile_path(pfaf)
            if riv.exists():
                gdf = gpd.read_file(riv)
                if gdf.crs is None:
                    gdf = gdf.set_crs(4326)
                gdf = gdf.to_crs(4326)
                clipped = gdf[gdf.geometry.intersects(clip)]
                if not clipped.empty:
                    layers.append(
                        {
                            "id": f"merit-rivers-{pfaf}",
                            "name": f"MERIT rivers (Pfaf {pfaf})",
                            "layer_type": "line",
                            "geojson": _gdf_to_feature_collection(clipped),
                            "style_preset": "flowlines",
                            "metadata": {
                                "source": "merit",
                                "merit_layer": "rivers",
                                "pfaf_code": pfaf,
                            },
                        }
                    )
        if include_catchments:
            cat = mgr.catchment_shapefile_path(pfaf)
            if cat.exists():
                gdf = gpd.read_file(cat)
                if gdf.crs is None:
                    gdf = gdf.set_crs(4326)
                gdf = gdf.to_crs(4326)
                clipped = gdf[gdf.geometry.intersects(clip)]
                if not clipped.empty:
                    layers.append(
                        {
                            "id": f"merit-catchments-{pfaf}",
                            "name": f"MERIT catchments (Pfaf {pfaf})",
                            "layer_type": "polygon",
                            "geojson": _gdf_to_feature_collection(clipped),
                            "style_preset": "default",
                            "metadata": {
                                "source": "merit",
                                "merit_layer": "catchments",
                                "pfaf_code": pfaf,
                            },
                        }
                    )

    return layers
