"""
Soil Data Retrieval for AI-Hydro

Routed through the ``aihydro_data.fetch()`` data door so soil acquisition is
globally robust: CONUS auto-routes to POLARIS (30 m), and basins outside CONUS
fall back to ISRIC SoilGrids (250 m, GEE).

The public return contract is preserved — an :class:`xarray.Dataset` whose
``data_vars`` carry texture fractions named POLARIS-style (``sand_5``,
``silt_5``, ``clay_5``, optionally ``ksat_5``), so the downstream Curve-Number
classifier (:mod:`ai_hydro.analysis.curve_number`) works unchanged regardless
of which backend served the data.

Provenance (which product/source actually served the data, and the detected
region) is attached to ``Dataset.attrs`` under the ``_adata_*`` keys.
"""
from __future__ import annotations

import logging
from typing import Optional, List

log = logging.getLogger(__name__)

try:
    import xarray as xr
    import geopandas as gpd
    _DEPS_AVAILABLE = True
except ImportError:
    _DEPS_AVAILABLE = False


def _to_single_geom(geometry):
    """Coerce a GeoSeries / GeoDataFrame / shapely geom to a single shapely geom."""
    if hasattr(geometry, "geometry") and hasattr(geometry, "iterrows"):
        gser = geometry.geometry
        if geometry.crs is not None and geometry.crs.to_epsg() != 4326:
            gser = geometry.to_crs(epsg=4326).geometry
        return gser.union_all() if hasattr(gser, "union_all") else gser.unary_union
    if hasattr(geometry, "union_all"):
        return geometry.union_all()
    if hasattr(geometry, "unary_union"):
        return geometry.unary_union
    return geometry


def fetch_soil_data_polaris(
    geometry,
    layers: Optional[List[str]] = None,
    product: Optional[str] = None,
) -> "xr.Dataset":
    """
    Fetch soil texture data for a geometry, region-routed with fallback.

    Acquisition flows through :func:`aihydro_data.fetch` (variable ``"soil"``):

    * **CONUS** → POLARIS (30 m probabilistic soil).
    * **Outside CONUS** → ISRIC SoilGrids (250 m global, GEE).

    Despite the historical name, this is no longer POLARIS-only; the name is
    kept for backward compatibility with existing callers.

    Parameters
    ----------
    geometry : gpd.GeoSeries | gpd.GeoDataFrame | shapely geometry
        Geometry to retrieve soil data for (WGS84, EPSG:4326).
    layers : list of str, optional
        POLARIS layer names (e.g. ``['sand_5','silt_5','clay_5','ksat_5']``).
        Applies only to the POLARIS (CONUS) path; ignored for SoilGrids.
    product : str, optional
        Pin a specific product ID (e.g. ``"SOILGRIDS"``, ``"POLARIS"``).
        ``None`` (default) → auto region-routing.

    Returns
    -------
    xr.Dataset
        Dataset with texture fraction variables named POLARIS-style
        (``sand_5``, ``silt_5``, ``clay_5``, optionally ``ksat_5``), in percent.
        ``Dataset.attrs`` carries provenance:
        ``_adata_product``, ``_adata_source``, ``_adata_region``,
        ``_adata_resolution_m``, ``_adata_citation``.

    Notes
    -----
    Top-layer (0–5 cm) properties are used as they most influence surface
    runoff. SoilGrids native units (g/kg) are converted to percent by the
    backend so the hydrologic-group thresholds apply directly.
    """
    if not _DEPS_AVAILABLE:
        raise ImportError("soil data requires: pip install aihydro-tools[data]")

    try:
        import aihydro_data
    except ImportError:
        log.warning("aihydro_data unavailable — using direct POLARIS (CONUS only).")
        return _fetch_polaris_direct(geometry, layers=layers)

    geom = _to_single_geom(geometry)

    # Static product — dates are ignored downstream, but fetch() requires them.
    fetch_kwargs = dict(
        variable="soil",
        geometry=geom,
        start="2020-01-01",
        end="2020-12-31",
        aggregation="raw_raster",
    )
    if product:
        fetch_kwargs.update(mode="manual", product=product)

    log.info(
        "fetch_soil_data_polaris: routing through aihydro_data.fetch(soil, "
        "product=%s)", product or "auto",
    )
    result = aihydro_data.fetch(**fetch_kwargs)

    ds = _adapt_soil_result(result)

    ds.attrs["_adata_product"] = getattr(result, "product", None)
    ds.attrs["_adata_source"] = getattr(result, "source", None)
    ds.attrs["_adata_citation"] = getattr(result, "citation", None)
    try:
        from aihydro_data.routing import detect_region
        ds.attrs["_adata_region"] = detect_region(geom)
    except Exception:
        ds.attrs["_adata_region"] = None
    try:
        first = ds[list(ds.data_vars)[0]]
        ds.attrs["_adata_resolution_m"] = first.attrs.get(
            "resolution_m", ds.attrs.get("resolution_m"),
        )
    except Exception:
        ds.attrs["_adata_resolution_m"] = None
    return ds


def _adapt_soil_result(result) -> "xr.Dataset":
    """Normalise a FetchResult.data into a Dataset with POLARIS-style texture vars."""
    data = getattr(result, "data", result)

    if isinstance(data, dict):
        data = next(iter(data.values()))

    if isinstance(data, xr.Dataset):
        # POLARIS (sand_5/…) or SoilGrids (sand_5/…) — already Dataset form.
        return data

    if isinstance(data, xr.DataArray):
        # A lone DataArray (e.g. single property) — wrap it. Best-effort name.
        name = data.name or "clay_5"
        return data.to_dataset(name=str(name))

    raise TypeError(
        f"Unexpected soil data type from aihydro_data: {type(data)!r} "
        f"(product={getattr(result, 'product', None)!r})."
    )


def _fetch_polaris_direct(geometry, layers: Optional[List[str]] = None) -> "xr.Dataset":
    """Direct POLARIS fetch via pygeohydro — CONUS-only hard fallback."""
    import pygeohydro as gh

    if layers is None:
        layers = ["sand_5", "silt_5", "clay_5", "ksat_5"]
    geom = _to_single_geom(geometry)
    log.info("Retrieving POLARIS soil directly (layers=%s)", layers)
    ds = gh.soil_polaris(layers=layers, geometry=geom, geo_crs=4326)
    ds.attrs["_adata_product"] = "POLARIS"
    ds.attrs["_adata_source"] = "pygeohydro"
    ds.attrs["_adata_region"] = "CONUS"
    return ds
