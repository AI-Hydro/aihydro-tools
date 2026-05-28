#!/usr/bin/env python
"""Benchmark adaptive-window local MERIT routing against NLDI references."""

from __future__ import annotations

import argparse
import json
import time
import traceback
from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely.geometry import shape

from ai_hydro.analysis.delineation.merit_flowdir_pipeline import (
    local_merit_flowdir_pyflwdir,
    merit_build_offline_snap_cache,
    merit_get_snap_reference,
)
from ai_hydro.analysis.delineation.nldi_point import delineate_nldi_at_point
from ai_hydro.data.merit_manager import MeritDataManager

PF74_POINTS = [
    {"group": "pfaf74", "name": "nebraska_test", "lat": 40.71829, "lon": -96.41265},
    {"group": "pfaf74", "name": "ia_1", "lat": 41.65, "lon": -93.55},
    {"group": "pfaf74", "name": "indiana_wabash", "lat": 40.44, "lon": -86.83},
    {"group": "pfaf74", "name": "white_river_indy", "lat": 39.77, "lon": -86.16},
    {"group": "pfaf74", "name": "wabash_lafayette", "lat": 40.42, "lon": -86.90},
    {"group": "pfaf74", "name": "illinois_flat", "lat": 40.00, "lon": -89.00},
    {"group": "pfaf74", "name": "ohio_flat", "lat": 40.00, "lon": -83.00},
    {"group": "pfaf74", "name": "minnesota", "lat": 46.00, "lon": -94.00},
    {"group": "pfaf74", "name": "south_dakota_rapid", "lat": 44.0697, "lon": -103.2310},
    {"group": "pfaf74", "name": "north_dakota", "lat": 47.00, "lon": -100.00},
]

FLAT_POINTS = [
    {"group": "indiana_flat", "name": "indiana_wabash", "lat": 40.44, "lon": -86.83},
    {"group": "indiana_flat", "name": "white_river_indy", "lat": 39.77, "lon": -86.16},
    {"group": "indiana_flat", "name": "wabash_lafayette", "lat": 40.42, "lon": -86.90},
    {"group": "indiana_flat", "name": "in_flat_1", "lat": 39.80, "lon": -86.20},
    {"group": "indiana_flat", "name": "ohio_flat", "lat": 40.00, "lon": -83.00},
]

LARGER_POINTS = [
    {"group": "larger", "name": "indiana_wabash", "lat": 40.44, "lon": -86.83},
    {"group": "larger", "name": "iowa_wapello", "lat": 41.19, "lon": -91.37},
    {"group": "larger", "name": "minnesota", "lat": 46.00, "lon": -94.00},
    {"group": "larger", "name": "north_dakota", "lat": 47.00, "lon": -100.00},
    {"group": "larger", "name": "montana", "lat": 46.00, "lon": -111.00},
]

SECOND_REGION_POINTS = [
    {"group": "second_region", "name": "lopez_ca", "lat": 35.03, "lon": -120.48},
    {"group": "second_region", "name": "sacramento_ca", "lat": 38.58, "lon": -121.49},
    {"group": "second_region", "name": "san_diego", "lat": 32.80, "lon": -117.10},
]

OFFLINE_POINTS = [
    {"group": "offline_prepared", "name": "nebraska_test", "lat": 40.71829, "lon": -96.41265},
    {"group": "offline_prepared", "name": "ia_1", "lat": 41.65, "lon": -93.55},
]


def _gdf_from_feature(feature: dict[str, Any]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[shape(feature["geometry"])], crs=4326)


def _iou(a: gpd.GeoDataFrame, b: gpd.GeoDataFrame) -> float | None:
    aa = a.to_crs(6933).geometry.iloc[0]
    bb = b.to_crs(6933).geometry.iloc[0]
    union = aa.union(bb).area
    return float(aa.intersection(bb).area / union) if union else None


