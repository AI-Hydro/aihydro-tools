#!/usr/bin/env python
"""Compare MERIT GEE + pyflwdir delineation against NLDI reference basins."""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely.geometry import shape

from ai_hydro.analysis.delineation.router import delineate_from_point


DEFAULT_POINTS = [
    {"name": "nebraska_test", "lat": 40.71829, "lon": -96.41265},
]


def _load_points(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return DEFAULT_POINTS
    p = Path(path)
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("points", [])
        return list(data)
    rows: list[dict[str, Any]] = []
    with p.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def _compare_one(point: dict[str, Any]) -> dict[str, Any]:
    name = str(point.get("name") or "point")
    lat = float(point["lat"])
    lon = float(point["lon"])
    started = time.time()
    try:
        nldi = delineate_from_point(lat, lon, method="nldi", name=name)
        nldi_area = float(nldi.data["area_km2"])
        nldi_gdf = gpd.GeoDataFrame(
            geometry=[shape(nldi.data["geometry_geojson"]["geometry"])], crs=4326
        )
        t0 = time.time()
        merit = delineate_from_point(
            lat,
            lon,
            method="merit_gee",
            expected_area_km2=nldi_area,
            name=name,
        )
        merit_seconds = time.time() - t0
        merit_area = float(merit.data["area_km2"])
        merit_gdf = gpd.GeoDataFrame(
            geometry=[shape(merit.data["geometry_geojson"]["geometry"])], crs=4326
        )
        equal_area = "EPSG:6933"
        ref = nldi_gdf.to_crs(equal_area).geometry.iloc[0]
        test = merit_gdf.to_crs(equal_area).geometry.iloc[0]
        intersection_km2 = ref.intersection(test).area / 1e6
        union_km2 = ref.union(test).area / 1e6
        return {
            "name": name,
            "lat": lat,
            "lon": lon,
            "ok": True,
            "nldi_area_km2": round(nldi_area, 3),
            "merit_area_km2": round(merit_area, 3),
            "area_error_pct": round(abs(merit_area - nldi_area) / nldi_area * 100, 3),
            "intersection_km2": round(intersection_km2, 3),
            "union_km2": round(union_km2, 3),
            "iou": round(intersection_km2 / union_km2 if union_km2 else 0.0, 4),
            "merit_seconds": round(merit_seconds, 2),
            "total_seconds": round(time.time() - started, 2),
            "method_used": merit.data.get("method_used"),
            "routing_dataset": merit.data.get("routing_dataset"),
            "snap_distance_m": merit.data.get("snap_distance_m"),
            "snap_quality": merit.data.get("snap_quality"),
            "snap_validation": merit.data.get("snap_validation"),
            "snapped_upa_km2": merit.data.get("snapped_upa_km2"),
            "area_validation": merit.data.get("area_validation"),
            "quality_flags": merit.data.get("quality_flags"),
            "workflow_steps": merit.data.get("workflow_steps"),
            "cache_key": merit.data.get("cache_key"),
        }
    except Exception as exc:
        return {
            "name": name,
            "lat": lat,
            "lon": lon,
            "ok": False,
            "error": str(exc),
            "total_seconds": round(time.time() - started, 2),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", help="CSV or JSON with columns/keys: name, lat, lon")
    parser.add_argument("--out", help="Optional JSON output path")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args()

    results = [_compare_one(point) for point in _load_points(args.points)]
    ok_rows = [row for row in results if row.get("ok")]
    failed_rows = [row for row in results if not row.get("ok")]
    flagged_rows = [row for row in ok_rows if row.get("quality_flags")]
    area_errors = [float(row["area_error_pct"]) for row in ok_rows if "area_error_pct" in row]
    ious = [float(row["iou"]) for row in ok_rows if "iou" in row]
    summary = {
        "n_points": len(results),
        "n_success": len(ok_rows),
        "n_failed": len(failed_rows),
        "n_flagged_success": len(flagged_rows),
        "median_area_error_pct": round(sorted(area_errors)[len(area_errors) // 2], 3)
        if area_errors
        else None,
        "max_area_error_pct": round(max(area_errors), 3) if area_errors else None,
        "median_iou": round(sorted(ious)[len(ious) // 2], 4) if ious else None,
        "min_iou": round(min(ious), 4) if ious else None,
    }
    payload = {
        "reference": "USGS NLDI / NHDPlus",
        "candidate": "MERIT/Hydro/v1_0_1 via GEE + pyflwdir",
        "summary": summary,
        "results": results,
    }
    text = json.dumps(payload, indent=2 if args.pretty else None)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if all(row.get("ok") for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
