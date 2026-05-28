#!/usr/bin/env python3
"""
Compare NLDI gauge delineation vs pour-point (fast tier) vs USGS drainage area.

Usage:
  python scripts/validate_delineation_basins.py
  python scripts/validate_delineation_basins.py --quick   # 2 gauges only
"""

from __future__ import annotations

import argparse
import json
import sys
import time

# Representative US basins (HydroCatch verification + reference gauges)
BASINS = [
    {"id": "11141280", "name": "Lopez Creek CA", "lat": 35.03, "lon": -120.48, "area_km2_pub": 54.13},
    {"id": "03182500", "name": "Greenbrier River WV", "lat": 37.78, "lon": -80.30, "area_km2_pub": 1396},
    {"id": "12045500", "name": "Elwha River WA", "lat": 48.09, "lon": -123.55, "area_km2_pub": 697},
    {"id": "01031500", "name": "Mattawamkeag ME", "lat": 45.87, "lon": -68.33, "area_km2_pub": None},
    {"id": "02361000", "name": "Sepulga River AL", "lat": 31.48, "lon": -86.87, "area_km2_pub": None},
    {"id": "09380000", "name": "Colorado R Lees Ferry", "lat": 36.86, "lon": -111.59, "area_km2_pub": None},
]


def fetch_nwis_area_km2(gauge_id: str) -> float | None:
    try:
        from pygeohydro import NWIS

        info = NWIS().get_info({"site": gauge_id})
        if info is None or info.empty:
            return None
        row = info.iloc[0]
        # drainage area often in sq mi
        for col in ("drain_area", "DrainageArea", "contrib_drain_area"):
            if col in info.columns:
                val = row.get(col)
                if val is not None and float(val) > 0:
                    # NWIS often sq mi
                    return float(val) * 2.58999
        if "drain_area_va" in str(row):
            pass
        # pygeohydro standard field
        if hasattr(row, "drainage_area"):
            da = row.drainage_area
            if da and float(da) > 0:
                return float(da) * 2.58999
    except Exception as e:
        print(f"  NWIS area lookup failed: {e}", file=sys.stderr)
    return None


def pct_error(actual: float, expected: float) -> float:
    if expected <= 0:
        return float("nan")
    return 100.0 * abs(actual - expected) / expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="Run first 2 basins only")
    parser.add_argument("--skip-fast", action="store_true", help="Skip slow pour-point tier")
    args = parser.parse_args()

    basins = BASINS[:2] if args.quick else BASINS

    from ai_hydro.analysis.watershed import delineate_watershed

    rows = []
    print(
        f"{'Gauge':<10} {'Name':<18} {'Pub':>8} {'GaugeNLDI':>9} {'%e':>6} "
        f"{'Auto@pt':>9} {'%e':>6} {'Fast':>8} {'%e':>6}"
    )
    print("-" * 100)

    for b in basins:
        gid = b["id"]
        name = b["name"][:22]
        pub = b.get("area_km2_pub") or fetch_nwis_area_km2(gid)
        pub_s = f"{pub:.1f}" if pub else "—"

        nldi_area = None
        nldi_err = None
        auto_area = None
        auto_err = None
        fast_area = None
        fast_err = None
        nldi_note = ""

        try:
            r = delineate_watershed(gid)
            nldi_area = r.data.get("area_km2")
            if pub and nldi_area:
                nldi_err = pct_error(nldi_area, pub)
        except Exception as e:
            nldi_note = str(e)[:40]

        try:
            from ai_hydro.analysis.delineation.router import delineate_from_point

            ar = delineate_from_point(
                b["lat"], b["lon"], method="auto", expected_area_km2=pub
            )
            auto_area = ar.data.get("area_km2")
            auto_method = ar.data.get("method_used")
            if pub and auto_area:
                auto_err = pct_error(auto_area, pub)
            nldi_note = auto_method or ""
        except Exception as e:
            nldi_note = (nldi_note + " auto:" + str(e)[:25]).strip()

        if not args.skip_fast:
            try:
                from ai_hydro.analysis.delineation.router import delineate_from_point

                fr = delineate_from_point(
                    b["lat"], b["lon"], method="fast", expected_area_km2=pub
                )
                fast_area = fr.data.get("area_km2")
                if pub and fast_area:
                    fast_err = pct_error(fast_area, pub)
            except Exception as e:
                nldi_note = (nldi_note + " fast:" + str(e)[:20]).strip()

        def fmt(v, err=None):
            if v is None:
                return "—", "—"
            return f"{v:.0f}", f"{err:.0f}" if err is not None and err == err else "—"

        na, ne = fmt(nldi_area, nldi_err)
        aa, ae = fmt(auto_area, auto_err)
        fa, fe = fmt(fast_area, fast_err)
        print(
            f"{gid:<10} {name:<18} {pub_s:>8} {na:>9} {ne:>6} {aa:>9} {ae:>6} {fa:>8} {fe:>6}  {nldi_note[:12]}"
        )
        rows.append(
            {
                "gauge_id": gid,
                "name": b["name"],
                "published_area_km2": pub,
                "gauge_nldi_km2": nldi_area,
                "gauge_nldi_pct_error": nldi_err,
                "auto_point_km2": auto_area,
                "auto_pct_error": auto_err,
                "fast_km2": fast_area,
                "fast_pct_error": fast_err,
                "notes": nldi_note,
            }
        )

    out_path = "/tmp/delineation_validation.json"
    with open(out_path, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"\nWrote {out_path}")

    # Summary
    g_errs = [r["gauge_nldi_pct_error"] for r in rows if r.get("gauge_nldi_pct_error") is not None]
    a_errs = [r["auto_pct_error"] for r in rows if r.get("auto_pct_error") is not None]
    f_errs = [r["fast_pct_error"] for r in rows if r.get("fast_pct_error") is not None]
    if g_errs:
        print(f"Gauge NLDI  median |error|: {sorted(g_errs)[len(g_errs)//2]:.0f}%  max: {max(g_errs):.0f}%")
    if a_errs:
        print(f"Auto@point median |error|: {sorted(a_errs)[len(a_errs)//2]:.0f}%  max: {max(a_errs):.0f}%")
    if f_errs:
        print(f"Fast DEM   median |error|: {sorted(f_errs)[len(f_errs)//2]:.0f}%  max: {max(f_errs):.0f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
