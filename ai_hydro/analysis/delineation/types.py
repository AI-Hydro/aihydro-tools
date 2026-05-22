"""Shared types for delineation tiers."""

from __future__ import annotations

from typing import NamedTuple

import geopandas as gpd

from ai_hydro.analysis.delineation.utils import EQUAL_AREA_CRS


class FastDelineationResult(NamedTuple):
    gdf: gpd.GeoDataFrame
    area_km2: float
    scout_box_maxed: bool
    outlet_lat: float
    outlet_lon: float
    merit_snap_distance_m: float | None
    pfaf_code: str | None
    used_nldi_basin: bool = False


def area_km2(gdf: gpd.GeoDataFrame) -> float:
    if gdf.empty or gdf.geometry.is_empty.all():
        return 0.0
    return float(gdf.to_crs(EQUAL_AREA_CRS).area.sum() / 1e6)
