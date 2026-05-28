#!/usr/bin/env python3
"""
Diagnose fast-tier delineation errors: COMID, snap strategy, DEM source, conditioning.

Usage:
  python scripts/experiment_delineation_snap.py
  python scripts/experiment_delineation_snap.py --gauge 01031500
"""

from __future__ import annotations

import argparse
import sys

BASINS = [
    {"id": "11141280", "name": "Lopez", "lat": 35.03, "lon": -120.48, "pub": 54.13},
    {"id": "12045500", "name": "Elwha", "lat": 48.09, "lon": -123.55, "pub": 697.0},
    {"id": "01031500", "name": "Mattawamkeag", "lat": 45.87, "lon": -68.33, "pub": 774.0},
    {"id": "03182500", "name": "Greenbrier", "lat": 37.78, "lon": -80.30, "pub": 1396.0},
    {"id": "02361000", "name": "Sepulga", "lat": 31.48, "lon": -86.87, "pub": None},
    {"id": "05413500", "name": "Iowa R Wapello", "lat": 41.19, "lon": -91.37, "pub": 27900.0},
    {"id": "01646500", "name": "Potomac Seneca", "lat": 39.07, "lon": -77.35, "pub": 348.0},
]


def _pct_err(actual: float, expected: float) -> str:
    if not expected:
        return "—"
    return f"{100 * abs(actual - expected) / expected:.0f}%"


def diagnose_basin(b: dict) -> None:
    from pynhd import NLDI

    from ai_hydro.analysis.delineation.nldi_point import nldi_basin_gdf, snap_outlet_nldi
    from ai_hydro.analysis.delineation.pysheds_pipeline import delineate_watershed_from_array
    from ai_hydro.analysis.delineation.dem_fetch import StacItemCache, fetch_dem_bbox
    from ai_hydro.analysis.delineation.types import area_km2
    from ai_hydro.analysis.delineation.utils import lonlat_to_utm_epsg, square_bbox_proj, normalize_affine
    from ai_hydro.analysis.watershed import delineate_watershed
    from pysheds.grid import Grid
    import geopandas as gpd
    import numpy as np
    import os
    import tempfile
    from shapely.geometry import Point

    gid, name, lat, lon, pub = b["id"], b["name"], b["lat"], b["lon"], b.get("pub")
    print(f"\n{'='*72}\n{name} ({gid})  pub={pub} km²  coords=({lat}, {lon})")

    # Gauge NLDI reference
    try:
        g_ref = delineate_watershed(gid).data["area_km2"]
        print(f"  gauge delineate_watershed: {g_ref:.0f} km²")
    except Exception as e:
        g_ref = None
        print(f"  gauge delineate_watershed: FAIL {e}")

    nldi = NLDI()
    for label, la, lo in [("pour", lat, lon)]:
        df = nldi.comid_byloc((lo, la))
        comid = int(df.comid.iloc[0]) if not df.empty else None
        basins = nldi.get_basins(comid, fsource="comid") if comid else None
        a_nldi = area_km2(basins.dissolve()) if basins is not None and not basins.empty else None
        a_s = f"{a_nldi:.0f}" if a_nldi else "—"
        print(f"  NLDI @ {label}: COMID={comid} area={a_s} km² err={_pct_err(a_nldi or 0, pub or g_ref or 0)}")

    lat_n, lon_n, _ = snap_outlet_nldi(lat, lon)
    print(f"  pynhd snap: ({lat_n:.5f}, {lon_n:.5f})")

    # DEM routing diagnostics @ 30m
    utm = lonlat_to_utm_epsg(lon_n, lat_n)
    box_km = 80 if (pub or 0) < 500 else 150
    for dem_name, coll, asset in [
        ("nasadem", "nasadem", "elevation"),
        ("copernicus", "copernicus", None),
    ]:
        try:
            dem = fetch_dem_bbox(
                square_bbox_proj(lat_n, lon_n, box_km, utm),
                utm,
                30,
                StacItemCache(),
                collection=coll,
                asset_key=asset,
            )
        except Exception as e:
            print(f"  {dem_name}: fetch failed {e}")
            continue

        arr = dem.values.astype(np.float32)
        n_nan = int(np.isnan(arr).sum())
        n_total = arr.size
        print(
            f"  {dem_name}: shape={arr.shape} nan_frac={n_nan/n_total:.2%} "
            f"elev_range={np.nanmin(arr):.0f}..{np.nanmax(arr):.0f}m"
        )

        for snap_mode in ("current", "area_target", "nearest_stream"):
            try:
                a = _dem_area_one(
                    dem, lat_n, lon_n, pub, snap_mode=snap_mode
                )
                print(
                    f"    snap={snap_mode:14s} area={a:>8.0f} km²  err={_pct_err(a, pub or g_ref or 0)}"
                )
            except Exception as e:
                print(f"    snap={snap_mode:14s} FAIL {e}")


