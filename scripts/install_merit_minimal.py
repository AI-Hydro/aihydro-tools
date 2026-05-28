#!/usr/bin/env python3
"""
Install minimal MERIT-Basins vectors for outlet snapping (level-2 index + river flowlines).

Downloads from the public MERIT-Basins Google Drive mirror (reachhydro.org).
Requires: pip install gdown

Usage:
  python scripts/install_merit_minimal.py --lat 35.03 --lon -120.48
  python scripts/install_merit_minimal.py --pfaf 77,78,73,74
  python scripts/install_merit_minimal.py --from-delineator-sample  # Iceland pfaf 27 only
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

_DELINEATOR_SAMPLE = Path("/tmp/delineator/data/shp")


def _copy_level2_from_delineator(dest: Path) -> bool:
    from ai_hydro.data.merit_download import ensure_level2_index

    return ensure_level2_index(dest, clone_delineator=False)


def _copy_riv_from_delineator(pfaf: str, dest: Path) -> bool:
    pfaf = str(int(pfaf)).zfill(2)
    src = _DELINEATOR_SAMPLE / "merit_rivers"
    for f in src.glob(f"riv_pfaf_{pfaf}_*"):
        shutil.copy2(f, dest / f.name)
    return any(dest.glob(f"riv_pfaf_{pfaf}_*.shp"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Install minimal MERIT vectors for snapping")
    parser.add_argument("--lat", type=float, help="Outlet latitude (resolves Pfaf basin)")
    parser.add_argument("--lon", type=float, help="Outlet longitude")
    parser.add_argument("--pfaf", type=str, help="Comma-separated Pfaf/BASIN codes (e.g. 77,78)")
    parser.add_argument(
        "--from-delineator-sample",
        action="store_true",
        help="Copy level-2 + Iceland rivers from /tmp/delineator clone",
    )
    parser.add_argument(
        "--clone-delineator",
        action="store_true",
        help="git clone mheberger/delineator to /tmp/delineator first",
    )
    args = parser.parse_args()

    from ai_hydro.data.merit_manager import MeritDataManager

    mgr = MeritDataManager()
    level2_dir = mgr.level2_shapefile_path().parent
    rivers_dir = mgr.river_shapefile_path("11").parent

    if args.clone_delineator or args.from_delineator_sample:
        if not _DELINEATOR_SAMPLE.exists():
            subprocess.check_call(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "https://github.com/mheberger/delineator.git",
                    str(_DELINEATOR_SAMPLE.parent),
                ]
            )
        _copy_level2_from_delineator(level2_dir)
        if args.from_delineator_sample:
            _copy_riv_from_delineator("27", rivers_dir)

    pfafs: list[str] = []
    if args.pfaf:
        pfafs = [p.strip() for p in args.pfaf.split(",") if p.strip()]
    elif args.lat is not None and args.lon is not None:
        if not mgr.level2_shapefile_path().exists():
            print("Level-2 index missing; run with --clone-delineator first.", file=sys.stderr)
            return 1
        pfafs = [mgr.resolve_pfaf_code(args.lat, args.lon)]
    else:
        pfafs = ["77", "78", "73", "74"]

    if not mgr.level2_shapefile_path().exists():
        print("Installing global level-2 index from delineator sample...")
        if not _copy_level2_from_delineator(level2_dir):
            if args.clone_delineator:
                subprocess.check_call(
                    [
                        "git",
                        "clone",
                        "--depth",
                        "1",
                        "https://github.com/mheberger/delineator.git",
                        str(_DELINEATOR_SAMPLE.parent),
                    ]
                )
                _copy_level2_from_delineator(level2_dir)
            if not mgr.level2_shapefile_path().exists():
                print("Failed to install level-2 index.", file=sys.stderr)
                return 1

    from ai_hydro.data.merit_download import download_river_shapefile

    for pfaf in pfafs:
        if mgr.river_shapefile_path(pfaf).exists():
            print(f"  rivers pfaf {pfaf}: already installed")
            continue
        if _copy_riv_from_delineator(pfaf, rivers_dir):
            print(f"  rivers pfaf {pfaf}: copied from delineator sample")
            continue
        print(f"  rivers pfaf {pfaf}: downloading from Google Drive...")
        download_river_shapefile(pfaf, rivers_dir)

    status = (
        mgr.ensure_basin(args.lat or 40.0, args.lon or -100.0, download=False)
        if args.lat is not None
        else None
    )
    print(f"\nMERIT root: {mgr.root}")
    print(f"  level2: {mgr.level2_shapefile_path().exists()}")
    for pfaf in pfafs:
        print(f"  riv_pfaf_{pfaf}: {mgr.river_shapefile_path(pfaf).exists()}")
    if status:
        print(f"  pfaf for ({args.lat}, {args.lon}): {status.pfaf_code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
