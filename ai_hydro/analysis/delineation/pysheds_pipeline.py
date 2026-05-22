"""
Coarse-to-fine pysheds D8 delineation on cloud DEM (MERIT-Hydro / Copernicus).

Ported from HydroCatch core.py with logging instead of print.
"""

from __future__ import annotations

import logging
import os
import tempfile
import geopandas as gpd
import numpy as np
import xarray as xr
from pysheds.grid import Grid
from shapely.geometry import Point, box, shape

from ai_hydro.analysis.delineation.dem_conditioning import prepare_dem_array
from ai_hydro.analysis.delineation.dem_fetch import StacItemCache, fetch_dem_bbox
from ai_hydro.analysis.delineation.merit_snap import snap_outlet_to_merit_rivers
from ai_hydro.analysis.delineation.nldi_point import is_conus, nldi_basin_gdf, snap_outlet_nldi
from ai_hydro.analysis.delineation.types import FastDelineationResult, area_km2
from ai_hydro.analysis.delineation.utils import (
    edge_strips_from_bounds,
    lonlat_to_utm_epsg,
    normalize_affine,
    square_bbox_proj,
)

log = logging.getLogger(__name__)

DEFAULT_RESOLUTION_M = 30.0
COARSE_RESOLUTION_M = 90.0
SCOUT_BOX_KM = 150.0
EDGE_MARGIN_CELLS = 3
MAX_SCOUT_BOX_KM = 1000.0
DEM_COLLECTION = "nasadem"
DEM_ASSET_KEY = "elevation"
COPERNICUS_COLLECTION = "copernicus"
_AREA_MISMATCH_FRAC = 0.25


def _nldi_area_matches(expected_area_km2: float | None, nldi_area: float) -> bool:
    if not expected_area_km2 or expected_area_km2 <= 0:
        # Nearest COMID without a target area often lands on a tributary (few km²).
        return False
    return abs(nldi_area - expected_area_km2) / expected_area_km2 <= _AREA_MISMATCH_FRAC


# Backward-compatible alias
def nldi_snap(lat: float, lon: float) -> tuple[float, float, bool]:
    """Snap to USGS NLDI flowline (CONUS only) via pynhd."""
    return snap_outlet_nldi(lat, lon)


def _stream_threshold_cells(pixel_size_m: float) -> int:
    """Minimum upstream cells to treat as a stream (~1 km² contributing area)."""
    cell_area_m2 = max(pixel_size_m**2, 1.0)
    target_area_m2 = 1e6
    return max(50, int(target_area_m2 / cell_area_m2))


def _search_radius_m(expected_area_km2: float | None, pixel_size_m: float) -> float:
    if expected_area_km2 and expected_area_km2 > 0:
        return min(12_000.0, max(1_500.0, 400.0 * float(np.sqrt(expected_area_km2))))
    return 3_000.0


def _snap_outlet_on_dem(
    grid: Grid,
    acc: np.ndarray,
    stream_mask: np.ndarray,
    x: float,
    y: float,
    *,
    expected_area_km2: float | None,
) -> tuple[float, float]:
    """
    Snap outlet to the highest-flow-accumulation stream cell within a distance
    budget (avoids nearest-tributary mistakes and continent-scale argmax snaps).
    """
    col, row = ~grid.affine * (x, y)
    row, col = int(round(row)), int(round(col))
    pixel_size_m = max(abs(grid.affine.a), abs(grid.affine.e), 1.0)
    rows, cols = np.indices(acc.shape)
    dist_m = np.sqrt((rows - row) ** 2 + (cols - col) ** 2) * pixel_size_m
    search_m = _search_radius_m(expected_area_km2, pixel_size_m)

    candidates = stream_mask & (dist_m <= search_m)
    if not candidates.any():
        candidates = dist_m <= search_m

    if expected_area_km2 and expected_area_km2 > 0:
        target_cells = expected_area_km2 * 1e6 / (pixel_size_m**2)
        acc_err = np.abs(acc - target_cells) / max(target_cells, 1.0)
        dist_w = dist_m / max(search_m, 1.0)
        score = acc_err + 0.35 * dist_w
        score = np.where(candidates, score, np.inf)
        snap_r, snap_c = np.unravel_index(int(np.argmin(score)), score.shape)
    else:
        acc_weighted = np.where(candidates, acc, -1.0)
        snap_r, snap_c = np.unravel_index(int(np.argmax(acc_weighted)), acc.shape)
    return grid.affine * (snap_c, snap_r)