def _run_point(point: dict[str, Any], *, offline: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = dict(point)
    mgr = MeritDataManager()
    started = time.perf_counter()
    try:
        row["pfaf_region"] = mgr.resolve_pfaf_region(float(point["lat"]), float(point["lon"]))
        nldi = delineate_nldi_at_point(float(point["lat"]), float(point["lon"]))
        nldi_gdf = _gdf_from_feature(nldi.data["geometry_geojson"])
        row["nldi_reference_area_km2"] = float(nldi.data["area_km2"])
        if offline:
            snap_reference = None
        else:
            snap_reference = merit_get_snap_reference(
                float(point["lat"]),
                float(point["lon"]),
                expected_area_km2=row["nldi_reference_area_km2"],
            )
            row["official_merit_upa_km2"] = snap_reference.official_merit_upa_km2
            row["snap_distance_m"] = snap_reference.snap.distance_m
            row["snap_quality"] = snap_reference.snap.snap_quality
        t0 = time.perf_counter()
        local = local_merit_flowdir_pyflwdir(
            float(point["lat"]),
            float(point["lon"]),
            expected_area_km2=row["nldi_reference_area_km2"],
            snap_reference=snap_reference,
            allow_offline=offline,
            force_offline=offline,
        )
        row["local_runtime_seconds_wall"] = round(time.perf_counter() - t0, 3)
        row["local_merit_polygon_area_km2"] = local.area_km2
        row["area_error_pct_vs_nldi"] = (
            abs(local.area_km2 - row["nldi_reference_area_km2"])
            / row["nldi_reference_area_km2"]
            * 100
        )
        row["iou_vs_nldi"] = _iou(local.gdf, nldi_gdf)
        official = local.official_merit_upa_km2
        row["official_merit_upa_km2"] = official
        row["official_merit_upa_error_pct"] = (
            abs(local.area_km2 - official) / official * 100 if official else None
        )
        row.update(
            {
                "snap_distance_m": local.snap_distance_m,
                "window_expansion_iterations": local.window_expansion_iterations,
                "final_window_cell_count": local.final_window_cell_count,
                "window_complete": local.window_complete,
                "runtime_seconds": local.runtime_seconds,
                "memory_telemetry": local.memory_telemetry,
                "quality_flags": local.quality_flags,
                "fallback_history": local.fallback_history,
                "validation_sources": local.validation_sources,
                "final_window_bounds": local.final_window_bounds,
            }
        )
        row["ok"] = True
    except Exception as exc:
        row["ok"] = False
        row["error"] = repr(exc)
        row["traceback"] = traceback.format_exc(limit=8)
    row["total_runtime_seconds"] = round(time.perf_counter() - started, 3)
    return row


def _export_indiana_artifacts(outdir: Path) -> dict[str, str]:
    outdir.mkdir(parents=True, exist_ok=True)
    lat, lon = 40.44, -86.83
    nldi = delineate_nldi_at_point(lat, lon)
    nldi_gdf = _gdf_from_feature(nldi.data["geometry_geojson"])
    snap = merit_get_snap_reference(lat, lon, expected_area_km2=float(nldi.data["area_km2"]))
    local = local_merit_flowdir_pyflwdir(
        lat,
        lon,
        expected_area_km2=float(nldi.data["area_km2"]),
        snap_reference=snap,
    )
    local_gdf = local.gdf.to_crs(4326)
    diff_geom = nldi_gdf.to_crs(6933).geometry.iloc[0].symmetric_difference(
        local_gdf.to_crs(6933).geometry.iloc[0]
    )
    diff_gdf = gpd.GeoDataFrame(geometry=[diff_geom], crs=6933).to_crs(4326)
    nldi_path = outdir / "indiana_wabash_nldi.geojson"
    local_path = outdir / "indiana_wabash_local_merit.geojson"
    diff_path = outdir / "indiana_wabash_symmetric_difference.geojson"
    nldi_gdf.to_file(nldi_path, driver="GeoJSON")
    local_gdf.to_file(local_path, driver="GeoJSON")
    diff_gdf.to_file(diff_path, driver="GeoJSON")
    return {"nldi": str(nldi_path), "local_merit": str(local_path), "symmetric_difference": str(diff_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/flowdir_first_envelope_benchmark.json")
    parser.add_argument("--artifacts-dir", default="/tmp/flowdir_first_indiana_artifacts")
    parser.add_argument("--quick", action="store_true", help="Run a short smoke benchmark.")
    parser.add_argument(
        "--group",
        choices=["all", "pfaf74", "flat", "larger", "second"],
        default="all",
        help="Benchmark group to run.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit online benchmark cases.")
    parser.add_argument("--skip-offline-cache-build", action="store_true")
    args = parser.parse_args()

    if args.quick:
        points = PF74_POINTS[:3]
    elif args.group == "pfaf74":
        points = PF74_POINTS
    elif args.group == "flat":
        points = FLAT_POINTS
    elif args.group == "larger":
        points = LARGER_POINTS
    elif args.group == "second":
        points = SECOND_REGION_POINTS
    else:
        points = PF74_POINTS + FLAT_POINTS + LARGER_POINTS + SECOND_REGION_POINTS
    if args.limit and args.limit > 0:
        points = points[: args.limit]
    out_path = Path(args.out)
    results: list[dict[str, Any]] = []
    for idx, point in enumerate(points, start=1):
        print(f"[{idx}/{len(points)}] {point['group']}:{point['name']}", flush=True)
        results.append(_run_point(point))
        out_path.write_text(
            json.dumps(
                {
                    "candidate": "local_merit_flowdir_pyflwdir adaptive-window",
                    "reference": "USGS NLDI / NHDPlus",
                    "results": results,
                    "partial": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    offline_cache: dict[str, Any] | None = None
    if not args.skip_offline_cache_build:
        t0 = time.perf_counter()
        try:
            offline_cache = merit_build_offline_snap_cache("74")
            offline_cache["build_runtime_seconds"] = round(time.perf_counter() - t0, 3)
            path = Path(offline_cache["local_upstream_area_path"])
            offline_cache["size_bytes"] = path.stat().st_size if path.exists() else None
        except Exception as exc:
            offline_cache = {
                "ok": False,
                "error": repr(exc),
                "traceback": traceback.format_exc(limit=8),
                "build_runtime_seconds": round(time.perf_counter() - t0, 3),
            }
    offline_results = [
        _run_point(p, offline=True)
        for p in (OFFLINE_POINTS[:1] if args.quick else OFFLINE_POINTS)
    ]
    artifacts = _export_indiana_artifacts(Path(args.artifacts_dir))
    payload = {
        "candidate": "local_merit_flowdir_pyflwdir adaptive-window",
        "reference": "USGS NLDI / NHDPlus",
        "results": results,
        "offline_cache": offline_cache,
        "offline_results": offline_results,
        "indiana_wabash_artifacts": artifacts,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
