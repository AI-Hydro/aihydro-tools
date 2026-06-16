"""
Land Cover Data Retrieval for AI-Hydro

Routed through the ``aihydro_data.fetch()`` data door so land-cover acquisition
is globally robust: CONUS auto-routes to NLCD, and basins outside CONUS fall
back to ESA WorldCover / Dynamic World (GEE) or the auth-free ESA WorldCover
via Planetary Computer STAC.

The public return contract is preserved — an :class:`xarray.Dataset` with a
``cover_{year}`` variable holding **NLCD-style class codes**, so the downstream
Curve-Number machinery (:mod:`ai_hydro.analysis.curve_number`) works unchanged
regardless of which backend served the data.  Non-NLCD products (ESA WorldCover,
Dynamic World) are remapped onto the nearest NLCD class so a single CN lookup
table covers every region.

Provenance (which product/source actually served the data, and the detected
region) is attached to ``Dataset.attrs`` under the ``_adata_*`` keys.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

try:
    import numpy as np
    import xarray as xr
    import geopandas as gpd
    _DEPS_AVAILABLE = True
except ImportError:
    _DEPS_AVAILABLE = False


# ---------------------------------------------------------------------------
# ESA WorldCover / Dynamic World → NLCD class remap
# ---------------------------------------------------------------------------
# ESA WorldCover v200 codes (10..100) and Dynamic World label codes (0..8) are
# remapped onto NLCD codes so the existing NRCS CN lookup table in
# curve_number.py (keyed on NLCD classes) applies everywhere.  The mapping is
# hydrologically conservative — each global class lands on the NLCD class with
# the closest runoff behaviour.

# ESA WorldCover (units: class code) → NLCD
_ESA_WORLDCOVER_TO_NLCD: dict[int, int] = {
    10:  42,   # Tree cover            → Evergreen Forest
    20:  52,   # Shrubland            → Shrub/Scrub
    30:  71,   # Grassland            → Grassland/Herbaceous
    40:  82,   # Cropland             → Cultivated Crops
    50:  24,   # Built-up             → Developed, High Intensity
    60:  31,   # Bare / sparse veg.   → Barren Land
    70:  12,   # Snow and ice         → Perennial Ice/Snow
    80:  11,   # Permanent water      → Open Water
    90:  95,   # Herbaceous wetland   → Emergent Herbaceous Wetlands
    95:  90,   # Mangroves            → Woody Wetlands
    100: 52,   # Moss and lichen      → Shrub/Scrub
}

# Google Dynamic World label band (0..8) → NLCD
_DYNAMIC_WORLD_TO_NLCD: dict[int, int] = {
    0: 11,   # water                → Open Water
    1: 42,   # trees                → Evergreen Forest
    2: 71,   # grass                → Grassland/Herbaceous
    3: 95,   # flooded_vegetation   → Emergent Herbaceous Wetlands
    4: 82,   # crops                → Cultivated Crops
    5: 52,   # shrub_and_scrub      → Shrub/Scrub
    6: 24,   # built                → Developed, High Intensity
    7: 31,   # bare                 → Barren Land
    8: 12,   # snow_and_ice         → Perennial Ice/Snow
}


def _remap_codes(arr: "np.ndarray", table: dict[int, int]) -> "np.ndarray":
    """Vectorised class-code remap; codes absent from the table become NaN."""
    out = np.full(arr.shape, np.nan, dtype=np.float32)
    finite = np.isfinite(arr)
    rounded = np.where(finite, np.round(arr), -1).astype(np.int64)
    for src, dst in table.items():
        out[rounded == src] = float(dst)
    return out


def _remap_table_for_product(product: str) -> Optional[dict[int, int]]:
    """Return the code-remap table for a non-NLCD land-cover product, or None."""
    p = (product or "").upper()
    if p.startswith("NLCD"):
        return None  # already NLCD codes
    if p.startswith("ESA_WORLDCOVER") or "WORLDCOVER" in p:
        return _ESA_WORLDCOVER_TO_NLCD
    if p.startswith("DYNAMIC_WORLD") or "DYNAMICWORLD" in p:
        return _DYNAMIC_WORLD_TO_NLCD
    # Unknown product — assume ESA-style codes (the common global fallback).
    return _ESA_WORLDCOVER_TO_NLCD


def _to_single_geom(geometry):
    """Coerce a GeoSeries / GeoDataFrame / shapely geom to a single shapely geom."""
    # GeoDataFrame / GeoSeries → unary union of all rows (handles multi-row)
    if hasattr(geometry, "geometry") and hasattr(geometry, "iterrows"):
        gser = geometry.geometry
        if geometry.crs is not None and geometry.crs.to_epsg() != 4326:
            gser = geometry.to_crs(epsg=4326).geometry
        return gser.union_all() if hasattr(gser, "union_all") else gser.unary_union
    if hasattr(geometry, "union_all"):       # GeoSeries
        return geometry.union_all()
    if hasattr(geometry, "unary_union"):      # older GeoSeries
        return geometry.unary_union
    return geometry  # already shapely / geojson-coercible


def fetch_lulc_data(
    geometry,
    resolution: int = 30,
    year: int = 2019,
    product: Optional[str] = None,
) -> "xr.Dataset":
    """
    Fetch Land Use/Land Cover data for a geometry, region-routed with fallback.

    Acquisition flows through :func:`aihydro_data.fetch` (variable
    ``"landcover"``):

    * **CONUS / N. America** → NLCD (30 m).
    * **Outside CONUS** → ESA WorldCover or Dynamic World (10 m, GEE), with the
      auth-free ESA WorldCover via Planetary Computer STAC as the final
      fallback.

    Parameters
    ----------
    geometry : gpd.GeoSeries | gpd.GeoDataFrame | shapely geometry
        Geometry to retrieve land cover for (WGS84, EPSG:4326).
    resolution : int, optional
        Target resolution in metres (default 30; NLCD native).  Served
        resolution may differ for global products — the actual value is
        reported in ``Dataset.attrs['_adata_resolution_m']``.
    year : int, optional
        Year of land-cover data (default 2019).  For NLCD the nearest available
        epoch is used; for ESA WorldCover (2020–2021) and Dynamic World the
        product's available period applies.
    product : str, optional
        Pin a specific product ID (e.g. ``"ESA_WORLDCOVER_STAC"``, ``"NLCD"``).
        ``None`` (default) → auto region-routing.  The region's policy fallback
        chain still applies unless the served product itself fails.

    Returns
    -------
    xr.Dataset
        Dataset with a ``cover_{year}`` variable holding **NLCD-style class
        codes** (global products are remapped onto the nearest NLCD class).
        ``Dataset.attrs`` carries provenance:
        ``_adata_product``, ``_adata_source``, ``_adata_region``,
        ``_adata_resolution_m``, ``_adata_citation``.

    Notes
    -----
    NLCD classes: 11 Open Water · 12 Ice/Snow · 21-24 Developed · 31 Barren ·
    41-43 Forest · 52 Shrub · 71 Grassland · 81-82 Agriculture ·
    90/95 Wetlands.
    """
    if not _DEPS_AVAILABLE:
        raise ImportError("land cover data requires: pip install aihydro-tools[data]")

    try:
        import aihydro_data
    except ImportError:
        # Hard fallback: direct pygeohydro (CONUS only, no global fallback).
        log.warning("aihydro_data unavailable — using direct NLCD (CONUS only).")
        return _fetch_nlcd_direct(geometry, resolution=resolution, year=year)

    geom = _to_single_geom(geometry)
    start = f"{year}-01-01"
    end = f"{year}-12-31"

    fetch_kwargs = dict(
        variable="landcover",
        geometry=geom,
        start=start,
        end=end,
        aggregation="raw_raster",
    )
    if product:
        fetch_kwargs.update(mode="manual", product=product)

    log.info(
        "fetch_lulc_data: routing through aihydro_data.fetch(landcover, "
        "year=%s, product=%s)", year, product or "auto",
    )
    result = aihydro_data.fetch(**fetch_kwargs)

    ds = _adapt_landcover_result(result, year=year)

    # Attach provenance so the CN tool can surface which product served it.
    ds.attrs["_adata_product"] = getattr(result, "product", None)
    ds.attrs["_adata_source"] = getattr(result, "source", None)
    ds.attrs["_adata_citation"] = getattr(result, "citation", None)
    try:
        from aihydro_data.routing import detect_region
        ds.attrs["_adata_region"] = detect_region(geom)
    except Exception:
        ds.attrs["_adata_region"] = None
    cover = ds[f"cover_{year}"]
    ds.attrs["_adata_resolution_m"] = cover.attrs.get("resolution_m", resolution)
    return ds


def _adapt_landcover_result(result, year: int) -> "xr.Dataset":
    """Normalise a FetchResult.data into a Dataset with NLCD-coded ``cover_{year}``."""
    data = getattr(result, "data", result)
    product = getattr(result, "product", "") or ""

    # pygeohydro nlcd_bygeom on a GeoDataFrame may yield {index: Dataset}
    if isinstance(data, dict):
        data = next(iter(data.values()))

    cover_var = f"cover_{year}"

    # ── NLCD path: already a Dataset with NLCD codes ───────────────────────
    if isinstance(data, xr.Dataset):
        # Find the cover variable (cover_{year} or first data var).
        if cover_var in data.data_vars:
            return data
        # NLCD backend may key by the actual epoch year, not the requested one.
        cover_vars = [v for v in data.data_vars if str(v).startswith("cover")]
        if cover_vars:
            return data.rename({cover_vars[0]: cover_var})
        first = list(data.data_vars)[0]
        return data.rename({first: cover_var})

    # ── Global path: single-band DataArray of native class codes ───────────
    if isinstance(data, xr.DataArray):
        da = data.squeeze(drop=True)
        table = _remap_table_for_product(product)
        if table is not None:
            remapped = _remap_codes(da.values, table)
            da = xr.DataArray(
                remapped, coords=da.coords, dims=da.dims, attrs=dict(da.attrs),
            )
        ds = da.to_dataset(name=cover_var)
        # Preserve CRS for downstream rio operations.
        try:
            if da.rio.crs is not None:
                ds = ds.rio.write_crs(da.rio.crs)
        except Exception:
            pass
        return ds

    raise TypeError(
        f"Unexpected land-cover data type from aihydro_data: {type(data)!r} "
        f"(product={product!r})."
    )


def _fetch_nlcd_direct(geometry, resolution: int = 30, year: int = 2019) -> "xr.Dataset":
    """Direct NLCD fetch via pygeohydro — CONUS-only hard fallback."""
    import pygeohydro as gh

    log.info("Retrieving NLCD land cover directly (year=%s, res=%sm)", year, resolution)
    # Ensure a GeoDataFrame for nlcd_bygeom.
    if not (hasattr(geometry, "geometry") and hasattr(geometry, "iterrows")):
        geom = _to_single_geom(geometry)
        geometry = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
    nlcd_dict = gh.nlcd_bygeom(
        geometry, resolution=resolution, years={"cover": [year]},
        region="L48", crs=4326,
    )
    ds = next(iter(nlcd_dict.values()))
    ds.attrs["_adata_product"] = "NLCD"
    ds.attrs["_adata_source"] = "pygeohydro"
    ds.attrs["_adata_region"] = "CONUS"
    ds.attrs["_adata_resolution_m"] = resolution
    return ds
