#!/usr/bin/env python3
"""
Compare fast-tier pour-point delineation vs gauge NLDI and snap ablations.

Reports whether MERIT vector snap ran (requires level2 + riv_pfaf_## locally).

Usage:
  python scripts/compare_fast_snap_tiers.py
  python scripts/compare_fast_snap_tiers.py --gauges 11141280,12045500
"""

from __future__ import annotations

import argparse
import sys

DEFAULT_GAUGES = [
    {"id": "11141280", "name": "Lopez Creek CA", "lat": 35.03, "lon": -120.48},
    {"id": "12045500", "name": "Elwha River WA", "lat": 48.09, "lon": -123.55},
    {"id": "01031500", "name": "Mattawamkeag ME", "lat": 45.87, "lon": -68.33},
    {"id": "03182500", "name": "Greenbrier River WV", "lat": 37.78, "lon": -80.30},
]


def pub_area_km2(gauge_id: str) -> float | None:
    try:
        from pygeohydro import NWIS

        info = NWIS().get_info({"site_no": gauge_id})
        if info is None or info.empty:
            return None
        for col in ("drain_area", "DrainageArea"):
            if col in info.columns:
                v = info.iloc[0][col]
                if v is not None and float(v) > 0:
                    return float(v) * 2.58999
    except Exception:
        pass
    return None


def pct_err(actual: float, expected: float) -> float:
    return 100.0 * abs(actual - expected) / expected if expected > 0 else float("nan")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--gauges",
        type=str,
        default="",
        help="Comma-separated USGS site IDs (default: built-in list)",
    )
    args = parser.parse_args()

    from ai_hydro.analysis.delineation.pysheds_pipeline import delineate_fast
    from ai_hydro.analysis.watershed import delineate_watershed
    from ai_hydro.data.merit_manager import MeritDataManager

    basins = DEFAULT_GAUGES
    if args.gauges.strip():
        ids = [g.strip() for g in args.gauges.split(",") if g.strip()]
        basins = [b for b in DEFAULT_GAUGES if b["id"] in ids]
        if not basins:
            print("No matching gauges in built-in list.", file=sys.stderr)
            return 1

    mgr = MeritDataManager()
    level2 = mgr.level2_shapefile_path().exists()
    print(f"MERIT root: {mgr.root}")
    print(f"MERIT level-2 index: {'yes' if level2 else 'NO — install merit_hydro_vect_level2.shp'}")
    print()

    modes = [
        ("no_snap", False, False),
        ("nldi_only", True, False),
        ("merit_vec", False, True),
        ("default", True, True),
    ]

    print(
        f"{'Gauge':<10} {'Pub':>7} {'NLDI_g':>8} | "
        f"{'mode':<11} {'km2':>8} {'%err':>6} {'merit_m':>8}"
    )
    print("-" * 72)

    for b in basins:
        pub = pub_area_km2(b["id"])
        nldi_g = None
        try:
            r = delineate_watershed(b["id"])
            nldi_g = r.data.get("area_km2")
        except Exception as e:
            nldi_g = f"ERR"

        pub_s = f"{pub:.0f}" if pub else "—"
        nldi_s = f"{nldi_g:.0f}" if isinstance(nldi_g, (int, float)) else str(nldi_g)[:8]
        print(f"{b['id']:<10} {pub_s:>7} {nldi_s:>8} |")

        for label, use_nldi, use_merit in modes:
            try:
                fr = delineate_fast(
                    b["lat"],
                    b["lon"],
                    use_nldi_snap=use_nldi,
                    use_merit_vector_snap=use_merit,
                    expected_area_km2=pub if pub else None,
                )
                err = pct_err(fr.area_km2, pub) if pub else float("nan")
                err_s = f"{err:.0f}" if err == err else "—"
                snap_s = (
                    f"{fr.merit_snap_distance_m:.0f}"
                    if fr.merit_snap_distance_m is not None
                    else "—"
                )
                print(
                    f"{'':10} {'':7} {'':8} | "
                    f"{label:<11} {fr.area_km2:>8.1f} {err_s:>6} {snap_s:>8}"
                )
            except Exception as e:
                print(f"{'':10} {'':7} {'':8} | {label:<11} {'FAIL':>8} {'—':>6} {str(e)[:28]}")

        print()

    print(
        "Interpretation:\n"
        "  NLDI_g = delineate_watershed(site_id) — best CONUS reference.\n"
        "  merit_vec = MERIT riv_pfaf snap only (needs ~/.aihydro/merit vectors).\n"
        "  nldi_only = pynhd COMID snap + NLDI basin when area matches expected.\n"
        "  If merit_m stays '—', install level2 + riv_pfaf for the outlet basin."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
