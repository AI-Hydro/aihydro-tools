#!/usr/bin/env python3
"""Prefetch MERIT basin assets for a pour point (wrapper around MeritDataManager)."""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure MERIT data for (lat, lon)")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--no-download", action="store_true", help="Only report status")
    args = parser.parse_args()

    from ai_hydro.data.merit_manager import MeritDataManager

    mgr = MeritDataManager()
    status = mgr.ensure_basin(args.lat, args.lon, download=not args.no_download)
    print(f"Pfafstetter basin: {status.pfaf_code}")
    print(f"  level2: {status.level2_ready}")
    print(f"  rivers: {status.rivers_ready}")
    print(f"  catchments: {status.catchments_ready}")
    print(f"  flowdir: {status.flowdir_ready}")
    print(f"  delineator_ready: {mgr.delineator_ready(status.pfaf_code)}")
    print(f"  root: {mgr.root}")
    print(status.message)
    if status.downloaded:
        print("Downloaded:", ", ".join(status.downloaded))
    return 0 if status.rivers_ready or status.level2_ready else 1


if __name__ == "__main__":
    sys.exit(main())
