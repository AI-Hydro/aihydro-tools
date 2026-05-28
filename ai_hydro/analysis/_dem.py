"""
Shared DEM-fetch helpers for analysis modules.

Provides a single global DEM-fetching path that:

1.  Inside CONUS, prefers ``py3dep`` (USGS 3DEP, 10–30 m, no auth) because it
    is faster and higher resolution than any global alternative.
2.  Anywhere else on Earth, falls back to ``aihydro_data.fetch("dem", …)`` —
    which currently serves Copernicus GLO-30 (global, 30 m, via GEE) and
    will gain MERIT-Hydro fallbacks over time.

The function returns a ``rioxarray``-compatible ``xr.DataArray`` so callers
(``twi.py``, ``geomorphic.py``) can keep using rioxarray-style operations
(``.rio.reproject``, ``.rio.to_raster``, ``.rio.bounds``, …) without
branching on the data source.

This is the Wave 2.5 Axis 3 enabler — wiring the analysis layer to
aihydro-data so that every analysis tool inherits the same global coverage
the delineator already has.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# ── CONUS bounding box (loose) ────────────────────────────────────────────
# Includes Alaska/Hawaii implicitly via the longitude span; the 3DEP service
# itself returns an HTTP error outside its actual coverage, which we trap
# and fall back from. So a slightly oversized bbox is safe.
_CONUS_BBOX = (-125.0, 24.0, -66.5, 50.0)


def _geom_in_conus(geometry: Any) -> bool:
    """Best-effort check whether *geometry*'s centroid lies inside CONUS.

    Accepts shapely geometry, GeoDataFrame, GeoJSON dict, ``(lat, lon)`` tuple,
    or a bbox 4-tuple. Returns False on any parsing failure (safer — picks
    the global path).
    """
    try:
        lat, lon = _centroid_latlon(geometry)
        west, south, east, north = _CONUS_BBOX
        return (west <= lon <= east) and (south <= lat <= north)
    except Exception:
        return False


def _centroid_latlon(geometry: Any) -> tuple[float, float]:
    """Best-effort centroid extraction. Returns (lat, lon)."""
    # shapely geometry
    if hasattr(geometry, "centroid") and hasattr(geometry, "geom_type"):
        c = geometry.centroid
        return float(c.y), float(c.x)
    # GeoDataFrame
    try:
        import geopandas as gpd  # local import to avoid hard dep at module load
        if isinstance(geometry, gpd.GeoDataFrame):
            c = geometry.geometry.unary_union.centroid
            return float(c.y), float(c.x)
    except Exception:
        pass
    # GeoJSON dict
    if isinstance(geometry, dict):
        from shapely.geometry import shape
        g = geometry.get("geometry", geometry)
        c = shape(g).centroid
        return float(c.y), float(c.x)
    # (lat, lon) tuple
    if isinstance(geometry, (tuple, list)) and len(geometry) == 2:
        return float(geometry[0]), float(geometry[1])
    # bbox (west, south, east, north)
    if isinstance(geometry, (tuple, list)) and len(geometry) == 4:
        west, south, east, north = geometry
        return float((south + north) / 2), float((west + east) / 2)
    raise ValueError(f"Cannot extract centroid from geometry of type {type(geometry).__name__}")


def fetch_dem(
    geometry: Any,
    resolution: int = 30,
    prefer: str = "auto",
) -> "xr.DataArray":  # type: ignore[name-defined]
    """Fetch a DEM clipped to *geometry* and return an ``xr.DataArray``.

    Parameters
    ----------
    geometry : shapely / GeoDataFrame / GeoJSON dict / (lat,lon) / bbox
        Watershed boundary (or point) the DEM should cover.
    resolution : int
        Target resolution in metres. py3dep honours this faithfully; the
        global path returns Copernicus GLO-30 at ~30 m regardless.
    prefer : {"auto", "py3dep", "aihydro_data"}
        ``auto`` picks py3dep inside CONUS, aihydro-data elsewhere.
        ``py3dep`` and ``aihydro_data`` force a backend (used by callers
        that need to retry after one path fails).

    Returns
    -------
    xr.DataArray
        Elevation raster in WGS84 by default. Always carries CRS metadata
        via rioxarray's ``.rio`` accessor so downstream reprojection works.

    Raises
    ------
    RuntimeError
        Both backends failed. The error message lists the underlying causes.
    """
    errors: list[str] = []

    use_py3dep = prefer == "py3dep" or (prefer == "auto" and _geom_in_conus(geometry))

    if use_py3dep:
        try:
            import py3dep
            # py3dep accepts shapely or GeoDataFrame directly
            geom_arg = _to_shapely(geometry)
            dem = py3dep.get_dem(geom_arg, resolution=resolution)
            log.info("DEM fetched via py3dep (USGS 3DEP, %d m)", resolution)
            return dem, "3DEP", "hyriver"
        except Exception as exc:
            errors.append(f"py3dep: {exc}")
            log.warning("py3dep DEM fetch failed (%s); falling back to aihydro-data", exc)

    # Global path — aihydro-data
    try:
        from aihydro_data import fetch as _adata_fetch
        geom_arg = _to_shapely(geometry)
        result = _adata_fetch("dem", geom_arg, "", "")
        dem = result.data  # xr.DataArray from gee/stac backend
        dem = _normalize_dem(dem)
        log.info(
            "DEM fetched via aihydro-data (%s, source=%s)",
            result.product, result.source,
        )
        return dem, result.product, result.source
    except Exception as exc:
        errors.append(f"aihydro-data: {exc}")

    raise RuntimeError(
        "Could not fetch a DEM for the requested geometry. "
        "Tried: " + " | ".join(errors)
    )


def _normalize_dem(dem: "xr.DataArray") -> "xr.DataArray":  # type: ignore[name-defined]
    """Normalise a DEM DataArray from any backend so downstream code never
    has to worry about dimension naming, CRS, or band dimensions.

    Three transformations applied in order:

    1. **Rename latitude/longitude → y/x** — rioxarray requires 'x'/'y';
       GEE and STAC backends sometimes return 'lat'/'lon' or
       'latitude'/'longitude'.

    2. **Squeeze singleton band dimension** — GEE rasters often carry an
       extra leading size-1 band axis: (1, H, W) → (H, W).  pysheds and
       np.gradient both need a 2-D array.

    3. **Write CRS if absent** — GEE does not always attach spatial_ref;
       assume EPSG:4326 (all global DEM products are delivered in WGS84).
    """
    import xarray as xr

    # 1. Rename non-standard dimension names
    dim_renames: dict[str, str] = {}
    for old, new in (("latitude", "y"), ("longitude", "x"), ("lat", "y"), ("lon", "x")):
        if old in dem.dims:
            dim_renames[old] = new
    if dim_renames:
        dem = dem.rename(dim_renames)

    # 2. Squeeze singleton band / time dimensions
    #    We want exactly 2-D (y, x).  Drop any leading size-1 dims.
    while dem.ndim > 2:
        for i, (dim, size) in enumerate(zip(dem.dims, dem.shape)):
            if size == 1:
                dem = dem.squeeze(dim, drop=True)
                break
        else:
            # No singleton dim found but still > 2-D: take the first band slice
            dem = dem.isel({dem.dims[0]: 0})

    # 3. Ensure CRS is set
    try:
        if dem.rio.crs is None:
            dem = dem.rio.write_crs("EPSG:4326", inplace=True)
            log.debug("DEM had no CRS metadata; assumed EPSG:4326 (WGS84)")
    except Exception:
        pass

    return dem


def _to_shapely(geometry: Any) -> Any:
    """Coerce *geometry* to a shapely geometry for downstream backends."""
    # Already shapely
    if hasattr(geometry, "geom_type"):
        return geometry
    # GeoDataFrame
    try:
        import geopandas as gpd
        if isinstance(geometry, gpd.GeoDataFrame):
            return geometry.geometry.unary_union
    except Exception:
        pass
    # GeoJSON dict
    if isinstance(geometry, dict):
        from shapely.geometry import shape
        return shape(geometry.get("geometry", geometry))
    # Pass through tuples/lists — backends accept these
    return geometry


def slope_from_dem(dem: "xr.DataArray") -> "xr.DataArray":  # type: ignore[name-defined]
    """Compute slope (degrees) from a DEM using numpy's gradient.

    Used when ``py3dep.get_map("Slope Degrees", …)`` is unavailable (i.e.
    outside CONUS). The DEM is assumed to be reprojected to a metric CRS
    so the gradient pixel-size is in metres; if it's not, the result is
    still in degrees but represents a per-pixel slope rather than a true
    metric slope. Callers that need the metric form should reproject the
    DEM (e.g. to EPSG:5070 for CONUS, UTM elsewhere) before calling this.
    """
    import numpy as np
    import xarray as xr

    # Ensure DEM is 2-D before computing gradient.  rioxarray reproject and
    # some GEE backends preserve a leading band axis (1, H, W); np.gradient
    # with only two spacing arguments only works on 2-D arrays.
    dem = _normalize_dem(dem)  # rename dims, squeeze band axis, set CRS

    arr = dem.values.astype("float64")
    # At this point arr must be exactly 2-D; guard just in case.
    if arr.ndim == 3:
        arr = arr[0]
    elif arr.ndim != 2:
        arr = arr.reshape(arr.shape[-2], arr.shape[-1])

    # Pixel size in the DEM's native CRS (metres for metric projections)
    try:
        dx, dy = (abs(v) for v in dem.rio.resolution())
    except Exception:
        dx = dy = 1.0

    # np.gradient returns (d/dy, d/dx) when axis order is (y, x), which it
    # is for rioxarray-loaded rasters (y increases downward in pixel space,
    # so dy carries the sign; we use abs above for the spacing argument).
    gy, gx = np.gradient(arr, dy, dx)
    slope_rad = np.arctan(np.hypot(gx, gy))
    slope_deg = np.degrees(slope_rad)

    # Reconstruct a 2-D DataArray with the DEM's spatial coords/dims.
    spatial_dims = [d for d in dem.dims if d in ("y", "x")]
    spatial_coords = {k: v for k, v in dem.coords.items() if k in spatial_dims}
    out = xr.DataArray(
        slope_deg,
        coords=spatial_coords,
        dims=spatial_dims if len(spatial_dims) == 2 else dem.dims,
        attrs={**dem.attrs, "units": "degrees", "long_name": "slope"},
    )
    try:
        out.rio.write_crs(dem.rio.crs, inplace=True)
    except Exception:
        pass
    return out
