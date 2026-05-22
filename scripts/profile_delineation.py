#!/usr/bin/env python3
"""Profile pour-point delineation tiers (CONUS test points)."""

from __future__ import annotations

import json
import sys
import time
from typing import Any

POINTS = [
    ("kansas_big_blue", 40.71829, -96.41265),
    ("indiana_wabash", 40.44, -86.83),
    ("california_sac", 38.58, -121.49),
]


def _sec(t0: float) -> float:
    return round(time.perf_counter() - t0, 2)


def profile_nldi(lat: float, lon: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    t0 = time.perf_counter()
    from ai_hydro.analysis.delineation.nldi_point import (
        delineate_nldi_at_point,
        is_conus,
        snap_outlet_nldi,
    )

    out["is_conus"] = is_conus(lat, lon)
    t1 = time.perf_counter()
    lat2, lon2, ok = snap_outlet_nldi(lat, lon)
    out["snap_s"] = _sec(t1)
    out["snapped"] = ok
    t2 = time.perf_counter()
    r = delineate_nldi_at_point(lat, lon)
    out["nearest_comid_s"] = _sec(t2)
    out["area_km2"] = r.data.get("area_km2")
    out["comid"] = r.data.get("comid")
    out["method"] = r.data.get("method_used")
    out["total_s"] = _sec(t0)
    return out


def profile_fast_stages(lat: float, lon: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    from ai_hydro.analysis.delineation.nldi_point import is_conus, nldi_basin_gdf, snap_outlet_nldi
    from ai_hydro.analysis.delineation.merit_snap import snap_outlet_to_merit_rivers

    t0 = time.perf_counter()
    if is_conus(lat, lon):
        t = time.perf_counter()
        snap_outlet_nldi(lat, lon)
        out["nldi_snap_s"] = _sec(t)
        t = time.perf_counter()
        gdf = nldi_basin_gdf(lat, lon)
        out["nldi_basin_gdf_s"] = _sec(t)
        out["nldi_basin_km2"] = None if gdf is None else round(float(gdf.to_crs(4326).geometry.area) * 111**2, 1)
    t = time.perf_counter()
    snap = snap_outlet_to_merit_rivers(lat, lon)
    out["merit_snap_s"] = _sec(t)
    out["merit_snap_ok"] = snap.success
    out["prefetch_s"] = _sec(t0)
    return out


def profile_method(lat: float, lon: float, method: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    if method == "nldi":
        from ai_hydro.analysis.delineation.nldi_point import delineate_nldi_at_point

        r = delineate_nldi_at_point(lat, lon)
        return {
            "method": method,
            "method_used": r.data.get("method_used"),
            "area_km2": r.data.get("area_km2"),
            "comid": r.data.get("comid"),
            "total_s": _sec(t0),
        }
    from ai_hydro.analysis.delineation.router import delineate_from_point

    r = delineate_from_point(lat, lon, method=method)  # type: ignore[arg-type]
    return {
        "method": method,
        "method_used": r.data.get("method_used"),
        "area_km2": r.data.get("area_km2"),
        "total_s": _sec(t0),
    }


def main() -> None:
    results: dict[str, Any] = {}
    for name, lat, lon in POINTS:
        pt: dict[str, Any] = {"lat": lat, "lon": lon}
        print(f"=== {name} ===", file=sys.stderr)
        print("  nldi nearest...", file=sys.stderr)
        pt["nldi_nearest"] = profile_nldi(lat, lon)
        print("  fast prefetch...", file=sys.stderr)
        pt["fast_prefetch"] = profile_fast_stages(lat, lon)
        for method in ("auto", "fast", "nldi"):
            print(f"  {method}...", file=sys.stderr)
            pt[method] = profile_method(lat, lon, method)
        results[name] = pt
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
