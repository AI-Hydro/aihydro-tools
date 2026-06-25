"""
Population exposure helpers for flood inundation (WorldPop / zonal).

Phase 2: optional Google Earth Engine WorldPop sum inside inundated geometry;
offline tests use aligned population rasters.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

WORLDPOP_DATASET = "WorldPop/GP/100m/pop"
WORLDPOP_BAND = "population"
WORLDPOP_LICENSE = (
    "WorldPop (www.worldpop.org) — open access for non-commercial research; "
    "verify redistribution terms for your jurisdiction."
)

__all__ = [
    "WORLDPOP_LICENSE",
    "zonal_population_from_raster",
    "try_worldpop_exposed_count",
    "enrich_exposure_summary",
    "bench_zonal_population",
]


def zonal_population_from_raster(
    inundated_mask: np.ndarray,
    population_raster: np.ndarray,
) -> float:
    """Sum population raster cells where inundated_mask is True."""
    mask = np.asarray(inundated_mask, dtype=bool)
    pop = np.asarray(population_raster, dtype=np.float64)
    if pop.shape != mask.shape:
        raise ValueError(f"Shape mismatch: mask {mask.shape} vs pop {pop.shape}")
    return float(np.nansum(pop[mask]))


def try_worldpop_exposed_count(
    extent_geojson_wgs84: dict[str, Any],
    *,
    year: int = 2020,
) -> dict[str, Any] | None:
    """
    Sum WorldPop within inundated GeoJSON via Google Earth Engine (optional).

    Returns None when EE is unavailable or geometry is empty.
    """
    if not extent_geojson_wgs84.get("features"):
        return None
    try:
        import ee

        try:
            ee.Initialize()
        except Exception:
            pass

        fc = ee.FeatureCollection(extent_geojson_wgs84)
        geom = fc.geometry()
        pop_ic = ee.ImageCollection(WORLDPOP_DATASET).filter(
            ee.Filter.calendarRange(int(year), int(year), "year")
        )
        pop = pop_ic.mosaic().select(WORLDPOP_BAND)
        stats = pop.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom,
            scale=100,
            maxPixels=int(1e10),
        )
        val = stats.get(WORLDPOP_BAND)
        if val is None:
            return None
        count = float(val.getInfo())
        return {
            "population_exposed": round(count, 1),
            "population_method": "worldpop_gee_zonal",
            "population_year": int(year),
            "population_license_note": WORLDPOP_LICENSE,
        }
    except Exception as exc:
        log.info("WorldPop GEE fetch skipped: %s", exc)
        return None


def enrich_exposure_summary(
    exposure: dict[str, Any],
    inundated_mask: np.ndarray,
    *,
    population_raster: np.ndarray | None = None,
    extent_geojson_wgs84: dict[str, Any] | None = None,
    use_worldpop_gee: bool = False,
    worldpop_year: int = 2020,
) -> dict[str, Any]:
    """Augment exposure dict with zonal population when data sources are available."""
    out = dict(exposure)
    data_gaps = list(out.get("data_gaps") or [])

    if population_raster is not None:
        try:
            pop = zonal_population_from_raster(inundated_mask, population_raster)
            out["population_exposed"] = round(pop, 1)
            out["population_method"] = "zonal_sum"
            out["population_license_note"] = WORLDPOP_LICENSE
            data_gaps = [g for g in data_gaps if g != "population"]
        except ValueError:
            data_gaps.append("population_raster_shape_mismatch")

    elif use_worldpop_gee and extent_geojson_wgs84:
        wp = try_worldpop_exposed_count(extent_geojson_wgs84, year=worldpop_year)
        if wp:
            out.update(wp)
            data_gaps = [g for g in data_gaps if g != "population"]

    out["data_gaps"] = data_gaps
    return out


def bench_zonal_population() -> dict[str, Any]:
    """HRB: zonal sum on synthetic 2×2 grid."""
    mask = np.array([[1, 1], [0, 0]], dtype=bool)
    pop = np.array([[10.0, 20.0], [5.0, 100.0]], dtype=float)
    total = zonal_population_from_raster(mask, pop)
    return {"population_exposed": total, "population_method": "zonal_sum"}
