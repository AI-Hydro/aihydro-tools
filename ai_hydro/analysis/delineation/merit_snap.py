"""MERIT-Hydro vector river snapping for outlet alignment."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import geopandas as gpd
import pyproj
from shapely.geometry import Point
from shapely.ops import nearest_points

if TYPE_CHECKING:
    from ai_hydro.data.merit_manager import MeritDataManager

log = logging.getLogger(__name__)


@dataclass
class MeritSnapResult:
    success: bool
    lat: float
    lon: float
    distance_m: float | None = None
    pfaf_code: str | None = None
    message: str = ""


def get_pfaf_code(lat: float, lon: float, manager: MeritDataManager) -> str:
    level2_path = manager.level2_shapefile_path()
    if not level2_path.exists():
        raise FileNotFoundError(f"MERIT level-2 basins not found: {level2_path}")
    gdf = gpd.read_file(level2_path)
    point = Point(lon, lat)
    subset = gdf[gdf.geometry.contains(point)]
    if subset.empty:
        raise ValueError("Point not found within any MERIT Level 2 basin.")
    row = subset.iloc[0]
    pfaf = str(
        row.get("pfaf_code")
        or row.get("PFAF_ID")
        or row.get("BASIN")
        or row.get("basin")
        or ""
    )
    return str(int(pfaf)).zfill(2)


def load_river_network(pfaf_code: str, manager: MeritDataManager) -> gpd.GeoDataFrame:
    river_path = manager.river_shapefile_path(pfaf_code)
    if not river_path.exists():
        raise FileNotFoundError(f"River network not found: {river_path}")
    return gpd.read_file(river_path)


def snap_to_river_network(
    lat: float, lon: float, river_gdf: gpd.GeoDataFrame
) -> tuple[float, float, float]:
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    utm_crs = pyproj.CRS.from_epsg(epsg)

    point = Point(lon, lat)
    point_gdf = gpd.GeoDataFrame([1], geometry=[point], crs="EPSG:4326").to_crs(utm_crs)
    river_proj = river_gdf.to_crs(utm_crs)
    nearest_line = river_proj.geometry.iloc[
        river_proj.distance(point_gdf.geometry.iloc[0]).idxmin()
    ]
    snapped_point = nearest_points(point_gdf.geometry.iloc[0], nearest_line)[1]
    distance_m = float(point_gdf.geometry.iloc[0].distance(snapped_point))
    snapped_gdf = gpd.GeoDataFrame([1], geometry=[snapped_point], crs=utm_crs).to_crs(
        "EPSG:4326"
    )
    return (
        float(snapped_gdf.geometry.iloc[0].y),
        float(snapped_gdf.geometry.iloc[0].x),
        distance_m,
    )


def snap_outlet_to_merit_rivers(lat: float, lon: float) -> MeritSnapResult:
    from ai_hydro.data.merit_manager import MeritDataManager

    manager = MeritDataManager()
    try:
        status = manager.ensure_basin(lat, lon, download=False)
        pfaf = status.pfaf_code
        if not manager.river_shapefile_path(pfaf).exists():
            return MeritSnapResult(
                success=False,
                lat=lat,
                lon=lon,
                pfaf_code=pfaf,
                message=status.message or "MERIT river vectors not installed for this basin.",
            )
        rivers = load_river_network(pfaf, manager)
        new_lat, new_lon, dist = snap_to_river_network(lat, lon, rivers)
        return MeritSnapResult(
            success=True,
            lat=new_lat,
            lon=new_lon,
            distance_m=dist,
            pfaf_code=pfaf,
            message="snapped",
        )
    except Exception as e:
        log.debug("MERIT vector snap unavailable: %s", e)
        return MeritSnapResult(
            success=False,
            lat=lat,
            lon=lon,
            message=str(e),
        )