def delineate_watershed_from_array(
    dem_da: xr.DataArray,
    latitude: float,
    longitude: float,
    expected_area_km2: float | None = None,
) -> gpd.GeoDataFrame:
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        temp_dem_path = tmp.name
    try:
        dem_arr, _dem_meta = prepare_dem_array(dem_da.values)
        dem_da = dem_da.copy(data=dem_arr)
        dem_da.rio.to_raster(temp_dem_path)
        grid = Grid.from_raster(temp_dem_path)
        dem_data = grid.read_raster(temp_dem_path)
        grid.affine = normalize_affine(getattr(grid, "affine", dem_da.rio.transform()))
        grid.transform = grid.affine

        pour = gpd.GeoSeries([Point(longitude, latitude)], crs=4326).to_crs(dem_da.rio.crs)
        x_pour, y_pour = pour.iloc[0].x, pour.iloc[0].y

        pit = grid.fill_pits(dem_data)
        dep = grid.fill_depressions(pit)
        flat = grid.resolve_flats(dep)
        dirmap = (64, 128, 1, 2, 4, 8, 16, 32)
        fdir = grid.flowdir(flat, dirmap=dirmap)
        acc = grid.accumulation(fdir, dirmap=dirmap)

        pixel_size_m = max(abs(grid.affine.a), abs(grid.affine.e), 1.0)
        stream_mask = acc >= _stream_threshold_cells(pixel_size_m)
        x_snap, y_snap = _snap_outlet_on_dem(
            grid,
            acc,
            stream_mask,
            x_pour,
            y_pour,
            expected_area_km2=expected_area_km2,
        )

        catch = grid.catchment(
            x=x_snap, y=y_snap, fdir=fdir, dirmap=dirmap, xytype="coordinate"
        )
        grid.clip_to(catch)
        mask = grid.view(catch).astype(np.uint8)
        geoms = list(grid.polygonize(mask))
        polys = [shape(g[0]) for g in geoms]
        if not polys:
            return gpd.GeoDataFrame(
                {"catchment": [0]}, geometry=[None], crs=dem_da.rio.crs
            ).to_crs(4326)

        ws = gpd.GeoDataFrame({"catchment": [1] * len(polys)}, geometry=polys, crs=dem_da.rio.crs)
        return ws.dissolve(by="catchment").to_crs(4326)
    finally:
        if os.path.exists(temp_dem_path):
            os.remove(temp_dem_path)


