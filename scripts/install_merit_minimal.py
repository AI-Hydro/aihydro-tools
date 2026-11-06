#!/usr/bin/env python3
"""
Stage the pinned, SHA-256-verified MERIT-Basins vectors for one or more Pfafstetter regions.

For each region this installs ``megabasins.db`` (level-2 index), ``basins##.db`` and ``rivers##.db`` under
``$AIHYDRO_HOME/merit`` through ``MeritDataManager.ensure_basins_region`` (aihydro-watershed >= 0.4). A file
is installed only when its size and SHA-256 match the manifest pinned in the package; a mismatch is
reported and nothing unverified is used. There is no Google Drive / reachhydro route and no ``gdown``.

MERIT-Basins is licensed for non-commercial use; the licence and citation are printed with the status.

Usage:
  python scripts/install_merit_minimal.py --lat 35.03 --lon -120.48
  python scripts/install_merit_minimal.py --pfaf 77,78,73,74
  python scripts/install_merit_minimal.py --pfaf 27 --check-only   # report sizes, download nothing
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage verified MERIT-Basins vectors (basins##.db, rivers##.db)")
    parser.add_argument("--lat", type=float, help="Outlet latitude (resolves the Pfaf region)")
    parser.add_argument("--lon", type=float, help="Outlet longitude")
    parser.add_argument("--pfaf", type=str, help="Comma-separated Pfaf/BASIN codes (e.g. 77,78)")
    parser.add_argument("--check-only", action="store_true", help="Report readiness and download size only")
    args = parser.parse_args()

    from ai_hydro.data.merit_manager import MeritDataManager

    mgr = MeritDataManager()
    if args.pfaf:
        pfafs = [p.strip() for p in args.pfaf.split(",") if p.strip()]
    elif args.lat is not None and args.lon is not None:
        if args.check_only:
            if mgr.verified_megabasins() is None:
                print("Level-2 index (megabasins.db) is not staged; rerun without --check-only.", file=sys.stderr)
                return 1
        elif mgr.ensure_level2_index(download=True) is None:
            print("Failed to stage the verified level-2 index (megabasins.db).", file=sys.stderr)
            return 1
        pfafs = [mgr.resolve_pfaf_region(args.lat, args.lon)]
    else:
        pfafs = ["77", "78", "73", "74"]

    policy = "check_only" if args.check_only else "download"
    ok = True
    print(f"MERIT root: {mgr.root}")
    for pfaf in pfafs:
        status = mgr.ensure_basins_region(pfaf, acquisition_policy=policy)
        ready = status.catchments_ready and status.rivers_ready
        ok = ok and ready
        print(f"  pfaf {status.pfaf_region}: basins={status.catchments_ready} rivers={status.rivers_ready}")
        print(f"    {status.message}")
        if status.downloaded:
            print("    downloaded:", ", ".join(status.downloaded))
    print(f"\nLicence: {status.license}\nCite: {status.citation}")
    return 0 if ok or args.check_only else 1


if __name__ == "__main__":
    sys.exit(main())
