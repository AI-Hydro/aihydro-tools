"""
Adapter for Upstream-Tech/delineator (MERIT-Basins accurate tier).

Optional dependency: install with pip install 'aihydro-tools[delineation]'.
"""

from __future__ import annotations

import csv
import logging
import os
import tempfile
from pathlib import Path
from typing import NamedTuple

import geopandas as gpd

from ai_hydro.analysis.delineation.types import area_km2
from ai_hydro.data.merit_manager import MeritDataManager

log = logging.getLogger(__name__)


class MeritBasinsResult(NamedTuple):
    gdf: gpd.GeoDataFrame
    area_km2: float
    output_dir: Path
    pfaf_code: str


def _upstream_delineator_available() -> bool:
    try:
        import upstream_delineator  # noqa: F401

        return True
    except ImportError:
        return False


def delineate_merit_basins(
    lat: float,
    lon: float,
    *,
    outlet_id: str = "outlet",
    verbose: bool = False,
    manager: MeritDataManager | None = None,
) -> MeritBasinsResult:
    """
    Delineate using upstream-delineator for a single outlet.

    Requires local MERIT-Basins vectors + flowdir rasters for the Pfaf basin.
    """
    if not _upstream_delineator_available():
        raise ImportError(
            "upstream-delineator is not installed. "
            "Install with: pip install 'aihydro-tools[delineation]'"
        )

    mgr = manager or MeritDataManager()
    status = mgr.ensure_basin(lat, lon, download=False)
    pfaf = status.pfaf_code
    if not mgr.delineator_ready(pfaf):
        raise FileNotFoundError(
            f"MERIT-Basins data incomplete for basin {pfaf}. "
            f"{status.message} "
            "Install catchments, rivers, and flowdir rasters — see merit_ensure_basin."
        )

    env = mgr.configure_delineator_env(pfaf)
    old_env = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v:
                os.environ[k] = v

        from upstream_delineator.delineator_utils.delineate import delineate

        with tempfile.TemporaryDirectory(prefix="aihydro_delineator_") as tmp:
            csv_path = Path(tmp) / "outlets.csv"
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=["id", "lat", "lng", "is_outlet", "name"]
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "id": outlet_id,
                        "lat": lat,
                        "lng": lon,
                        "is_outlet": "true",
                        "name": outlet_id,
                    }
                )
            prefix = str(Path(tmp) / outlet_id)
            config = {"VERBOSE": verbose, "NO_VERBOSE": not verbose, "PLOTS": False}
            delineate(str(csv_path), prefix, config)

            # Prefer geopackage / geojson outputs
            candidates = list(Path(tmp).glob(f"{outlet_id}_subbasins.*"))
            if not candidates:
                candidates = list(Path(tmp).glob(f"*{outlet_id}*subbasin*.*"))
            if not candidates:
                raise RuntimeError(
                    "upstream-delineator finished but no subbasins output found."
                )
            gdf = gpd.read_file(candidates[0])
            if gdf.crs is None:
                gdf = gdf.set_crs("EPSG:4326")
            gdf = gdf.to_crs(4326)
            dissolved = gdf.dissolve().reset_index(drop=True)
            dissolved = dissolved[~dissolved.geometry.is_empty]
            if dissolved.empty:
                raise RuntimeError("Delineator returned empty watershed geometry.")
            a = area_km2(dissolved)
            out_copy = dissolved.copy()
            return MeritBasinsResult(
                gdf=out_copy,
                area_km2=a,
                output_dir=Path(tmp),
                pfaf_code=pfaf,
            )
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