def delineate_fast(
    lat: float,
    lon: float,
    *,
    expected_area_km2: float | None = None,
    verbose: bool = False,
    use_merit_vector_snap: bool = True,
    use_nldi_snap: bool = True,
) -> FastDelineationResult:
    """Coarse-to-fine cloud DEM delineation."""
    merit_snap_m: float | None = None
    pfaf: str | None = None

    nldi_gdf: gpd.GeoDataFrame | None = None
    if use_nldi_snap and is_conus(lat, lon):
        lat, lon, nldi_ok = snap_outlet_nldi(lat, lon)
        if verbose and nldi_ok:
            log.info("Snapped outlet to NLDI flowline (pynhd)")
        if expected_area_km2 and expected_area_km2 > 0:
            nldi_gdf = nldi_basin_gdf(lat, lon, expected_area_km2=expected_area_km2)

    if use_merit_vector_snap:
        snap = snap_outlet_to_merit_rivers(lat, lon)
        if snap.success:
            lat, lon = snap.lat, snap.lon
            merit_snap_m = snap.distance_m
            pfaf = snap.pfaf_code
            if verbose:
                log.info("MERIT vector snap: %.0f m (basin %s)", merit_snap_m, pfaf)

    if nldi_gdf is not None and _nldi_area_matches(expected_area_km2, area_km2(nldi_gdf)):
        a = area_km2(nldi_gdf)
        if verbose:
            log.info("Using NLDI indexed basin (%.2f km²)", a)
        return FastDelineationResult(
            gdf=nldi_gdf,
            area_km2=a,
            scout_box_maxed=False,
            outlet_lat=lat,
            outlet_lon=lon,
            merit_snap_distance_m=merit_snap_m,
            pfaf_code=pfaf,
            used_nldi_basin=True,
        )
    if nldi_gdf is not None and verbose:
        log.info(
            "NLDI basin area %.1f km² mismatches expected %s; using DEM delineation",
            area_km2(nldi_gdf),
            expected_area_km2,
        )

    utm_crs = lonlat_to_utm_epsg(lon, lat)
    item_cache = StacItemCache()
    current_box_km = SCOUT_BOX_KM
    scout_maxed = False
    ws_coarse = None

    while current_box_km <= MAX_SCOUT_BOX_KM:
        if verbose:
            log.info("Scout %.0f km @ %sm", current_box_km, COARSE_RESOLUTION_M)
        bbox_proj = square_bbox_proj(lat, lon, current_box_km, utm_crs)
        try:
            dem_coarse = fetch_dem_bbox(
                bbox_proj,
                utm_crs,
                COARSE_RESOLUTION_M,
                item_cache,
                collection=DEM_COLLECTION,
                asset_key=DEM_ASSET_KEY,
                verbose=verbose,
            )
        except Exception as e:
            log.warning("Primary DEM failed (%s); Copernicus fallback", e)
            dem_coarse = fetch_dem_bbox(
                bbox_proj,
                utm_crs,
                COARSE_RESOLUTION_M,
                item_cache,
                collection=COPERNICUS_COLLECTION,
                verbose=verbose,
            )

        ws_coarse = delineate_watershed_from_array(
            dem_coarse, lat, lon, expected_area_km2=expected_area_km2
        )
        if ws_coarse.empty or ws_coarse.geometry.is_empty.all():
            raise RuntimeError(
                "Could not delineate watershed at coarse scale. Check outlet coordinates."
            )

        ws_coarse_proj = ws_coarse.to_crs(utm_crs)
        xmin, ymin, xmax, ymax = dem_coarse.rio.bounds()
        tfm = dem_coarse.rio.transform()
        pixel_size = max(abs(tfm.a), abs(tfm.e))
        strips = edge_strips_from_bounds((xmin, ymin, xmax, ymax), pixel_size, EDGE_MARGIN_CELLS)
        edges_touched = {
            name: (
                not ws_coarse_proj.is_empty.all() and ws_coarse_proj.intersects(poly).any()
            )
            for name, poly in strips.items()
        }
        if any(edges_touched.values()):
            current_box_km *= 1.5
        else:
            break
    else:
        scout_maxed = True
        log.warning("Scout box reached maximum size; watershed may be truncated")

    ws_coarse_proj = ws_coarse.to_crs(utm_crs)
    minx, miny, maxx, maxy = ws_coarse_proj.total_bounds
    margin = 20000.0
    refined_bbox = box(minx - margin, miny - margin, maxx + margin, maxy + margin)

    try:
        dem_fine = fetch_dem_bbox(
            refined_bbox,
            utm_crs,
            DEFAULT_RESOLUTION_M,
            item_cache,
            collection=DEM_COLLECTION,
            asset_key=DEM_ASSET_KEY,
            verbose=verbose,
        )
    except Exception as e:
        log.warning("Fine DEM primary failed (%s); Copernicus fallback", e)
        dem_fine = fetch_dem_bbox(
            refined_bbox,
            utm_crs,
            DEFAULT_RESOLUTION_M,
            item_cache,
            collection=COPERNICUS_COLLECTION,
            verbose=verbose,
        )

    ws_final = delineate_watershed_from_array(
        dem_fine, lat, lon, expected_area_km2=expected_area_km2
    )
    a = area_km2(ws_final)
    if verbose:
        log.info("Fast delineation area: %.2f km²", a)

    return FastDelineationResult(
        gdf=ws_final,
        area_km2=a,
        scout_box_maxed=scout_maxed,
        outlet_lat=lat,
        outlet_lon=lon,
        merit_snap_distance_m=merit_snap_m,
        pfaf_code=pfaf,
    )
