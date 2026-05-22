"""
CLI bridge for AI-Hydro map panel (MERIT vectors + pour-point delineation).

Usage:
  python -m ai_hydro.hydro_map_cli merit-ensure-basin --lat 30 --lon 76 --json
  python -m ai_hydro.hydro_map_cli merit-ensure-region --preset south_asia --json
  python -m ai_hydro.hydro_map_cli merit-layers --lat 30 --lon 76 --json
  python -m ai_hydro.hydro_map_cli delineate-point --lat 30 --lon 76 --json
  python -m ai_hydro.hydro_map_cli wbd-layers --lat 40 --lon -86 --huc-level 8 --json
  python -m ai_hydro.hydro_map_cli search-hydrology --q colorado --min-lon -109 --min-lat 36 --max-lon -102 --max-lat 41 --json
  python -m ai_hydro.hydro_map_cli gauges-in-view --lat 40 --lon -86 --min-lon -88 --min-lat 39 --max-lon -85 --max-lat 41 --json
  python -m ai_hydro.hydro_map_cli dams-in-view --lat 40 --lon -86 --min-lon -88 --min-lat 39 --max-lon -85 --max-lat 41 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str))


def cmd_merit_ensure_basin(args: argparse.Namespace) -> dict[str, Any]:
    from ai_hydro.data.merit_manager import MeritDataManager

    mgr = MeritDataManager()
    status = mgr.ensure_basin(args.lat, args.lon, download=not args.no_download)
    return {
        "ok": status.rivers_ready or status.level2_ready,
        "type": "merit_ensure_basin",
        "pfaf_code": status.pfaf_code,
        "level2_ready": status.level2_ready,
        "rivers_ready": status.rivers_ready,
        "catchments_ready": status.catchments_ready,
        "merit_root": str(mgr.root),
        "message": status.message,
        "downloaded": status.downloaded,
    }


def cmd_merit_ensure_region(args: argparse.Namespace) -> dict[str, Any]:
    import geopandas as gpd
    from shapely.geometry import box

    from ai_hydro.data.merit_manager import MeritDataManager
    from ai_hydro.data.region_presets import REGION_BBOX, pfaf_codes_for_preset

    mgr = MeritDataManager()
    preset = args.preset.strip().lower()
    if args.lat is not None and args.lon is not None:
        mgr.ensure_basin(args.lat, args.lon, download=not args.no_download)
    else:
        min_lon, min_lat, max_lon, max_lat = REGION_BBOX[preset]
        cx, cy = (min_lon + max_lon) / 2, (min_lat + max_lat) / 2
        mgr.ensure_basin(cy, cx, download=not args.no_download)

    codes = pfaf_codes_for_preset(preset)
    min_lon, min_lat, max_lon, max_lat = REGION_BBOX[preset]
    region = box(min_lon, min_lat, max_lon, max_lat)
    l2 = mgr.level2_shapefile_path()
    if l2.exists():
        gdf = gpd.read_file(l2)
        if gdf.crs is None:
            gdf = gdf.set_crs(4326)
        gdf = gdf.to_crs(4326)
        for _, row in gdf[gdf.geometry.intersects(region)].iterrows():
            raw = row.get("BASIN") or row.get("pfaf_code") or row.get("PFAF_ID") or ""
            try:
                pfaf = str(int(raw)).zfill(2)
            except (TypeError, ValueError):
                continue
            if pfaf not in codes:
                continue
            rep = row.geometry.representative_point()
            mgr.ensure_basin(rep.y, rep.x, download=not args.no_download)

    ready = sum(1 for pfaf in codes if mgr.river_shapefile_path(pfaf).exists())
    return {
        "ok": ready > 0 or mgr.level2_shapefile_path().exists(),
        "type": "merit_ensure_region",
        "preset": preset,
        "pfaf_codes": codes,
        "rivers_ready_count": ready,
        "pfaf_count": len(codes),
        "merit_root": str(mgr.root),
        "message": f"Preset {preset}: {ready}/{len(codes)} Pfaf river sets ready",
    }


def _merit_layers_message(
    layers: list[dict[str, Any]],
    *,
    include_catchments: bool,
    lat: float,
    lon: float,
) -> str:
    if not layers:
        return "No MERIT vectors in view (install rivers first)"
    kinds: list[str] = []
    for layer in layers:
        kind = (layer.get("metadata") or {}).get("merit_layer", "layer")
        kinds.append(str(kind))
    summary = f"{len(layers)} layer(s) on map ({', '.join(kinds)})"
    if not include_catchments:
        return summary
    if "catchments" in kinds:
        return summary
    from ai_hydro.data.merit_manager import MeritDataManager

    mgr = MeritDataManager()
    try:
        pfaf = mgr.resolve_pfaf_code(lat, lon)
    except Exception:
        pfaf = "??"
    cat_path = mgr.catchment_shapefile_path(pfaf)
    if not cat_path.exists():
        return (
            f"{summary}. Catchments requested but not installed "
            f"(missing {cat_path.name}). Load rivers only downloads riv_pfaf_* — "
            "install cat_pfaf_* separately for map polygons or use Delineate with agent."
        )
    return (
        f"{summary}. Catchment file exists but none intersect the current map view "
        "(zoom in or widen the view)."
    )


def cmd_merit_layers(args: argparse.Namespace) -> dict[str, Any]:
    from ai_hydro.data.merit_map_layers import merit_map_layers_for_view

    layers = merit_map_layers_for_view(
        lat=args.lat,
        lon=args.lon,
        min_lon=args.min_lon,
        min_lat=args.min_lat,
        max_lon=args.max_lon,
        max_lat=args.max_lat,
        include_level2=not args.no_level2,
        include_rivers=not args.no_rivers,
        include_catchments=args.catchments,
        pfaf_codes=[c.zfill(2) for c in args.pfaf] if args.pfaf else None,
    )
    return {
        "ok": len(layers) > 0,
        "type": "merit_layers",
        "layers": layers,
        "message": _merit_layers_message(
            layers,
            include_catchments=bool(args.catchments),
            lat=args.lat,
            lon=args.lon,
        ),
    }


def cmd_delineate_point(args: argparse.Namespace) -> dict[str, Any]:
    from ai_hydro.mcp.helpers import _normalize_session_id
    from ai_hydro.mcp.tools_analysis import delineate_watershed_from_point

    session_id = _normalize_session_id(args.session_id)
    result = delineate_watershed_from_point(
        session_id=session_id,
        lat=args.lat,
        lon=args.lon,
        workspace_dir=args.workspace_dir,
        expected_area_km2=args.expected_area_km2,
        method=args.method,
        name=args.name,
    )
    if result.get("error") or result.get("code"):
        return {"ok": False, "type": "delineate_point", "message": result.get("message", "Delineation failed"), **result}
    return {
        "ok": True,
        "type": "delineate_point",
        "data": result.get("data", {}),
        "message": f"Delineated {result.get('data', {}).get('area_km2', '?')} km² ({result.get('data', {}).get('method_used', '')})",
    }


def cmd_list_presets(_args: argparse.Namespace) -> dict[str, Any]:
    from ai_hydro.data.region_presets import list_region_presets

    return {"ok": True, "type": "merit_presets", "presets": list_region_presets()}


def cmd_wbd_layers(args: argparse.Namespace) -> dict[str, Any]:
    from ai_hydro.data.wbd_layers import is_conus_point, wbd_map_layers_for_view

    if not is_conus_point(args.lat, args.lon):
        return {
            "ok": False,
            "type": "wbd_layers",
            "layers": [],
            "message": "WBD hydrologic units are available for CONUS only (lat 24–50°, lon -125–-66°).",
        }
    layers = wbd_map_layers_for_view(
        lat=args.lat,
        lon=args.lon,
        min_lon=args.min_lon,
        min_lat=args.min_lat,
        max_lon=args.max_lon,
        max_lat=args.max_lat,
        huc_level=args.huc_level,
    )
    return {
        "ok": len(layers) > 0,
        "type": "wbd_layers",
        "layers": layers,
        "message": (
            f"Added {len(layers)} WBD layer(s) for HUC{args.huc_level or 8}"
            if layers
            else "No WBD units in this view (zoom in or check CONUS extent)"
        ),
    }


def cmd_huc_at_point(args: argparse.Namespace) -> dict[str, Any]:
    from ai_hydro.data.wbd_layers import huc_at_point, is_conus_point

    if not is_conus_point(args.lat, args.lon):
        return {
            "ok": False,
            "type": "huc_at_point",
            "message": "HUC lookup is CONUS-only.",
        }
    info = huc_at_point(args.lat, args.lon, huc_level=args.huc_level)
    if not info:
        return {"ok": False, "type": "huc_at_point", "message": "No HUC unit found at this point."}
    return {"ok": True, "type": "huc_at_point", "huc": info, "message": info.get("label", "")}


def cmd_search_hydrology(args: argparse.Namespace) -> dict[str, Any]:
    from ai_hydro.data.hydro_search import search_hydrology

    hits = search_hydrology(
        args.q,
        min_lon=args.min_lon,
        min_lat=args.min_lat,
        max_lon=args.max_lon,
        max_lat=args.max_lat,
        limit=args.limit,
    )
    return {
        "ok": True,
        "type": "search_hydrology",
        "hits": hits,
        "message": f"{len(hits)} result(s)",
    }


def cmd_gauges_in_view(args: argparse.Namespace) -> dict[str, Any]:
    from ai_hydro.data.hydro_search import gauges_in_view_layer
    from ai_hydro.data.wbd_layers import is_conus_point

    if not is_conus_point(args.lat, args.lon):
        return {
            "ok": False,
            "type": "gauges_in_view",
            "layers": [],
            "message": "USGS gauge layers are available for CONUS only.",
        }
    if args.min_lon is None or args.min_lat is None or args.max_lon is None or args.max_lat is None:
        return {
            "ok": False,
            "type": "gauges_in_view",
            "layers": [],
            "message": "Map bounding box (min/max lon/lat) is required.",
        }
    layers = gauges_in_view_layer(
        min_lon=args.min_lon,
        min_lat=args.min_lat,
        max_lon=args.max_lon,
        max_lat=args.max_lat,
        lat=args.lat,
        lon=args.lon,
        limit=args.limit,
    )
    return {
        "ok": len(layers) > 0,
        "type": "gauges_in_view",
        "layers": layers,
        "message": (
            layers[0]["name"] if layers else "No active USGS streamgages in this view."
        ),
    }


def cmd_dams_in_view(args: argparse.Namespace) -> dict[str, Any]:
    from ai_hydro.data.hydro_search import dams_in_view_layer
    from ai_hydro.data.wbd_layers import is_conus_point

    if not is_conus_point(args.lat, args.lon):
        return {
            "ok": False,
            "type": "dams_in_view",
            "layers": [],
            "message": "NID dam layers are available for CONUS only.",
        }
    if args.min_lon is None or args.min_lat is None or args.max_lon is None or args.max_lat is None:
        return {
            "ok": False,
            "type": "dams_in_view",
            "layers": [],
            "message": "Map bounding box (min/max lon/lat) is required.",
        }
    layers = dams_in_view_layer(
        min_lon=args.min_lon,
        min_lat=args.min_lat,
        max_lon=args.max_lon,
        max_lat=args.max_lat,
        lat=args.lat,
        lon=args.lon,
        limit=args.limit,
    )
    return {
        "ok": len(layers) > 0,
        "type": "dams_in_view",
        "layers": layers,
        "message": (
            layers[0]["name"] if layers else "No NID dams in this view."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI-Hydro map hydrography CLI")
    parser.add_argument("--json", action="store_true", help="Emit JSON on stdout")
    sub = parser.add_subparsers(dest="command", required=True)

    p_basin = sub.add_parser("merit-ensure-basin")
    p_basin.add_argument("--lat", type=float, required=True)
    p_basin.add_argument("--lon", type=float, required=True)
    p_basin.add_argument("--no-download", action="store_true")

    p_reg = sub.add_parser("merit-ensure-region")
    p_reg.add_argument("--preset", required=True)
    p_reg.add_argument("--lat", type=float, default=None)
    p_reg.add_argument("--lon", type=float, default=None)
    p_reg.add_argument("--no-download", action="store_true")

    p_layers = sub.add_parser("merit-layers")
    p_layers.add_argument("--lat", type=float, required=True)
    p_layers.add_argument("--lon", type=float, required=True)
    p_layers.add_argument("--min-lon", type=float, default=None)
    p_layers.add_argument("--min-lat", type=float, default=None)
    p_layers.add_argument("--max-lon", type=float, default=None)
    p_layers.add_argument("--max-lat", type=float, default=None)
    p_layers.add_argument("--no-level2", action="store_true")
    p_layers.add_argument("--no-rivers", action="store_true")
    p_layers.add_argument("--catchments", action="store_true")
    p_layers.add_argument("--pfaf", action="append", default=None)

    p_del = sub.add_parser("delineate-point")
    p_del.add_argument("--lat", type=float, required=True)
    p_del.add_argument("--lon", type=float, required=True)
    p_del.add_argument("--session-id", default="map")
    p_del.add_argument("--workspace-dir", default=None)
    p_del.add_argument("--expected-area-km2", type=float, default=None)
    p_del.add_argument("--method", default="auto")
    p_del.add_argument("--name", default=None)

    sub.add_parser("list-presets")

    p_wbd = sub.add_parser("wbd-layers")
    p_wbd.add_argument("--lat", type=float, required=True)
    p_wbd.add_argument("--lon", type=float, required=True)
    p_wbd.add_argument("--min-lon", type=float, default=None)
    p_wbd.add_argument("--min-lat", type=float, default=None)
    p_wbd.add_argument("--max-lon", type=float, default=None)
    p_wbd.add_argument("--max-lat", type=float, default=None)
    p_wbd.add_argument("--huc-level", type=int, default=8)

    p_huc = sub.add_parser("huc-at-point")
    p_huc.add_argument("--lat", type=float, required=True)
    p_huc.add_argument("--lon", type=float, required=True)
    p_huc.add_argument("--huc-level", type=int, default=8)

    p_search = sub.add_parser("search-hydrology")
    p_search.add_argument("--q", type=str, required=True)
    p_search.add_argument("--min-lon", type=float, default=None)
    p_search.add_argument("--min-lat", type=float, default=None)
    p_search.add_argument("--max-lon", type=float, default=None)
    p_search.add_argument("--max-lat", type=float, default=None)
    p_search.add_argument("--limit", type=int, default=20)

    p_gauges = sub.add_parser("gauges-in-view")
    p_gauges.add_argument("--lat", type=float, required=True)
    p_gauges.add_argument("--lon", type=float, required=True)
    p_gauges.add_argument("--min-lon", type=float, required=True)
    p_gauges.add_argument("--min-lat", type=float, required=True)
    p_gauges.add_argument("--max-lon", type=float, required=True)
    p_gauges.add_argument("--max-lat", type=float, required=True)
    p_gauges.add_argument("--limit", type=int, default=250)

    p_dams = sub.add_parser("dams-in-view")
    p_dams.add_argument("--lat", type=float, required=True)
    p_dams.add_argument("--lon", type=float, required=True)
    p_dams.add_argument("--min-lon", type=float, required=True)
    p_dams.add_argument("--min-lat", type=float, required=True)
    p_dams.add_argument("--max-lon", type=float, required=True)
    p_dams.add_argument("--max-lat", type=float, required=True)
    p_dams.add_argument("--limit", type=int, default=150)

    args = parser.parse_args(argv)
    handlers = {
        "merit-ensure-basin": cmd_merit_ensure_basin,
        "merit-ensure-region": cmd_merit_ensure_region,
        "merit-layers": cmd_merit_layers,
        "delineate-point": cmd_delineate_point,
        "list-presets": cmd_list_presets,
        "wbd-layers": cmd_wbd_layers,
        "huc-at-point": cmd_huc_at_point,
        "search-hydrology": cmd_search_hydrology,
        "gauges-in-view": cmd_gauges_in_view,
        "dams-in-view": cmd_dams_in_view,
    }
    try:
        out = handlers[args.command](args)
    except Exception as e:
        out = {"ok": False, "type": args.command, "message": str(e), "error": str(e)}

    if args.json:
        _emit(out)
    else:
        print(out)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
