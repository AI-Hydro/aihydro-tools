"""Planetary Computer STAC DEM fetch for delineation."""

from __future__ import annotations

import hashlib
import logging
import time
import warnings
from pathlib import Path
from typing import Iterable

import numpy as np
import planetary_computer
import rioxarray  # noqa: F401 — activates .rio on xarray
import xarray as xr
from odc.stac import load as stac_load
from pystac_client import Client
from shapely.geometry import Polygon

from ai_hydro.analysis.delineation.utils import meters_to_degrees_bbox

log = logging.getLogger(__name__)

STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
_CACHE_DIR = Path.home() / ".aihydro" / "cache" / "dem"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Collection aliases used by fast tier (merit-hydro STAC coverage is sparse on PC)
COLLECTION_ALIASES = {
    "nasadem": ("nasadem", "elevation"),
    "merit-hydro": ("merit-hydro", "elevtn"),
    "copernicus": ("cop-dem-glo-30", "data"),
}


class StacItemCache:
    def __init__(self) -> None:
        self._cache_ids: set[str] = set()
        self._cache_items: list = []

    def add(self, items: Iterable) -> None:
        for it in items:
            if it.id not in self._cache_ids:
                self._cache_ids.add(it.id)
                self._cache_items.append(it)

    @property
    def items(self) -> list:
        return list(self._cache_items)


def _retry_api_call(func, max_retries: int = 3, delay: int = 2):
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            error_msg = str(e).lower()
            is_retryable = any(
                k in error_msg for k in ("timeout", "time", "connection", "network", "exceeded")
            )
            if attempt < max_retries - 1 and is_retryable:
                wait_time = delay * (2**attempt)
                warnings.warn(f"STAC call failed (attempt {attempt + 1}/{max_retries}): {e}")
                time.sleep(wait_time)
            else:
                raise


def _dem_cache_key(bbox_proj: Polygon, crs_proj: str, resolution_m: float, collection: str) -> str:
    key_str = f"{bbox_proj.wkt}_{crs_proj}_{resolution_m}_{collection}"
    return hashlib.sha256(key_str.encode()).hexdigest()


def _load_cached_dem(key: str) -> xr.DataArray | None:
    cache_file = _CACHE_DIR / f"{key}.tif"
    if not cache_file.exists():
        return None
    try:
        import rioxarray  # noqa: F401

        return xr.open_dataarray(cache_file)
    except Exception:
        return None


def _save_cached_dem(key: str, dem_da: xr.DataArray) -> None:
    cache_file = _CACHE_DIR / f"{key}.tif"
    try:
        dem_da.rio.to_raster(str(cache_file))
    except Exception:
        pass


def fetch_dem_bbox(
    bbox_proj: Polygon,
    crs_proj: str,
    resolution_m: float,
    item_cache: StacItemCache,
    collection: str = "merit-hydro",
    asset_key: str | None = None,
    verbose: bool = False,
) -> xr.DataArray:
    """Fetch DEM for a projected bbox from Planetary Computer."""
    if collection in COLLECTION_ALIASES:
        collection, default_asset = COLLECTION_ALIASES[collection]
        asset_key = asset_key or default_asset
    asset_key = asset_key or "elevtn"

    bbox_wgs84 = meters_to_degrees_bbox(bbox_proj, crs_proj)
    cache_key = _dem_cache_key(bbox_proj, crs_proj, resolution_m, collection)
    cached = _load_cached_dem(cache_key)
    if cached is not None:
        if verbose:
            log.info("Loaded DEM from cache (%sm)", resolution_m)
        return cached

    if verbose:
        log.info("Fetching DEM %s @ %sm", collection, resolution_m)

    catalog = Client.open(STAC_API_URL, modifier=planetary_computer.sign_inplace)

    def _search_items():
        search = catalog.search(collections=[collection], bbox=bbox_wgs84)
        return list(search.item_collection())

    items = _retry_api_call(_search_items)
    if not items:
        raise RuntimeError(f"No {collection} items found for bbox {bbox_wgs84}")

    item_cache.add(items)

    def _load_stac():
        return stac_load(
            item_cache.items,
            bands=[asset_key],
            bbox=bbox_wgs84,
            resolution=resolution_m,
            crs=crs_proj,
            patch_url=planetary_computer.sign,
        )

    dem = _retry_api_call(_load_stac)
    dem_da = dem[asset_key].squeeze().astype(np.float32)
    if dem_da.rio.crs is None:
        dem_da = dem_da.rio.write_crs(crs_proj)

    _save_cached_dem(cache_key, dem_da)
    return dem_da
