"""
Lazy MERIT-Hydro / MERIT-Basins data layout under ~/.aihydro/merit/.

Vector rivers + level-2 index enable outlet snapping; full raster sets enable
upstream-delineator (accurate tier) when installed locally.
"""

from __future__ import annotations

import logging
import os
import hashlib
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import yaml
from shapely.geometry import Point

log = logging.getLogger(__name__)

_MANIFEST_PATH = Path(__file__).parent / "merit_manifest.yaml"

# ---------------------------------------------------------------------------
# Auto-staging policy (Design B)
# ---------------------------------------------------------------------------
# Read once from ~/.aihydro/config.json.  Keys:
#   merit_auto_stage: "tiered" | "always" | "never"   (default "tiered")
#   merit_auto_stage_max_gb: float                     (default 2.0)
# ---------------------------------------------------------------------------

def _merit_auto_stage_policy() -> tuple[str, float]:
    """Return (policy, max_gb) from ~/.aihydro/config.json."""
    import json as _json
    cfg_path = Path.home() / ".aihydro" / "config.json"
    try:
        if cfg_path.exists():
            cfg = _json.loads(cfg_path.read_text())
            policy = str(cfg.get("merit_auto_stage", "tiered"))
            max_gb = float(cfg.get("merit_auto_stage_max_gb", 2.0))
            return policy, max_gb
    except Exception:
        pass
    return "tiered", 2.0


def _estimate_flowdir_size_bytes(url: str) -> int | None:
    """HTTP HEAD request to get content-length without downloading."""
    import urllib.request
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=8) as r:
            cl = r.headers.get("Content-Length")
            return int(cl) if cl else None
    except Exception:
        return None


@dataclass
class BasinEnsureStatus:
    pfaf_code: str
    level2_ready: bool
    rivers_ready: bool
    catchments_ready: bool
    flowdir_ready: bool
    message: str = ""
    downloaded: list[str] | None = None


@dataclass
class RoutingRegionStatus:
    pfaf_region: str
    flowdir_ready: bool
    flowdir_path: str | None
    accum_ready: bool
    accum_path: str | None
    local_upstream_area_ready: bool
    local_upstream_area_path: str | None
    acquisition_required: bool
    acquisition_policy: str
    required_assets: tuple[str, ...]
    estimated_download_size_bytes: int | None
    metadata_path: str | None
    message: str
    downloaded: list[str] | None = None


