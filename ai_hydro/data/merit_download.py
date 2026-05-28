"""
Download MERIT-Basins vector shapefiles from the public Google Drive mirror.

Used when ``AIHYDRO_MERIT_BASE_URL`` is not set (typical for India / global users).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# MERIT-Basins river vectors (all Pfaf basins) — reachhydro.org mirror
_RIVERS_GDRIVE_FOLDER = "https://drive.google.com/drive/folders/1uCQFmdxFbjwoT9OYJxw-pXaP8q_GYH1a"
_DELINEATOR_SAMPLE = Path("/tmp/delineator/data/shp")


def _ensure_gdown() -> None:
    try:
        import gdown  # noqa: F401
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "gdown", "-q"])


def _copy_level2_from_delineator(dest: Path) -> bool:
    src = _DELINEATOR_SAMPLE / "basins_level2"
    shp = src / "merit_hydro_vect_level2.shp"
    if not shp.exists():
        return False
    dest.mkdir(parents=True, exist_ok=True)
    for f in src.glob("merit_hydro_vect_level2.*"):
        if f.is_file():
            import shutil

            shutil.copy2(f, dest / f.name)
    return (dest / "merit_hydro_vect_level2.shp").exists()


def ensure_level2_index(level2_dir: Path, *, clone_delineator: bool = True) -> bool:
    """Install global Pfaf level-2 index (~small) from delineator sample repo."""
    if (level2_dir / "merit_hydro_vect_level2.shp").exists():
        return True
    if clone_delineator and not _DELINEATOR_SAMPLE.exists():
        try:
            subprocess.check_call(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "https://github.com/mheberger/delineator.git",
                    str(_DELINEATOR_SAMPLE.parent),
                ],
                timeout=300,
            )
        except Exception as e:
            log.warning("delineator clone for level-2 failed: %s", e)
    return _copy_level2_from_delineator(level2_dir)


def _download_vector_shapefile(pfaf: str, dest_dir: Path, *, prefix_kind: str) -> bool:
    """
    Download ``riv_pfaf_##`` or ``cat_pfaf_##`` components into ``dest_dir``.

    Returns True when ``.shp`` exists after download.
    """
    pfaf = str(int(pfaf)).zfill(2)
    dest_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{prefix_kind}_pfaf_{pfaf}_MERIT_Hydro_v07_Basins_v01"
    shp = dest_dir / f"{prefix}.shp"
    if shp.exists():
        return True

    _ensure_gdown()
    import gdown

    label = "rivers" if prefix_kind == "riv" else "catchments"
    log.info("Downloading MERIT %s for Pfaf %s from Google Drive…", label, pfaf)
    files = gdown.download_folder(_RIVERS_GDRIVE_FOLDER, skip_download=True, quiet=True)
    targets = {
        getattr(f, "path", ""): getattr(f, "id", "")
        for f in files
        if f"{prefix_kind}_pfaf_{pfaf}_" in getattr(f, "path", "")
    }
    if not targets:
        log.warning("No Google Drive files found for %s_pfaf_%s", prefix_kind, pfaf)
        return False

    for path, fid in targets.items():
        name = Path(path).name
        out = dest_dir / name
        if out.exists():
            continue
        gdown.download(id=fid, output=str(out), quiet=False)

    return shp.exists()


def download_river_shapefile(pfaf: str, dest_dir: Path) -> bool:
    """Download ``riv_pfaf_##`` shapefile components into ``dest_dir``."""
    return _download_vector_shapefile(pfaf, dest_dir, prefix_kind="riv")


def download_catchment_shapefile(pfaf: str, dest_dir: Path) -> bool:
    """Download ``cat_pfaf_##`` shapefile components into ``dest_dir``."""
    return _download_vector_shapefile(pfaf, dest_dir, prefix_kind="cat")
