"""
Named MERIT vector install regions (Pfafstetter level-2 basins).
"""

from __future__ import annotations

from typing import Any

# Approximate WGS84 bounds for preset labels (min_lon, min_lat, max_lon, max_lat)
REGION_BBOX: dict[str, tuple[float, float, float, float]] = {
    "conus": (-125.0, 24.0, -66.0, 50.0),
    "south_asia": (60.0, 5.0, 100.0, 40.0),
}

REGION_LABELS: dict[str, str] = {
    "conus": "CONUS (continental US)",
    "south_asia": "South Asia (India and vicinity)",
}


def list_region_presets() -> list[dict[str, Any]]:
    return [
        {
            "id": key,
            "label": REGION_LABELS.get(key, key),
            "bbox": list(REGION_BBOX[key]),
        }
        for key in REGION_BBOX
    ]


def pfaf_codes_for_preset(preset_id: str, *, merit_root=None) -> list[str]:
    """Resolve Pfaf codes intersecting a named region bbox via level-2 index."""
    preset = preset_id.strip().lower()
    if preset not in REGION_BBOX:
        raise ValueError(f"Unknown region preset: {preset_id!r}. Choose from: {list(REGION_BBOX)}")

    from shapely.geometry import box

    from ai_hydro.data.merit_manager import MeritDataManager

    mgr = MeritDataManager(root=merit_root) if merit_root else MeritDataManager()
    shp = mgr.level2_shapefile_path()
    if not shp.exists():
        raise FileNotFoundError(
            f"MERIT level-2 index missing at {shp}. Install level-2 first (merit_ensure_basin)."
        )

    import geopandas as gpd

    min_lon, min_lat, max_lon, max_lat = REGION_BBOX[preset]
    region = box(min_lon, min_lat, max_lon, max_lat)
    gdf = gpd.read_file(shp)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(4326)
    hit = gdf[gdf.geometry.intersects(region)]
    codes: list[str] = []
    for _, row in hit.iterrows():
        raw = (
            row.get("pfaf_code")
            or row.get("PFAF_ID")
            or row.get("BASIN")
            or row.get("basin")
            or ""
        )
        if raw is None or str(raw).strip() == "":
            continue
        try:
            codes.append(str(int(raw)).zfill(2))
        except (TypeError, ValueError):
            continue
    return sorted(set(codes))