@dataclass
class BasinsRegionStatus:
    pfaf_region: str
    catchments_ready: bool
    catchments_path: str | None
    rivers_ready: bool
    rivers_path: str | None
    acquisition_required: bool
    acquisition_policy: str
    estimated_download_size_bytes: int | None
    metadata_path: str | None
    source: str | None
    license: str
    citation: str
    message: str
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
        matches = list(folder.glob(f"*pfaf_{pfaf}*.tif")) or list(folder.glob(f"flowdir{pfaf}.tif"))
        return matches[0] if matches else None

    def _glob_accum(self, pfaf_code: str) -> Path | None:
        pfaf = pfaf_code.zfill(2)
        rel = self._manifest.get("basin_template", {}).get("accumulation", {}).get(
            "relative_dir", "raster/accum_basins"
        )
        folder = self.root / rel
        if not folder.exists():
            return None
        matches = list(folder.glob(f"*pfaf_{pfaf}*.tif")) or list(folder.glob(f"accum{pfaf}.tif"))
        return matches[0] if matches else None

    def _local_upstream_area_path(self, pfaf_code: str) -> Path:
        pfaf = pfaf_code.zfill(2)
        return self.root / "raster" / "upstream_area_local" / f"upa_local_{pfaf}.tif"

    def _flowdir_metadata_path(self, pfaf_code: str) -> Path:
        pfaf = pfaf_code.zfill(2)
        return self.root / "metadata" / f"flowdir_{pfaf}.json"

    def _basins_metadata_path(self, pfaf_code: str) -> Path:
        pfaf = pfaf_code.zfill(2)
        return self.root / "metadata" / f"basins_{pfaf}.json"

    def _merit_license(self) -> str:
        return "CC-BY-NC 4.0 or ODbL 1.0; derived-data obligations may apply."

    def _merit_basins_citation(self) -> str:
        return (
            "Yamazaki et al. 2019, MERIT Hydro; MERIT-Basins vector hydrography, "
            "https://www.reachhydro.org/home/params/merit-basins"
        )

    def resolve_pfaf_region(self, lat: float, lon: float) -> str:
        """Resolve the MERIT Pfafstetter level-2 region for a point."""
        return self.resolve_pfaf_code(lat, lon)

    def flowdir_path(self, pfaf_code: str) -> Path | None:
        """Return the cached regional MERIT flow-direction raster path, if present."""
        return self._glob_flowdir(pfaf_code)

    def routing_region_cache(self, pfaf_region: str) -> RoutingRegionStatus:
        """Report flowdir-first regional routing cache readiness."""
        pfaf = pfaf_region.zfill(2)
        flowdir = self._glob_flowdir(pfaf)
        accum = self._glob_accum(pfaf)
        local_upa = self._local_upstream_area_path(pfaf)
        meta = self._flowdir_metadata_path(pfaf)
        return RoutingRegionStatus(
            pfaf_region=pfaf,
            flowdir_ready=flowdir is not None,
            flowdir_path=str(flowdir) if flowdir else None,
            accum_ready=accum is not None,
            accum_path=str(accum) if accum else None,
            local_upstream_area_ready=local_upa.exists(),
            local_upstream_area_path=str(local_upa) if local_upa.exists() else None,
            acquisition_required=flowdir is None,
            acquisition_policy="check_only",
            required_assets=("flowdir",),
            estimated_download_size_bytes=flowdir.stat().st_size if flowdir and flowdir.exists() else None,
            metadata_path=str(meta) if meta.exists() else None,
            message=(
                f"Regional MERIT flowdir cached for Pfaf {pfaf}."
                if flowdir
                else f"Regional MERIT flowdir missing for Pfaf {pfaf}; staging required."
            ),
        )

    def basins_region_cache(self, pfaf_region: str) -> BasinsRegionStatus:
        """Report regional MERIT-Basins vector/topology cache readiness."""
        pfaf = pfaf_region.zfill(2)
        catchments = self.catchment_shapefile_path(pfaf)
        rivers = self.river_shapefile_path(pfaf)
        meta = self._basins_metadata_path(pfaf)
        catch_ready = catchments.exists()
        rivers_ready = rivers.exists()
        source = self._base_url or "local_cache_or_manifest"
        return BasinsRegionStatus(
            pfaf_region=pfaf,
            catchments_ready=catch_ready,
            catchments_path=str(catchments) if catch_ready else None,
            rivers_ready=rivers_ready,
            rivers_path=str(rivers) if rivers_ready else None,
            acquisition_required=not (catch_ready and rivers_ready),
            acquisition_policy="check_only",
            estimated_download_size_bytes=None,
            metadata_path=str(meta) if meta.exists() else None,
            source=source,
            license=self._merit_license(),
            citation=self._merit_basins_citation(),
            message=(
                f"MERIT-Basins catchments/rivers cached for Pfaf {pfaf}."
                if catch_ready and rivers_ready
                else f"MERIT-Basins vectors missing for Pfaf {pfaf}; staging required."
            ),
        )

    def ensure_basins_region(
        self,
        pfaf_region: str,
        *,
        acquisition_policy: str = "check_only",
    ) -> BasinsRegionStatus:
        """Ensure/report regional MERIT-Basins vector assets for hybrid routing."""
        pfaf = pfaf_region.zfill(2)
        status = self.basins_region_cache(pfaf)
        downloaded: list[str] = []
        fallback_downloaded = False
        if acquisition_policy in ("download", "download_if_missing") and status.acquisition_required:
            # Reuse the existing lazy vector installer. It resolves by point, so this
            # path remains check/report oriented unless a lab mirror is configured.
            for asset, tmpl_key, dest_path in (
                ("catchments", "catchments", self.catchment_shapefile_path(pfaf)),
                ("rivers", "rivers", self.river_shapefile_path(pfaf)),
            ):
                if dest_path.exists():
                    continue
                tmpl = (
                    self._manifest.get("basin_template", {})
                    .get(tmpl_key, {})
                    .get("download_url")
                )
                url = tmpl.format(pfaf=pfaf) if tmpl else None
                if self._base_url:
                    url = f"{self._base_url}/{asset}/{dest_path.stem}.zip"
                if url:
                    zip_path = self.root / "downloads" / f"{asset}_{pfaf}.zip"
                    if self._try_download(url, zip_path):
                        downloaded.append(f"{asset}_{pfaf}")
            status = self.basins_region_cache(pfaf)
            if status.catchments_ready is False:
                try:
                    from ai_hydro.data.merit_download import download_catchment_shapefile

                    catchments_dir = self.catchment_shapefile_path(pfaf).parent
                    if download_catchment_shapefile(pfaf, catchments_dir):
                        downloaded.append(f"catchments_{pfaf}")
                        fallback_downloaded = True
                except Exception as e:
                    log.warning("MERIT catchment download (Google Drive) failed for pfaf %s: %s", pfaf, e)
            if status.rivers_ready is False:
                try:
                    from ai_hydro.data.merit_download import download_river_shapefile

                    rivers_dir = self.river_shapefile_path(pfaf).parent
                    if download_river_shapefile(pfaf, rivers_dir):
                        downloaded.append(f"rivers_{pfaf}")
                        fallback_downloaded = True
                except Exception as e:
                    log.warning("MERIT river download (Google Drive) failed for pfaf %s: %s", pfaf, e)
            status = self.basins_region_cache(pfaf)
            if not status.acquisition_required:
                source = "google_drive_public_merit_basins" if fallback_downloaded else self._base_url or "manifest_urls"
                self._write_basins_metadata(pfaf, source=source)
                status = self.basins_region_cache(pfaf)
        elif acquisition_policy in ("download", "download_if_missing") and not status.acquisition_required:
            if status.metadata_path is None:
                self._write_basins_metadata(pfaf, source="existing_local_cache")
                status = self.basins_region_cache(pfaf)

        status.acquisition_policy = acquisition_policy
        status.downloaded = downloaded or None
        if status.acquisition_required and acquisition_policy == "check_only":
            status.message = (
                f"MERIT-Basins vectors missing for Pfaf {pfaf}; call "
                "merit_ensure_basins_region(..., acquisition_policy='download') "
                "after reviewing the regional vector assets."
            )
        return status

    def _write_basins_metadata(self, pfaf: str, *, source: str) -> None:
        meta = self._basins_metadata_path(pfaf)
        meta.parent.mkdir(parents=True, exist_ok=True)
        assets: dict[str, dict[str, Any]] = {}
        for name, path in (
            ("catchments", self.catchment_shapefile_path(pfaf)),
            ("rivers", self.river_shapefile_path(pfaf)),
        ):
            if not path.exists():
                continue
            digest = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
            assets[name] = {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "checksum_sha256": digest.hexdigest(),
            }
        payload = {
            "pfaf_region": pfaf,
            "asset": "merit_basins_vectors",
            "assets": assets,
            "source": source,
            "dataset_version": "MERIT_Hydro_v07_Basins_v01",
            "acquisition_date": datetime.now(timezone.utc).isoformat(),
            "license": self._merit_license(),
            "citation": self._merit_basins_citation(),
        }
        meta.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    def _write_flowdir_metadata(self, pfaf: str, flowdir: Path, *, source: str) -> None:
        meta = self._flowdir_metadata_path(pfaf)
        meta.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with flowdir.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        payload = {
            "pfaf_region": pfaf,
            "asset": "flowdir",
            "path": str(flowdir),
            "source": source,
            "checksum_sha256": digest.hexdigest(),
            "size_bytes": flowdir.stat().st_size,
            "acquisition_date": datetime.now(timezone.utc).isoformat(),
            "license": "CC-BY-NC 4.0 or ODbL 1.0; derived-data obligations may apply.",
            "citation": "Yamazaki et al. 2019, MERIT Hydro, https://doi.org/10.1029/2019WR024873",
        }
        meta.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

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

    def ensure_routing_region(
        self,
        lat: float | None = None,
        lon: float | None = None,
        *,
        pfaf_region: str | None = None,
        required_assets: tuple[str, ...] = ("flowdir",),
        acquisition_policy: str = "check_only",
    ) -> RoutingRegionStatus:
        """
        Ensure/report flowdir-first regional MERIT routing assets.

        ``check_only`` never downloads. ``download`` attempts only configured
        manifest/base-url flowdir URLs and still never requires accumulation.
        """
        if pfaf_region is None:
            if lat is None or lon is None:
                raise ValueError("lat/lon or pfaf_region is required.")
            pfaf_region = self.resolve_pfaf_region(lat, lon)
        pfaf = pfaf_region.zfill(2)
        required = tuple(required_assets or ("flowdir",))
        if any(asset != "flowdir" for asset in required):
            raise ValueError("Default regional routing currently supports required_assets=('flowdir',) only.")

        status = self.routing_region_cache(pfaf)
        downloaded: list[str] = []
        if (
            acquisition_policy in ("download", "download_if_missing")
            and not status.flowdir_ready
        ):
            tmpl = (
                self._manifest.get("basin_template", {})
                .get("flowdir", {})
                .get("download_url")
            )
            url = tmpl.format(pfaf=pfaf) if tmpl else None
            if self._base_url:
                url = f"{self._base_url}/flowdir/flowdir{pfaf}.tif"
            if url:
                rel = self._manifest.get("basin_template", {}).get("flowdir", {}).get(
                    "relative_dir", "raster/flowdir_basins"
                )
                dest = self.root / rel / f"flowdir{pfaf}.tif"
                if self._try_download(url, dest):
                    downloaded.append(f"flowdir_{pfaf}")
                    self._write_flowdir_metadata(pfaf, dest, source=url)
                    status = self.routing_region_cache(pfaf)

        status.acquisition_policy = acquisition_policy
        status.required_assets = required
        status.downloaded = downloaded or None
        if not status.flowdir_ready and acquisition_policy == "check_only":
            status.message = (
                f"Regional MERIT flowdir missing for Pfaf {pfaf}; call "
                "merit_ensure_routing_region(..., acquisition_policy='download') "
                "after reviewing the expected regional download."
            )
        return status

    def auto_stage_flowdir(
        self,
        pfaf: str,
        progress_cb: "Callable[[str, int, int], None] | None" = None,
    ) -> "RoutingRegionStatus":
        """
        Auto-stage MERIT flowdir for *pfaf* using the tiered policy.

        Tiered policy (from ~/.aihydro/config.json):
          ≤500 MB   — silent (progress_cb still called so callers can log)
          ≤max_gb   — stage with progress messages
          >max_gb   — return status with action_required = "confirm_large_download"
          policy="never" — return status unchanged, no download attempted

        *progress_cb(message, bytes_done, bytes_total)* — caller can stream to
        ctx.report_progress or log.  Pass None to suppress.

        Returns RoutingRegionStatus; check ``.flowdir_ready`` for success.
        """
        from typing import Callable  # local to avoid circular at module level
        import shutil

        policy, max_gb = _merit_auto_stage_policy()
        pfaf = pfaf.zfill(2)
        status = self.routing_region_cache(pfaf)

        if status.flowdir_ready:
            return status  # already staged — nothing to do

        if policy == "never":
            status.message = (
                "MERIT auto-staging is disabled (merit_auto_stage='never' in config). "
                "Set to 'tiered' or 'always' to enable on-demand staging."
            )
            return status

        # Resolve download URL
        tmpl = (
            self._manifest.get("basin_template", {})
            .get("flowdir", {})
            .get("download_url")
        )
        url = tmpl.format(pfaf=pfaf) if tmpl else None
        if self._base_url:
            url = f"{self._base_url}/flowdir/flowdir{pfaf}.tif"
        if not url:
            status.message = "No download URL configured for MERIT flowdir."
            return status

        # HEAD request for size estimate
        size_bytes = _estimate_flowdir_size_bytes(url)
        size_mb = (size_bytes or 0) / (1024 ** 2)
        size_gb = size_mb / 1024

        # Disk-free check
        free_bytes = shutil.disk_usage(self.root.parent).free
        free_gb = free_bytes / (1024 ** 3)
        if size_bytes and free_bytes < size_bytes * 2:
            status.message = (
                f"Not enough disk space to stage Pfaf {pfaf} flowdir "
                f"(needs {size_mb:.0f} MB, only {free_gb:.1f} GB free under "
                f"{self.root.parent}). Free up space or set AIHYDRO_CACHE_DIR."
            )
            if progress_cb:
                progress_cb(status.message, 0, 0)
            return status

        # Tiered gate for very large downloads
        max_bytes = max_gb * (1024 ** 3)
        if policy == "tiered" and size_bytes and size_bytes > max_bytes:
            status.message = (
                f"Pfaf {pfaf} flowdir is large (~{size_gb:.1f} GB > {max_gb:.0f} GB "
                f"threshold). To proceed: call merit_ensure_routing_region("
                f"pfaf_region='{pfaf}', acquisition_policy='download') explicitly, "
                f"or set merit_auto_stage_max_gb>{size_gb:.0f} in ~/.aihydro/config.json."
            )
            setattr(status, "action_required", "confirm_large_download")
            setattr(status, "size_gb", round(size_gb, 1))
            if progress_cb:
                progress_cb(status.message, 0, size_bytes or 0)
            return status

        # --- Proceed with download ---
        region_names = {
            "23": "Rhine / Main basin (Central Europe)",
            "24": "Danube basin (Central-Eastern Europe)",
            "51": "Ganges-Brahmaputra basin (South Asia)",
            "52": "Yangtze basin (East Asia)",
            "41": "Mississippi basin (North America)",
            "81": "Amazon basin (South America)",
        }
        region_label = region_names.get(pfaf, f"Pfaf {pfaf} region")
        size_label = f"{size_mb:.0f} MB" if size_mb < 1000 else f"{size_gb:.1f} GB"

        _start_msg = (
            f"Staging MERIT-Hydro flow-direction for the {region_label}. "
            f"This enables proper hydrological routing (MERIT-pyflwdir) instead of "
            f"raw DEM fallback. Source: Yamazaki et al. 2019 (CC-BY-NC). "
            f"Download: {size_label} → {self.root / 'raster/flowdir_basins'}"
        )
        log.info(_start_msg)
        if progress_cb:
            progress_cb(_start_msg, 0, size_bytes or 0)

        rel = self._manifest.get("basin_template", {}).get("flowdir", {}).get(
            "relative_dir", "raster/flowdir_basins"
        )
        dest = self.root / rel / f"flowdir{pfaf}.tif"
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Download with chunk-based progress reporting
        success = self._try_download_with_progress(url, dest, size_bytes, progress_cb)
        if success:
            self._write_flowdir_metadata(pfaf, dest, source=url)
            status = self.routing_region_cache(pfaf)
            _done_msg = (
                f"MERIT flowdir for {region_label} staged successfully "
                f"({dest.stat().st_size / (1024**2):.0f} MB). "
                f"Future delineations in this region will use MERIT-pyflwdir."
            )
            log.info(_done_msg)
            if progress_cb:
                progress_cb(_done_msg, size_bytes or 0, size_bytes or 0)
        else:
            status.message = f"Download failed for Pfaf {pfaf} flowdir from {url}."
            if progress_cb:
                progress_cb(status.message, 0, size_bytes or 0)

        return status

    def _try_download_with_progress(
        self,
        url: str,
        dest: Path,
        total_bytes: int | None,
        progress_cb: "Callable[[str, int, int], None] | None",
    ) -> bool:
        """Download *url* to *dest*, calling *progress_cb* every 5 MB."""
        import urllib.request
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            chunk_size = 5 * 1024 * 1024  # 5 MB
            downloaded = 0
            with urllib.request.urlopen(url, timeout=300) as resp, open(dest, "wb") as f:  # noqa: S310
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total_bytes:
                        pct = int(downloaded / total_bytes * 100)
                        mb_done = downloaded / (1024 ** 2)
                        mb_total = total_bytes / (1024 ** 2)
                        progress_cb(
                            f"Downloading… {mb_done:.0f} / {mb_total:.0f} MB ({pct}%)",
                            downloaded,
                            total_bytes,
                        )
            if dest.suffix == ".zip":
                import zipfile
                with zipfile.ZipFile(dest, "r") as zf:
                    zf.extractall(dest.parent)
            return dest.exists()
        except Exception as exc:
            log.warning("_try_download_with_progress failed for %s: %s", url, exc)
            return False

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
