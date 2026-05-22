"""
Lazy MERIT-Hydro / MERIT-Basins data layout under ~/.aihydro/merit/.

Vector rivers + level-2 index enable outlet snapping; full raster sets enable
upstream-delineator (accurate tier) when installed locally.
"""

from __future__ import annotations

import logging
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import yaml
from shapely.geometry import Point

log = logging.getLogger(__name__)

_MANIFEST_PATH = Path(__file__).parent / "merit_manifest.yaml"


@dataclass
class BasinEnsureStatus:
    pfaf_code: str
    level2_ready: bool
    rivers_ready: bool
    catchments_ready: bool
    flowdir_ready: bool
    message: str = ""
    downloaded: list[str] | None = None


class MeritDataManager:
    """Resolve Pfafstetter basin and local MERIT file paths."""

    def __init__(self, root: Path | str | None = None) -> None:
        if root is not None:
            self.root = Path(root)
        else:
            env = os.environ.get("AIHYDRO_MERIT_DIR") or os.environ.get("MERIT_DATA_DIR")
            self.root = Path(env) if env else Path.home() / ".aihydro" / "merit"
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest = self._load_manifest()
        self._base_url = os.environ.get("AIHYDRO_MERIT_BASE_URL", "").rstrip("/")

    def _load_manifest(self) -> dict[str, Any]:
        if _MANIFEST_PATH.exists():
            with open(_MANIFEST_PATH) as f:
                return yaml.safe_load(f) or {}
        return {}

    def level2_shapefile_path(self) -> Path:
        rel = self._manifest.get("level2", {}).get("relative_dir", "shp/basins_level2")
        return self.root / rel / "merit_hydro_vect_level2.shp"

    def river_shapefile_path(self, pfaf_code: str) -> Path:
        pfaf = pfaf_code.zfill(2)
        tmpl = (
            self._manifest.get("basin_template", {})
            .get("rivers", {})
            .get("filename", "riv_pfaf_{pfaf}_MERIT_Hydro_v07_Basins_v01.shp")
        )
        rel = self._manifest.get("basin_template", {}).get("rivers", {}).get(
            "relative_dir", "shp/merit_rivers"
        )
        return self.root / rel / tmpl.format(pfaf=pfaf)

    def catchment_shapefile_path(self, pfaf_code: str) -> Path:
        pfaf = pfaf_code.zfill(2)
        tmpl = (
            self._manifest.get("basin_template", {})
            .get("catchments", {})
            .get("filename", "cat_pfaf_{pfaf}_MERIT_Hydro_v07_Basins_v01.shp")
        )
        rel = self._manifest.get("basin_template", {}).get("catchments", {}).get(
            "relative_dir", "shp/merit_catchments"
        )
        return self.root / rel / tmpl.format(pfaf=pfaf)

    def _glob_flowdir(self, pfaf_code: str) -> Path | None:
        pfaf = pfaf_code.zfill(2)
        rel = self._manifest.get("basin_template", {}).get("flowdir", {}).get(
            "relative_dir", "raster/flowdir_basins"
        )
        folder = self.root / rel
        if not folder.exists():
            return None
        matches = list(folder.glob(f"*pfaf_{pfaf}*.tif"))
        return matches[0] if matches else None

    def resolve_pfaf_code(self, lat: float, lon: float) -> str:
        shp = self.level2_shapefile_path()
        if not shp.exists():
            raise FileNotFoundError(
                f"MERIT level-2 index missing at {shp}. "
                "Run merit_ensure_basin or install MERIT vectors — see local-docs/watershed-delineation.md"
            )
        gdf = gpd.read_file(shp)
        pt = Point(lon, lat)
        hit = gdf[gdf.geometry.contains(pt)]
        if hit.empty:
            raise ValueError(f"({lat}, {lon}) is outside MERIT level-2 basins.")
        row = hit.iloc[0]
        code = str(
            row.get("pfaf_code")
            or row.get("PFAF_ID")
            or row.get("BASIN")
            or row.get("basin")
            or ""
        )
        if not code:
            raise ValueError(f"Could not read Pfaf/BASIN code from level-2 index at ({lat}, {lon}).")
        return str(int(code)).zfill(2)

    def _try_download(self, url: str, dest: Path) -> bool:
        if not url:
            return False
        try:
            import urllib.request

            dest.parent.mkdir(parents=True, exist_ok=True)
            log.info("Downloading %s", url)
            urllib.request.urlretrieve(url, dest)  # noqa: S310
            if dest.suffix == ".zip" and dest.exists():
                with zipfile.ZipFile(dest, "r") as zf:
                    zf.extractall(dest.parent)
            return True
        except Exception as e:
            log.warning("Download failed %s: %s", url, e)
            return False

    def ensure_basin(
        self, lat: float, lon: float, *, download: bool = True
    ) -> BasinEnsureStatus:
        """Ensure MERIT assets for the basin containing (lat, lon)."""
        downloaded: list[str] = []
        level2_ready = self.level2_shapefile_path().exists()

        if not level2_ready and download:
            url = self._manifest.get("level2", {}).get("download_url")
            if self._base_url:
                url = f"{self._base_url}/level2/merit_hydro_vect_level2.zip"
            if url:
                zip_path = self.root / "downloads" / "merit_hydro_vect_level2.zip"
                if self._try_download(url, zip_path):
                    downloaded.append("level2")
                    level2_ready = self.level2_shapefile_path().exists()
            if not level2_ready:
                try:
                    from ai_hydro.data.merit_download import ensure_level2_index

                    if ensure_level2_index(self.level2_shapefile_path().parent):
                        downloaded.append("level2")
                        level2_ready = True
                except Exception as e:
                    log.warning("Level-2 install via delineator sample failed: %s", e)

        pfaf = self.resolve_pfaf_code(lat, lon) if level2_ready else "00"
        rivers_ready = self.river_shapefile_path(pfaf).exists()
        catchments_ready = self.catchment_shapefile_path(pfaf).exists()
        flowdir_ready = self._glob_flowdir(pfaf) is not None

        if download and not rivers_ready:
            url = (
                self._manifest.get("basin_template", {})
                .get("rivers", {})
                .get("download_url")
            )
            if self._base_url:
                url = f"{self._base_url}/rivers/riv_pfaf_{pfaf}.zip"
            if url:
                zip_path = self.root / "downloads" / f"riv_pfaf_{pfaf}.zip"
                if self._try_download(url, zip_path):
                    downloaded.append(f"rivers_{pfaf}")
                    rivers_ready = self.river_shapefile_path(pfaf).exists()
            if not rivers_ready:
                try:
                    from ai_hydro.data.merit_download import download_river_shapefile

                    rivers_dir = self.river_shapefile_path(pfaf).parent
                    if download_river_shapefile(pfaf, rivers_dir):
                        downloaded.append(f"rivers_{pfaf}")
                        rivers_ready = self.river_shapefile_path(pfaf).exists()
                except Exception as e:
                    log.warning("MERIT river download (Google Drive) failed for pfaf %s: %s", pfaf, e)

        msg_parts = [f"Pfafstetter basin {pfaf}"]
        if not level2_ready:
            msg_parts.append("level-2 index missing")
        if not rivers_ready:
            msg_parts.append(
                "river vectors missing — click Install again or run: "
                "python scripts/install_merit_minimal.py --lat <lat> --lon <lon>"
            )

        return BasinEnsureStatus(
            pfaf_code=pfaf,
            level2_ready=level2_ready,
            rivers_ready=rivers_ready,
            catchments_ready=catchments_ready,
            flowdir_ready=flowdir_ready,
            message="; ".join(msg_parts),
            downloaded=downloaded or None,
        )

    def configure_delineator_env(self, pfaf_code: str) -> dict[str, str]:
        """Return env var dict for upstream-delineator (paths must exist)."""
        pfaf = pfaf_code.zfill(2)
        catch = self.catchment_shapefile_path(pfaf)
        river = self.river_shapefile_path(pfaf)
        flowdir = self._glob_flowdir(pfaf)
        accum_dir = self.root / "raster" / "accum_basins"
        accum_matches = list(accum_dir.glob(f"*pfaf_{pfaf}*.tif")) if accum_dir.exists() else []
        megabasins = self.level2_shapefile_path()

        env = {
            "CATCHMENT_PATH": str(catch) if catch.exists() else "",
            "RIVER_PATH": str(river) if river.exists() else "",
            "FLOW_DIR_PATH": str(flowdir) if flowdir else "",
            "ACCUM_PATH": str(accum_matches[0]) if accum_matches else "",
            "MEGABASINS_PATH": str(megabasins) if megabasins.exists() else "",
        }
        return env

    def delineator_ready(self, pfaf_code: str) -> bool:
        env = self.configure_delineator_env(pfaf_code)
        required = ("CATCHMENT_PATH", "RIVER_PATH", "FLOW_DIR_PATH")
        return all(env.get(k) for k in required)