def _dem_area_one(dem, lat, lon, expected_km2, *, snap_mode: str) -> float:
    """Single DEM delineation with alternate snap modes."""
    import os
    import tempfile

    import geopandas as gpd
    import numpy as np
    import xarray as xr
    from pysheds.grid import Grid
    from shapely.geometry import Point, shape

    from ai_hydro.analysis.delineation.pysheds_pipeline import (
        _search_radius_m,
        _snap_outlet_on_dem,
        _stream_threshold_cells,
    )
    from ai_hydro.analysis.delineation.types import area_km2
    from ai_hydro.analysis.delineation.utils import normalize_affine

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        path = tmp.name
    try:
        dem_arr = dem.values.astype(np.float32)
        dem_arr = np.where(np.isfinite(dem_arr), dem_arr, np.nan)
        dem_arr = np.where(dem_arr < -500, np.nan, dem_arr)
        fill = float(np.nanmedian(dem_arr))
        dem_arr = np.where(np.isnan(dem_arr), fill, dem_arr)
        dem = dem.copy(data=dem_arr)
        dem.rio.to_raster(path)
        grid = Grid.from_raster(path)
        dem_data = grid.read_raster(path)
        grid.affine = normalize_affine(getattr(grid, "affine", dem.rio.transform()))

        pour = gpd.GeoSeries([Point(lon, lat)], crs=4326).to_crs(dem.rio.crs)
        x, y = pour.iloc[0].x, pour.iloc[0].y
        pit = grid.fill_pits(dem_data)
        dep = grid.fill_depressions(pit)
        flat = grid.resolve_flats(dep)
        dirmap = (64, 128, 1, 2, 4, 8, 16, 32)
        fdir = grid.flowdir(flat, dirmap=dirmap)
        acc = grid.accumulation(fdir, dirmap=dirmap)
        px = max(abs(grid.affine.a), abs(grid.affine.e), 1.0)
        stream = acc >= _stream_threshold_cells(px)

        if snap_mode == "current":
            x_s, y_s = _snap_outlet_on_dem(
                grid, acc, stream, x, y, expected_area_km2=expected_km2
            )
        elif snap_mode == "area_target" and expected_km2 and expected_km2 > 0:
            target_cells = expected_km2 * 1e6 / (px * px)
            col, row = ~grid.affine * (x, y)
            row, col = int(round(row)), int(round(col))
            rows, cols = np.indices(acc.shape)
            dist_m = np.sqrt((rows - row) ** 2 + (cols - col) ** 2) * px
            search_m = _search_radius_m(expected_km2, px)
            cand = stream & (dist_m <= search_m)
            if not cand.any():
                cand = dist_m <= search_m
            acc_c = np.where(cand, acc, np.nan)
            err = np.abs(acc_c - target_cells)
            err[~np.isfinite(err)] = np.inf
            snap_r, snap_c = np.unravel_index(int(np.nanargmin(err)), acc.shape)
            x_s, y_s = grid.affine * (snap_c, snap_r)
        else:
            dist = np.where(stream, np.inf, 0)
            col, row = ~grid.affine * (x, y)
            row, col = int(round(row)), int(round(col))
            rows, cols = np.indices(acc.shape)
            d = np.sqrt((rows - row) ** 2 + (cols - col) ** 2)
            d[~stream] = np.inf
            snap_r, snap_c = np.unravel_index(int(np.argmin(d)), d.shape)
            x_s, y_s = grid.affine * (snap_c, snap_r)

        catch = grid.catchment(x=x_s, y=y_s, fdir=fdir, dirmap=dirmap, xytype="coordinate")
        grid.clip_to(catch)
        mask = grid.view(catch).astype(np.uint8)
        geoms = [shape(g[0]) for g in grid.polygonize(mask)]
        if not geoms:
            return 0.0
        gdf = gpd.GeoDataFrame(geometry=geoms, crs=dem.rio.crs).to_crs(4326)
        return area_km2(gdf.dissolve())
    finally:
        if os.path.exists(path):
            os.remove(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gauge", type=str, default="")
    args = parser.parse_args()
    basins = [x for x in BASINS if x["id"] == args.gauge] if args.gauge else BASINS[:5]
    for b in basins:
        diagnose_basin(b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
