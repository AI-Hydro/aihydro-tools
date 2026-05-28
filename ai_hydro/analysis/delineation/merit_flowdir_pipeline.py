"""MERIT Hydro flow-direction delineation with GEE fetch + local pyflwdir routing."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import resource
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
from affine import Affine
from pyproj import Geod
from shapely.geometry import Point, box, shape

from ai_hydro.analysis.delineation.types import area_km2

log = logging.getLogger(__name__)

MERIT_GEE_DATASET = "MERIT/Hydro/v1_0_1"
MERIT_SCALE_M = 92.77
MERIT_LICENSE = "CC-BY-NC 4.0 or ODbL 1.0; derived-data obligations may apply."
MERIT_CITATION = (
    "Yamazaki D., Ikeshima D., Sosa J., Bates P.D., Allen G.H., Pavelsky T.M. "
    "MERIT Hydro: A high-resolution global hydrography map based on latest "
    "topography datasets. Water Resources Research, 55, 5053-5073, 2019. "
    "https://doi.org/10.1029/2019WR024873"
)
MERIT_BASINS_VECTOR_VERSION = "MERIT_Hydro_v07_Basins_v01"
MERIT_BASINS_VECTOR_SOURCE = "https://drive.google.com/drive/folders/1uCQFmdxFbjwoT9OYJxw-pXaP8q_GYH1a"
MERIT_FLOWDIR_RASTER_VERSION = "MERIT Hydro regional flowdir raster flowdir{pfaf}.tif"
MERIT_FLOWDIR_RASTER_SOURCE = "https://mghydro.com/watersheds/rasters/flow_dir_basins/flowdir{pfaf}.tif"

_TOOL_PATH = "ai_hydro.analysis.delineation.merit_flowdir_pipeline.delineate_merit_flowdir"
_CACHE_DIR = Path.home() / ".aihydro" / "cache" / "delineation" / "merit_gee"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_GEOD = Geod(ellps="WGS84")
_DEFAULT_BANDS = ("dir", "upa", "wth")
_SNAP_REFERENCE_BANDS = ("upa", "wth")
_EDGE_MARGIN_CELLS = 2
_AREA_MISMATCH_WARN_FRAC = 0.25
_MAX_LOCAL_FLOWDIR_CELLS = 250_000_000
_LOCAL_ROUTING_MEMORY_RISK_MB = 1_500.0
MERIT_SAFE_ENVELOPE_VERSION = "benchmark_2026-05-26_v1"
MERIT_INTERACTIVE_MAX_WINDOW_CELLS = 60_000_000
MERIT_INTERACTIVE_MAX_RSS_DELTA_MB = 600.0
MERIT_SCIENTIFIC_MAX_WINDOW_CELLS = 120_000_000
MERIT_SCIENTIFIC_MAX_RSS_DELTA_MB = 1_500.0

_CATCHMENT_ID_FIELDS = (
    "COMID",
    "comid",
    "HYBAS_ID",
    "hybas_id",
    "cat",
    "CAT_ID",
    "cat_id",
    "id",
)
_DOWNSTREAM_ID_FIELDS = (
    "NextDownID",
    "NEXT_DOWN",
    "next_down",
    "NextDown",
    "NEXT_SINK",
    "downstream",
    "toCOMID",
    "TOCOMID",
    "nextid",
)


@dataclass(frozen=True)
class MeritRasterBundle:
    bands: dict[str, np.ndarray]
    transform: Affine
    crs: str
    cache_key: str
    source: str
    bbox: tuple[float, float, float, float]
    resolution_m: float = MERIT_SCALE_M


@dataclass(frozen=True)
class MeritSnap:
    lat: float
    lon: float
    row: int
    col: int
    distance_m: float
    snapped_upa_km2: float | None
    snap_quality: str


@dataclass(frozen=True)
class MeritSnapReference:
    snap: MeritSnap
    source: str
    cache_key: str
    bbox: tuple[float, float, float, float]
    official_merit_upa_km2: float | None


@dataclass(frozen=True)
class MeritFlowdirResult:
    gdf: gpd.GeoDataFrame
    area_km2: float
    outlet_lat: float
    outlet_lon: float
    snap_distance_m: float
    snapped_upa_km2: float | None
    snap_quality: str
    snap_validation: dict[str, Any]
    cache_key: str
    bbox: tuple[float, float, float, float]
    scout_box_maxed: bool
    touches_edge: bool
    area_validation: dict[str, Any]
    quality_flags: list[str]
    source: str
    routing_dataset: str
    routing_resolution_m: float
    pfaf_region: str | None = None
    routing_data_source: str | None = None
    snap_source: str | None = None
    official_merit_upa_km2: float | None = None
    local_upstream_area_km2: float | None = None
    polygon_area_km2: float | None = None
    validation_sources: list[str] | None = None
    regional_flowdir_cached: bool | None = None
    execution_mode: str | None = None
    regional_flowdir_file_size_bytes: int | None = None
    window_expansion_iterations: int | None = None
    final_window_bounds: dict[str, float] | None = None
    final_window_cell_count: int | None = None
    basin_touched_window_boundary: bool | None = None
    window_complete: bool | None = None
    peak_memory_mb: float | None = None
    runtime_seconds: float | None = None
    fallback_history: list[dict[str, Any]] | None = None
    memory_telemetry: dict[str, float] | None = None
    safe_envelope_version: str | None = None
    terminal_catchment_id: str | int | None = None
    upstream_catchment_count: int | None = None
    terminal_refinement_used: bool | None = None
    vector_assembly_area_km2: float | None = None
    refined_polygon_area_km2: float | None = None
    vector_dataset_version: str | None = None
    raster_dataset_version: str | None = None
    vector_data_source: str | None = None
    raster_data_source: str | None = None


def _peak_memory_mb() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(ru / (1024 * 1024) if ru > 10_000_000 else ru / 1024)


def _rss_memory_mb() -> float:
    try:
        import psutil  # type: ignore

        return float(psutil.Process().memory_info().rss / (1024 * 1024))
    except Exception:
        return _peak_memory_mb()


def merit_dir_to_pyflwdir(merit_dir: np.ndarray) -> np.ndarray:
    """Convert MERIT Hydro local drainage direction to pyflwdir D8 codes."""
    arr = np.asarray(merit_dir)
    out = np.full(arr.shape, 247, dtype=np.uint8)
    valid = np.isin(arr, [0, 1, 2, 4, 8, 16, 32, 64, 128])
    out[valid] = arr[valid].astype(np.uint8)
    return out


def bbox_from_point(lat: float, lon: float, half_side_km: float) -> tuple[float, float, float, float]:
    """Build a WGS84 bbox roughly half_side_km around a point."""
    lat_delta = half_side_km / 111.32
    lon_delta = half_side_km / max(111.32 * math.cos(math.radians(lat)), 1e-6)
    return (
        max(-180.0, lon - lon_delta),
        max(-90.0, lat - lat_delta),
        min(180.0, lon + lon_delta),
        min(90.0, lat + lat_delta),
    )


def basin_touches_edge(mask: np.ndarray, margin_cells: int = _EDGE_MARGIN_CELLS) -> bool:
    """Return True if a basin mask reaches the raster edge margin."""
    if mask.size == 0 or not np.any(mask):
        return True
    m = max(1, int(margin_cells))
    return bool(
        mask[:m, :].any()
        or mask[-m:, :].any()
        or mask[:, :m].any()
        or mask[:, -m:].any()
    )


def validate_area(
    area: float,
    *,
    snapped_upa_km2: float | None,
    expected_area_km2: float | None,
    touches_edge: bool,
) -> tuple[dict[str, Any], list[str]]:
    """Compare polygon area to MERIT UPA and optional user-provided area."""
    validation: dict[str, Any] = {
        "area_km2": round(float(area), 3),
        "snapped_upa_km2": round(float(snapped_upa_km2), 3)
        if snapped_upa_km2 is not None and np.isfinite(snapped_upa_km2)
        else None,
        "expected_area_km2": expected_area_km2,
        "touches_edge": bool(touches_edge),
    }
    flags: list[str] = []
    if touches_edge:
        flags.append("BASIN_TOUCHES_WINDOW_EDGE")
    if area < 1.0:
        flags.append("VERY_SMALL_BASIN")
    if snapped_upa_km2 and snapped_upa_km2 > 0:
        rel = abs(area - snapped_upa_km2) / snapped_upa_km2
        validation["relative_error_vs_merit_upa"] = round(float(rel), 4)
        if rel > _AREA_MISMATCH_WARN_FRAC:
            flags.append("MERIT_UPA_AREA_MISMATCH")
    if expected_area_km2 and expected_area_km2 > 0:
        rel = abs(area - expected_area_km2) / expected_area_km2
        validation["relative_error_vs_expected"] = round(float(rel), 4)
        if rel > _AREA_MISMATCH_WARN_FRAC:
            flags.append("AREA_DIFFERS_FROM_EXPECTED")
    validation["ok"] = not flags
    return validation, flags


def validate_snap(
    snap: MeritSnap,
    *,
    expected_area_km2: float | None,
) -> tuple[dict[str, Any], list[str]]:
    validation: dict[str, Any] = {
        "snap_distance_m": round(float(snap.distance_m), 3),
        "snapped_upa_km2": round(float(snap.snapped_upa_km2), 3)
        if snap.snapped_upa_km2 is not None and np.isfinite(snap.snapped_upa_km2)
        else None,
        "expected_area_km2": expected_area_km2,
        "snap_quality": snap.snap_quality,
    }
    flags: list[str] = []
    if snap.distance_m > 5_000:
        flags.append("OUTLET_SNAP_FAR")
    if expected_area_km2 and expected_area_km2 > 0 and snap.snapped_upa_km2:
        rel = abs(snap.snapped_upa_km2 - expected_area_km2) / expected_area_km2
        validation["relative_error_vs_expected"] = round(float(rel), 4)
        if rel > _AREA_MISMATCH_WARN_FRAC:
            flags.append("SNAP_UPA_DIFFERS_FROM_EXPECTED")
    validation["ok"] = not flags
    return validation, flags


def classify_safe_envelope(
    *,
    final_window_cell_count: int | None,
    rss_delta_mb: float | None,
    window_complete: bool,
    scientific_mode: bool = False,
) -> tuple[str, list[str]]:
    """Classify adaptive-window routing against the current benchmark envelope."""
    cells = int(final_window_cell_count or 0)
    rss_delta = float(rss_delta_mb or 0.0)
    flags: list[str] = []
    if not window_complete:
        return "hybrid_required", ["HYBRID_ROUTING_REQUIRED", "HYBRID_ROUTING_RECOMMENDED"]
    if cells > MERIT_SCIENTIFIC_MAX_WINDOW_CELLS or rss_delta > MERIT_SCIENTIFIC_MAX_RSS_DELTA_MB:
        return "hybrid_required", [
            "LOCAL_ROUTING_MEMORY_RISK",
            "HYBRID_ROUTING_REQUIRED",
            "HYBRID_ROUTING_RECOMMENDED",
        ]
    if cells > MERIT_INTERACTIVE_MAX_WINDOW_CELLS or rss_delta > MERIT_INTERACTIVE_MAX_RSS_DELTA_MB:
        flags.append("HYBRID_ROUTING_RECOMMENDED")
        if not scientific_mode:
            flags.append("HYBRID_ROUTING_REQUIRED")
            return "hybrid_required", flags
        return "scientific_allowed", flags
    return "interactive_ok", flags


def _cell_centers(transform: Affine, shape_: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    rows, cols = np.indices(shape_)
    xs, ys = transform * (cols + 0.5, rows + 0.5)
    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)


def _distance_m(lon: float, lat: float, lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
    _, _, dist = _GEOD.inv(
        np.full(lons.shape, lon, dtype=float),
        np.full(lats.shape, lat, dtype=float),
        lons,
        lats,
    )
    return np.asarray(dist, dtype=float)


def snap_outlet_on_merit(
    lat: float,
    lon: float,
    *,
    upa: np.ndarray,
    transform: Affine,
    wth: np.ndarray | None = None,
    expected_area_km2: float | None = None,
    search_radius_m: float | None = None,
) -> MeritSnap:
    """Snap a pour point to a nearby MERIT drainage cell using UPA/width evidence."""
    upa_arr = np.asarray(upa, dtype=float)
    if upa_arr.ndim != 2:
        raise ValueError("MERIT UPA band must be a 2D array.")
    col_f, row_f = ~transform * (lon, lat)
    row0, col0 = int(round(row_f)), int(round(col_f))
    if not (0 <= row0 < upa_arr.shape[0] and 0 <= col0 < upa_arr.shape[1]):
        raise ValueError("Outlet is outside the fetched MERIT raster window.")

    if search_radius_m is None:
        if expected_area_km2 and expected_area_km2 > 0:
            search_radius_m = min(30_000.0, max(3_000.0, 1_500.0 * math.sqrt(expected_area_km2)))
        else:
            search_radius_m = 3_000.0

    lons, lats = _cell_centers(transform, upa_arr.shape)
    dist = _distance_m(lon, lat, lons, lats)
    finite_upa = np.isfinite(upa_arr) & (upa_arr > 0)
    if wth is not None:
        wth_arr = np.asarray(wth, dtype=float)
        stream = finite_upa & ((wth_arr > 0) | (upa_arr >= 1.0))
    else:
        stream = finite_upa & (upa_arr >= 1.0)
    candidates = stream & (dist <= search_radius_m)
    if not candidates.any():
        candidates = finite_upa & (dist <= search_radius_m)
    if not candidates.any():
        candidates = dist <= search_radius_m

    if expected_area_km2 and expected_area_km2 > 0:
        area_err = np.abs(upa_arr - expected_area_km2) / max(expected_area_km2, 1.0)
        score = area_err + 0.35 * (dist / max(search_radius_m, 1.0))
        quality_base = "area_targeted"
    else:
        upa_norm = np.log1p(np.where(np.isfinite(upa_arr), upa_arr, 0.0))
        max_upa = max(float(np.nanmax(upa_norm)), 1.0)
        score = (dist / max(search_radius_m, 1.0)) - 0.20 * (upa_norm / max_upa)
        quality_base = "nearest_merit_stream"
    score = np.where(candidates, score, np.inf)
    snap_r, snap_c = np.unravel_index(int(np.argmin(score)), score.shape)
    snap_lon, snap_lat = transform * (snap_c + 0.5, snap_r + 0.5)
    snap_dist = float(dist[snap_r, snap_c])
    snap_upa = float(upa_arr[snap_r, snap_c]) if np.isfinite(upa_arr[snap_r, snap_c]) else None
    quality = quality_base
    if snap_dist > 5_000:
        quality = f"{quality_base}_far"
    return MeritSnap(
        lat=float(snap_lat),
        lon=float(snap_lon),
        row=int(snap_r),
        col=int(snap_c),
        distance_m=snap_dist,
        snapped_upa_km2=snap_upa,
        snap_quality=quality,
    )


def _cache_key(
    *,
    bbox: tuple[float, float, float, float],
    bands: tuple[str, ...],
    scale_m: float,
    dataset: str,
) -> str:
    payload = json.dumps(
        {
            "bbox": [round(v, 7) for v in bbox],
            "bands": bands,
            "scale_m": scale_m,
            "dataset": dataset,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_cached_bundle(key: str) -> MeritRasterBundle | None:
    meta_path = _CACHE_DIR / f"{key}.json"
    if not meta_path.exists():
        return None
    try:
        import rasterio

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        bands: dict[str, np.ndarray] = {}
        for band, rel in meta["band_files"].items():
            with rasterio.open(_CACHE_DIR / rel) as src:
                bands[band] = src.read(1)
                transform = src.transform
                crs = src.crs.to_string() if src.crs else "EPSG:4326"
        return MeritRasterBundle(
            bands=bands,
            transform=transform,
            crs=crs,
            cache_key=key,
            source="gee_cache",
            bbox=tuple(meta["bbox"]),
            resolution_m=float(meta.get("resolution_m", MERIT_SCALE_M)),
        )
    except Exception as exc:
        log.debug("Could not load cached MERIT bundle %s: %s", key, exc)
        return None


def _write_cached_bundle(
    key: str,
    *,
    band_files: dict[str, Path],
    bbox: tuple[float, float, float, float],
    resolution_m: float,
) -> None:
    rels = {band: path.name for band, path in band_files.items()}
    (_CACHE_DIR / f"{key}.json").write_text(
        json.dumps({"band_files": rels, "bbox": bbox, "resolution_m": resolution_m}, indent=2),
        encoding="utf-8",
    )


def fetch_merit_gee_bbox(
    bbox: tuple[float, float, float, float],
    *,
    bands: tuple[str, ...] = _DEFAULT_BANDS,
    scale_m: float = MERIT_SCALE_M,
    project_id: str | None = None,
) -> MeritRasterBundle:
    """Fetch MERIT Hydro bands from Earth Engine as cached GeoTIFF rasters."""
    key = _cache_key(bbox=bbox, bands=bands, scale_m=scale_m, dataset=MERIT_GEE_DATASET)
    cached = _load_cached_bundle(key)
    if cached is not None:
        return cached

    try:
        import ee  # type: ignore
        import requests
        import rasterio
    except Exception as exc:
        raise RuntimeError(
            "MERIT GEE delineation requires earthengine-api, requests, and rasterio. "
            "Install with `pip install aihydro-tools[delineation,gee]`."
        ) from exc

    try:
        if project_id:
            ee.Initialize(project=project_id)
        else:
            ee.Initialize()
    except Exception as exc:
        raise RuntimeError(
            "Google Earth Engine is not initialized. Run `aihydro-gee status`, "
            "select a registered project, or call delineation_doctor()."
        ) from exc

    xmin, ymin, xmax, ymax = bbox
    region = ee.Geometry.Rectangle([xmin, ymin, xmax, ymax], proj="EPSG:4326", geodesic=False)
    image = ee.Image(MERIT_GEE_DATASET).select(list(bands)).clip(region)
    url = image.getDownloadURL(
        {
            "name": f"aihydro_merit_{key[:12]}",
            "scale": scale_m,
            "crs": "EPSG:4326",
            "region": region,
            "filePerBand": True,
            "format": "GEO_TIFF",
        }
    )
    response = requests.get(url, timeout=120)
    if not response.ok:
        detail = response.text[:500]
        raise RuntimeError(
            f"GEE MERIT download failed with HTTP {response.status_code}: {detail}. "
            "This usually means the synchronous raster window exceeded Earth Engine "
            "memory/size limits; use a smaller basin/window or an async export path."
        )

    band_files: dict[str, Path] = {}
    with tempfile.TemporaryDirectory(prefix="aihydro_merit_gee_") as tmp:
        zip_path = Path(tmp) / "merit.zip"
        zip_path.write_bytes(response.content)
        if zipfile.is_zipfile(zip_path):
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmp)
            for band in bands:
                matches = [
                    p
                    for p in Path(tmp).glob("*.tif")
                    if band in p.stem.split(".")[-1] or p.stem.endswith(band)
                ]
                if not matches:
                    matches = list(Path(tmp).glob(f"*{band}*.tif"))
                if not matches:
                    raise RuntimeError(f"GEE MERIT download did not include band {band}.")
                dest = _CACHE_DIR / f"{key}_{band}.tif"
                dest.write_bytes(matches[0].read_bytes())
                band_files[band] = dest
        else:
            tif_path = Path(tmp) / "merit.tif"
            tif_path.write_bytes(response.content)
            with rasterio.open(tif_path) as src:
                if src.count < len(bands):
                    raise RuntimeError(
                        f"GEE MERIT download returned {src.count} band(s), expected {len(bands)}."
                    )
                for idx, band in enumerate(bands, start=1):
                    dest = _CACHE_DIR / f"{key}_{band}.tif"
                    profile = src.profile.copy()
                    profile.update(count=1)
                    with rasterio.open(dest, "w", **profile) as dst:
                        dst.write(src.read(idx), 1)
                    band_files[band] = dest

    _write_cached_bundle(key, band_files=band_files, bbox=bbox, resolution_m=scale_m)
    with rasterio.open(band_files[bands[0]]) as src:
        transform = src.transform
        crs = src.crs.to_string() if src.crs else "EPSG:4326"
    loaded: dict[str, np.ndarray] = {}
    for band, path in band_files.items():
        with rasterio.open(path) as src:
            loaded[band] = src.read(1)
    return MeritRasterBundle(
        bands=loaded,
        transform=transform,
        crs=crs,
        cache_key=key,
        source="gee",
        bbox=bbox,
        resolution_m=scale_m,
    )


def merit_resolve_pfaf_region(lat: float, lon: float) -> str:
    """Resolve the MERIT Pfafstetter level-2 region for a point."""
    from ai_hydro.data.merit_manager import MeritDataManager

    return MeritDataManager().resolve_pfaf_region(lat, lon)


def merit_check_routing_region_cache(pfaf_region: str) -> dict[str, Any]:
    """Return flowdir-first regional cache status for a Pfaf region."""
    from dataclasses import asdict

    from ai_hydro.data.merit_manager import MeritDataManager

    return asdict(MeritDataManager().routing_region_cache(pfaf_region))


def merit_ensure_routing_region(
    lat: float | None = None,
    lon: float | None = None,
    *,
    pfaf_region: str | None = None,
    required_assets: tuple[str, ...] = ("flowdir",),
    acquisition_policy: str = "check_only",
) -> dict[str, Any]:
    """Ensure/report regional MERIT routing assets without requiring accumulation."""
    from dataclasses import asdict

    from ai_hydro.data.merit_manager import MeritDataManager

    status = MeritDataManager().ensure_routing_region(
        lat,
        lon,
        pfaf_region=pfaf_region,
        required_assets=required_assets,
        acquisition_policy=acquisition_policy,
    )
    return asdict(status)


def merit_check_basins_region_cache(pfaf_region: str) -> dict[str, Any]:
    """Return MERIT-Basins vector/topology cache status for a Pfaf region."""
    from dataclasses import asdict

    from ai_hydro.data.merit_manager import MeritDataManager

    return asdict(MeritDataManager().basins_region_cache(pfaf_region))


def merit_ensure_basins_region(
    pfaf_region: str,
    *,
    acquisition_policy: str = "check_only",
) -> dict[str, Any]:
    """Ensure/report regional MERIT-Basins vectors for hybrid routing."""
    from dataclasses import asdict

    from ai_hydro.data.merit_manager import MeritDataManager

    return asdict(
        MeritDataManager().ensure_basins_region(
            pfaf_region,
            acquisition_policy=acquisition_policy,
        )
    )


def _first_existing_column(gdf: gpd.GeoDataFrame, candidates: tuple[str, ...]) -> str | None:
    cols = set(gdf.columns)
    for col in candidates:
        if col in cols:
            return col
    lowered = {str(col).lower(): col for col in gdf.columns}
    for col in candidates:
        hit = lowered.get(col.lower())
        if hit is not None:
            return str(hit)
    return None


def merit_load_catchment_topology(pfaf_region: str) -> gpd.GeoDataFrame:
    """Load staged MERIT-Basins catchments with normalized topology columns."""
    from ai_hydro.data.merit_manager import MeritDataManager

    mgr = MeritDataManager()
    path = mgr.catchment_shapefile_path(pfaf_region)
    if not path.exists():
        raise FileNotFoundError(
            f"MERIT-Basins catchment vectors are not staged for Pfaf {pfaf_region}."
        )
    gdf = gpd.read_file(path)
    if gdf.empty:
        raise ValueError(f"MERIT-Basins catchment file is empty: {path}")
    id_col = _first_existing_column(gdf, _CATCHMENT_ID_FIELDS)
    if id_col is None:
        raise ValueError(
            f"Could not identify catchment id field in {path}. "
            f"Tried: {', '.join(_CATCHMENT_ID_FIELDS)}"
        )
    downstream_col = _first_existing_column(gdf, _DOWNSTREAM_ID_FIELDS)
    gdf = gdf.copy()
    gdf["_aihydro_catchment_id"] = gdf[id_col].astype(str)
    river_path = mgr.river_shapefile_path(pfaf_region)
    if river_path.exists():
        rivers = gpd.read_file(river_path, ignore_geometry=True)
        river_id_col = _first_existing_column(rivers, _CATCHMENT_ID_FIELDS)
        if river_id_col is not None:
            rivers = rivers.copy()
            rivers["_aihydro_catchment_id"] = rivers[river_id_col].astype(str)
            keep_cols = [
                col
                for col in (
                    "_aihydro_catchment_id",
                    "NextDownID",
                    "next_down",
                    "NEXT_DOWN",
                    "uparea",
                    "maxup",
                    "up1",
                    "up2",
                    "up3",
                    "up4",
                )
                if col in rivers.columns
            ]
            if len(keep_cols) > 1:
                gdf = gdf.merge(
                    rivers[keep_cols].drop_duplicates("_aihydro_catchment_id"),
                    on="_aihydro_catchment_id",
                    how="left",
                    suffixes=("", "_river"),
                )
                downstream_col = _first_existing_column(gdf, _DOWNSTREAM_ID_FIELDS)
    if downstream_col is None:
        gdf["_aihydro_downstream_id"] = ""
    else:
        gdf["_aihydro_downstream_id"] = gdf[downstream_col].fillna("").astype(str)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    return gdf.to_crs(4326)


def merit_identify_terminal_catchment(
    snapped_outlet: MeritSnap | tuple[float, float] | dict[str, float],
    catchments: gpd.GeoDataFrame | None = None,
    *,
    pfaf_region: str | None = None,
) -> str:
    """Return the MERIT-Basins catchment id containing the snapped outlet."""
    if catchments is None:
        if pfaf_region is None:
            raise ValueError("catchments or pfaf_region is required.")
        catchments = merit_load_catchment_topology(pfaf_region)
    if isinstance(snapped_outlet, MeritSnap):
        lon, lat = snapped_outlet.lon, snapped_outlet.lat
    elif isinstance(snapped_outlet, dict):
        lon = float(snapped_outlet["lon"])
        lat = float(snapped_outlet["lat"])
    else:
        lon, lat = float(snapped_outlet[0]), float(snapped_outlet[1])
    pt = gpd.GeoSeries([Point(lon, lat)], crs=4326)
    pt = pt.to_crs(catchments.crs)
    hit = catchments[catchments.geometry.contains(pt.iloc[0])]
    if hit.empty:
        hit = catchments[catchments.geometry.intersects(pt.iloc[0])]
    if hit.empty:
        distances = catchments.geometry.distance(pt.iloc[0])
        hit = catchments.loc[[distances.idxmin()]]
    return str(hit.iloc[0]["_aihydro_catchment_id"])


def merit_traverse_upstream_catchments(
    terminal_catchment_id: str | int,
    catchments: gpd.GeoDataFrame,
) -> list[str]:
    """Traverse normalized MERIT-Basins catchment topology upstream."""
    terminal = str(terminal_catchment_id)
    upstream_by_downstream: dict[str, list[str]] = {}
    for _, row in catchments.iterrows():
        cid = str(row["_aihydro_catchment_id"])
        downstream = str(row.get("_aihydro_downstream_id", "") or "")
        if downstream and downstream.lower() not in ("0", "nan", "none", "-1"):
            upstream_by_downstream.setdefault(downstream, []).append(cid)

    seen: set[str] = set()
    stack = [terminal]
    ordered: list[str] = []
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        ordered.append(cid)
        stack.extend(upstream_by_downstream.get(cid, []))
    return ordered


def _refine_terminal_catchment_with_flowdir(
    terminal: gpd.GeoDataFrame,
    snap_reference: MeritSnapReference,
    *,
    pfaf_region: str,
    margin_cells: int = 32,
) -> gpd.GeoDataFrame:
    """Raster-refine only the MERIT-Basins terminal catchment around the outlet."""
    import pyflwdir
    import rasterio
    from rasterio.windows import from_bounds

    from ai_hydro.data.merit_manager import MeritDataManager

    mgr = MeritDataManager()
    flowdir = mgr.flowdir_path(pfaf_region)
    if not flowdir:
        raise FileNotFoundError(f"Local MERIT flow direction raster missing for basin {pfaf_region}.")
    snap = snap_reference.snap
    with rasterio.open(flowdir) as src:
        crs = src.crs.to_string() if src.crs else "EPSG:4326"
        terminal_src = terminal.to_crs(crs)
        xmin, ymin, xmax, ymax = terminal_src.total_bounds
        res_x = abs(float(src.transform.a))
        res_y = abs(float(src.transform.e))
        xmin -= margin_cells * res_x
        xmax += margin_cells * res_x
        ymin -= margin_cells * res_y
        ymax += margin_cells * res_y
        left = max(src.bounds.left, xmin)
        right = min(src.bounds.right, xmax)
        bottom = max(src.bounds.bottom, ymin)
        top = min(src.bounds.top, ymax)
        if left >= right or bottom >= top:
            raise ValueError("Terminal catchment is outside the cached flowdir raster.")
        window = from_bounds(left, bottom, right, top, transform=src.transform).round_offsets().round_lengths()
        if int(window.width * window.height) > MERIT_INTERACTIVE_MAX_WINDOW_CELLS:
            raise RuntimeError(
                "TERMINAL_CATCHMENT_REFINEMENT_FAILED: terminal raster window exceeds "
                f"interactive limit ({int(window.width * window.height)} cells)."
            )
        dir_arr = src.read(1, window=window)
        transform = src.window_transform(window)
    d8 = merit_dir_to_pyflwdir(dir_arr)
    flw = pyflwdir.from_array(
        d8,
        ftype="d8",
        mask=np.isin(d8, [0, 1, 2, 4, 8, 16, 32, 64, 128]),
        transform=transform,
        latlon=crs.upper() in ("EPSG:4326", "OGC:CRS84"),
    )
    basin_ids = flw.basins(xy=([snap.lon], [snap.lat]), ids=np.array([1], dtype=np.uint32))
    refined = _polygonize_mask(basin_ids == 1, transform, crs)
    if refined.empty:
        raise RuntimeError("Terminal raster refinement produced an empty mask.")
    clipped = gpd.overlay(refined.to_crs(terminal.crs), terminal[["geometry"]], how="intersection")
    if clipped.empty:
        raise RuntimeError("Terminal raster refinement did not intersect terminal catchment.")
    return clipped[["geometry"]]


def merit_get_snap_reference(
    lat: float,
    lon: float,
    *,
    expected_area_km2: float | None = None,
    project_id: str | None = None,
    half_side_km: float = 8.0,
) -> MeritSnapReference:
    """Fetch a tiny GEE MERIT upa/wth window and snap to an official drainage cell."""
    if expected_area_km2 and expected_area_km2 > 0:
        snap_radius_km = min(30.0, max(3.0, 1.5 * math.sqrt(expected_area_km2)))
        half_side_km = max(half_side_km, snap_radius_km * 1.2)
    bbox = bbox_from_point(lat, lon, half_side_km)
    bundle = fetch_merit_gee_bbox(
        bbox,
        bands=_SNAP_REFERENCE_BANDS,
        project_id=project_id,
    )
    snap = snap_outlet_on_merit(
        lat,
        lon,
        upa=np.asarray(bundle.bands["upa"], dtype=float),
        wth=bundle.bands.get("wth"),
        transform=bundle.transform,
        expected_area_km2=expected_area_km2,
    )
    return MeritSnapReference(
        snap=snap,
        source=bundle.source,
        cache_key=bundle.cache_key,
        bbox=bundle.bbox,
        official_merit_upa_km2=snap.snapped_upa_km2,
    )


def _polygonize_mask(mask: np.ndarray, transform: Affine, crs: str) -> gpd.GeoDataFrame:
    import rasterio.features

    geoms = [
        shape(geom)
        for geom, value in rasterio.features.shapes(
            mask.astype(np.uint8), mask=mask.astype(bool), transform=transform
        )
        if int(value) == 1
    ]
    if not geoms:
        return gpd.GeoDataFrame({"catchment": []}, geometry=[], crs=crs)
    gdf = gpd.GeoDataFrame({"catchment": [1] * len(geoms)}, geometry=geoms, crs=crs)
    return gdf.dissolve(by="catchment").reset_index(drop=True).to_crs(4326)


def delineate_merit_bundle(
    bundle: MeritRasterBundle,
    lat: float,
    lon: float,
    *,
    expected_area_km2: float | None = None,
) -> MeritFlowdirResult:
    """Delineate a basin from an already-fetched MERIT raster bundle."""
    try:
        import pyflwdir
    except Exception as exc:
        raise RuntimeError(
            "pyflwdir is required for MERIT flow-direction delineation. "
            "Install with `pip install aihydro-tools[delineation]`."
        ) from exc

    d8 = merit_dir_to_pyflwdir(bundle.bands["dir"])
    upa = np.asarray(bundle.bands["upa"], dtype=float)
    wth = bundle.bands.get("wth")
    snap = snap_outlet_on_merit(
        lat,
        lon,
        upa=upa,
        wth=wth,
        transform=bundle.transform,
        expected_area_km2=expected_area_km2,
    )
    mask = np.isin(d8, [0, 1, 2, 4, 8, 16, 32, 64, 128])
    flw = pyflwdir.from_array(
        d8,
        ftype="d8",
        mask=mask,
        transform=bundle.transform,
        latlon=bundle.crs.upper() in ("EPSG:4326", "OGC:CRS84"),
    )
    basin_ids = flw.basins(xy=([snap.lon], [snap.lat]), ids=np.array([1], dtype=np.uint32))
    basin_mask = basin_ids == 1
    touches = basin_touches_edge(basin_mask)
    gdf = _polygonize_mask(basin_mask, bundle.transform, bundle.crs)
    area = area_km2(gdf)
    validation, flags = validate_area(
        area,
        snapped_upa_km2=snap.snapped_upa_km2,
        expected_area_km2=expected_area_km2,
        touches_edge=touches,
    )
    snap_validation, snap_flags = validate_snap(snap, expected_area_km2=expected_area_km2)
    flags.extend(flag for flag in snap_flags if flag not in flags)
    return MeritFlowdirResult(
        gdf=gdf,
        area_km2=area,
        outlet_lat=snap.lat,
        outlet_lon=snap.lon,
        snap_distance_m=snap.distance_m,
        snapped_upa_km2=snap.snapped_upa_km2,
        snap_quality=snap.snap_quality,
        snap_validation=snap_validation,
        cache_key=bundle.cache_key,
        bbox=bundle.bbox,
        scout_box_maxed=False,
        touches_edge=touches,
        area_validation=validation,
        quality_flags=flags,
        source=bundle.source,
        routing_dataset=MERIT_GEE_DATASET,
        routing_resolution_m=bundle.resolution_m,
        routing_data_source=bundle.source,
        snap_source=bundle.source,
        official_merit_upa_km2=snap.snapped_upa_km2,
        polygon_area_km2=area,
        validation_sources=["gee_official_upa"],
        regional_flowdir_cached=False,
    )


def delineate_merit_gee(
    lat: float,
    lon: float,
    *,
    expected_area_km2: float | None = None,
    project_id: str | None = None,
    initial_half_side_km: float = 20.0,
    max_half_side_km: float = 1200.0,
) -> MeritFlowdirResult:
    """Fetch MERIT Hydro from GEE adaptively and delineate with pyflwdir."""
    half_side = initial_half_side_km
    last: MeritFlowdirResult | None = None
    while half_side <= max_half_side_km:
        bbox = bbox_from_point(lat, lon, half_side)
        bundle = fetch_merit_gee_bbox(bbox, project_id=project_id)
        result = delineate_merit_bundle(bundle, lat, lon, expected_area_km2=expected_area_km2)
        last = result
        if not result.touches_edge:
            return result
        half_side *= 1.6
    if last is None:
        raise RuntimeError("MERIT GEE delineation did not produce a basin.")
    flags = list(last.quality_flags)
    if "SCOUT_WINDOW_MAXED" not in flags:
        flags.append("SCOUT_WINDOW_MAXED")
    return MeritFlowdirResult(
        **{**last.__dict__, "scout_box_maxed": True, "quality_flags": flags}
    )


def local_merit_flowdir_pyflwdir(
    lat: float,
    lon: float,
    *,
    expected_area_km2: float | None = None,
    snap_reference: MeritSnapReference | None = None,
    allow_offline: bool = False,
    compute_local_upstream_area: bool = False,
    initial_half_side_km: float | None = None,
    max_half_side_km: float = 1200.0,
    force_offline: bool = False,
    scientific_mode: bool = False,
) -> MeritFlowdirResult:
    """Delineate from a cached regional MERIT flowdir raster only."""
    started = time.perf_counter()
    rss_before = _rss_memory_mb()
    rss_after_window_read = rss_before
    rss_after_pyflwdir_build = rss_before
    rss_after_basin_mask = rss_before
    rss_after_vectorization = rss_before
    import rasterio
    from rasterio.windows import from_bounds

    from ai_hydro.data.merit_manager import MeritDataManager

    mgr = MeritDataManager()
    status = mgr.ensure_basin(lat, lon, download=False)
    pfaf = status.pfaf_code
    flowdir_path = mgr.flowdir_path(pfaf)
    if not flowdir_path:
        raise FileNotFoundError(f"Local MERIT flow direction raster missing for basin {pfaf}.")
    flowdir_size = flowdir_path.stat().st_size if flowdir_path.exists() else None

    snap_flags: list[str] = []
    if snap_reference is None:
        try:
            if force_offline:
                raise RuntimeError("forced offline local routing")
            snap_reference = merit_get_snap_reference(
                lat,
                lon,
                expected_area_km2=expected_area_km2,
            )
        except Exception:
            if not allow_offline:
                raise
            snap_reference = MeritSnapReference(
                snap=MeritSnap(
                    lat=lat,
                    lon=lon,
                    row=-1,
                    col=-1,
                    distance_m=0.0,
                    snapped_upa_km2=None,
                    snap_quality="offline_unsnapped",
                ),
                source="offline",
                cache_key="offline",
                bbox=(lon, lat, lon, lat),
                official_merit_upa_km2=None,
            )
            snap_flags.append("GEE_VALIDATION_UNAVAILABLE")

    try:
        import pyflwdir
    except Exception as exc:
        raise RuntimeError(
            "pyflwdir is required for local MERIT flow-direction delineation. "
            "Install with `pip install aihydro-tools[delineation]`."
        ) from exc

    snap = snap_reference.snap
    if initial_half_side_km is None:
        area_hint = snap_reference.official_merit_upa_km2 or expected_area_km2 or 100.0
        initial_half_side_km = min(120.0, max(20.0, 1.2 * math.sqrt(max(area_hint, 1.0))))

    local_upstream_area_km2: float | None = None
    validation_sources: list[str] = []
    if snap_reference.official_merit_upa_km2 is not None:
        validation_sources.append("gee_official_upa")

    half_side = initial_half_side_km
    last_error: str | None = None
    expansion_iterations = 0
    final_cell_count = 0
    with rasterio.open(flowdir_path) as src:
        crs = src.crs.to_string() if src.crs else "EPSG:4326"
        src_bounds = src.bounds
        result_bbox = (src_bounds.left, src_bounds.bottom, src_bounds.right, src_bounds.top)
        while half_side <= max_half_side_km:
            bbox = bbox_from_point(snap.lat, snap.lon, half_side)
            left = max(src_bounds.left, bbox[0])
            bottom = max(src_bounds.bottom, bbox[1])
            right = min(src_bounds.right, bbox[2])
            top = min(src_bounds.top, bbox[3])
            if left >= right or bottom >= top:
                raise ValueError("Snapped outlet is outside the cached regional flowdir bounds.")
            window = from_bounds(left, bottom, right, top, transform=src.transform).round_offsets().round_lengths()
            result_bbox = (left, bottom, right, top)
            final_cell_count = int(window.width * window.height)
            if (
                final_cell_count > MERIT_INTERACTIVE_MAX_WINDOW_CELLS
                and not scientific_mode
            ):
                raise RuntimeError(
                    "HYBRID_ROUTING_REQUIRED HYBRID_ROUTING_RECOMMENDED "
                    f"safe_envelope_version={MERIT_SAFE_ENVELOPE_VERSION}: requested local "
                    f"flowdir window has {final_cell_count} cells, exceeding the "
                    f"interactive limit {MERIT_INTERACTIVE_MAX_WINDOW_CELLS}."
                )
            if final_cell_count > MERIT_SCIENTIFIC_MAX_WINDOW_CELLS:
                raise RuntimeError(
                    "HYBRID_ROUTING_REQUIRED LOCAL_MEMORY_THRESHOLD_EXCEEDED "
                    f"safe_envelope_version={MERIT_SAFE_ENVELOPE_VERSION}: requested local "
                    f"flowdir window has {final_cell_count} cells, exceeding the "
                    f"scientific limit {MERIT_SCIENTIFIC_MAX_WINDOW_CELLS}."
                )
            if final_cell_count > _MAX_LOCAL_FLOWDIR_CELLS:
                raise RuntimeError(
                    "LOCAL_MEMORY_THRESHOLD_EXCEEDED ADAPTIVE_WINDOW_LIMIT_REACHED "
                    "HYBRID_ROUTING_RECOMMENDED: requested local flowdir window exceeds "
                    "the current raster-only processing envelope."
                )
            dir_arr = src.read(1, window=window)
            rss_after_window_read = _rss_memory_mb()
            transform = src.window_transform(window)
            d8 = merit_dir_to_pyflwdir(dir_arr)
            mask = np.isin(d8, [0, 1, 2, 4, 8, 16, 32, 64, 128])
            try:
                flw = pyflwdir.from_array(
                    d8,
                    ftype="d8",
                    mask=mask,
                    transform=transform,
                    latlon=crs.upper() in ("EPSG:4326", "OGC:CRS84"),
                )
                rss_after_pyflwdir_build = _rss_memory_mb()
                basin_ids = flw.basins(
                    xy=([snap.lon], [snap.lat]), ids=np.array([1], dtype=np.uint32)
                )
                rss_after_basin_mask = _rss_memory_mb()
            except Exception as exc:
                last_error = str(exc)
                half_side *= 1.6
                continue
            basin_mask = basin_ids == 1
            touches = basin_touches_edge(basin_mask)
            gdf = _polygonize_mask(basin_mask, transform, crs)
            rss_after_vectorization = _rss_memory_mb()
            area = area_km2(gdf)
            if compute_local_upstream_area:
                col_f, row_f = ~transform * (snap.lon, snap.lat)
                row, col = int(round(row_f)), int(round(col_f))
                uparea = flw.upstream_area(unit="km2")
                if 0 <= row < uparea.shape[0] and 0 <= col < uparea.shape[1]:
                    local_upstream_area_km2 = float(uparea[row, col])
                    if "local_pyflwdir_upstream_area" not in validation_sources:
                        validation_sources.append("local_pyflwdir_upstream_area")
            if not touches:
                break
            half_side *= 1.6
            expansion_iterations += 1
        else:
            raise RuntimeError(
                "HYBRID_ROUTING_REQUIRED LARGE_BASIN_HYBRID_REQUIRED ADAPTIVE_WINDOW_LIMIT_REACHED "
                "HYBRID_ROUTING_RECOMMENDED: local flowdir window reached maximum "
                f"extent without containing the full basin. Last error: {last_error or 'basin touches edge'}"
            )

    area_validation, flags = validate_area(
        area,
        snapped_upa_km2=snap_reference.official_merit_upa_km2,
        expected_area_km2=expected_area_km2,
        touches_edge=touches,
    )
    snap_validation, more_snap_flags = validate_snap(snap, expected_area_km2=expected_area_km2)
    flags.extend(flag for flag in more_snap_flags if flag not in flags)
    flags.extend(flag for flag in snap_flags if flag not in flags)
    if expansion_iterations > 0 and "ADAPTIVE_WINDOW_EXPANDED" not in flags:
        flags.append("ADAPTIVE_WINDOW_EXPANDED")
    if touches and "BASIN_TOUCHES_ROUTING_WINDOW_BOUNDARY" not in flags:
        flags.append("BASIN_TOUCHES_ROUTING_WINDOW_BOUNDARY")
    peak_memory = _peak_memory_mb()
    rss_peak = max(
        rss_before,
        rss_after_window_read,
        rss_after_pyflwdir_build,
        rss_after_basin_mask,
        rss_after_vectorization,
    )
    rss_delta = max(0.0, rss_peak - rss_before)
    if (
        rss_delta > _LOCAL_ROUTING_MEMORY_RISK_MB
        or final_cell_count > int(_MAX_LOCAL_FLOWDIR_CELLS * 0.75)
    ) and "LOCAL_ROUTING_MEMORY_RISK" not in flags:
        flags.append("LOCAL_ROUTING_MEMORY_RISK")
    window_complete = not touches
    envelope_status, envelope_flags = classify_safe_envelope(
        final_window_cell_count=final_cell_count,
        rss_delta_mb=rss_delta,
        window_complete=window_complete,
        scientific_mode=scientific_mode,
    )
    for flag in envelope_flags:
        if flag not in flags:
            flags.append(flag)
    if envelope_status == "hybrid_required":
        raise RuntimeError(
            "HYBRID_ROUTING_REQUIRED HYBRID_ROUTING_RECOMMENDED "
            f"safe_envelope_version={MERIT_SAFE_ENVELOPE_VERSION}: adaptive local routing "
            f"outside safe envelope (cells={final_cell_count}, rss_delta_mb={rss_delta:.3f}, "
            f"window_complete={window_complete})."
        )
    key = hashlib.sha256(
        f"{flowdir_path}:{snap_reference.cache_key}:{compute_local_upstream_area}".encode("utf-8")
    ).hexdigest()
    return MeritFlowdirResult(
        gdf=gdf,
        area_km2=area,
        outlet_lat=snap.lat,
        outlet_lon=snap.lon,
        snap_distance_m=snap.distance_m,
        snapped_upa_km2=snap_reference.official_merit_upa_km2,
        snap_quality=snap.snap_quality,
        snap_validation=snap_validation,
        cache_key=key,
        bbox=result_bbox,
        scout_box_maxed=False,
        touches_edge=touches,
        area_validation=area_validation,
        quality_flags=flags,
        source="local_merit_flowdir",
        routing_dataset=MERIT_GEE_DATASET,
        routing_resolution_m=MERIT_SCALE_M,
        pfaf_region=pfaf,
        routing_data_source="local_flowdir",
        raster_dataset_version=MERIT_FLOWDIR_RASTER_VERSION.format(pfaf=pfaf),
        raster_data_source=MERIT_FLOWDIR_RASTER_SOURCE.format(pfaf=pfaf),
        snap_source=snap_reference.source,
        official_merit_upa_km2=snap_reference.official_merit_upa_km2,
        local_upstream_area_km2=local_upstream_area_km2,
        polygon_area_km2=area,
        validation_sources=validation_sources,
        regional_flowdir_cached=True,
        execution_mode="local_staged_flowdir_adaptive_window",
        regional_flowdir_file_size_bytes=flowdir_size,
        window_expansion_iterations=expansion_iterations,
        final_window_bounds={
            "xmin": float(result_bbox[0]),
            "ymin": float(result_bbox[1]),
            "xmax": float(result_bbox[2]),
            "ymax": float(result_bbox[3]),
        },
        final_window_cell_count=final_cell_count,
        basin_touched_window_boundary=touches,
        window_complete=window_complete,
        peak_memory_mb=round(peak_memory, 3),
        runtime_seconds=round(time.perf_counter() - started, 3),
        memory_telemetry={
            "rss_before_mb": round(rss_before, 3),
            "rss_after_window_read_mb": round(rss_after_window_read, 3),
            "rss_after_pyflwdir_build_mb": round(rss_after_pyflwdir_build, 3),
            "rss_after_basin_mask_mb": round(rss_after_basin_mask, 3),
            "rss_after_vectorization_mb": round(rss_after_vectorization, 3),
            "rss_peak_mb": round(rss_peak, 3),
            "rss_delta_mb": round(rss_delta, 3),
        },
        safe_envelope_version=MERIT_SAFE_ENVELOPE_VERSION,
    )


def delineate_local_merit(
    lat: float,
    lon: float,
    *,
    expected_area_km2: float | None = None,
) -> MeritFlowdirResult:
    """Backward-compatible local MERIT entrypoint using flowdir-only routing."""
    return local_merit_flowdir_pyflwdir(
        lat,
        lon,
        expected_area_km2=expected_area_km2,
    )


def merit_basins_hybrid_delineate(
    lat: float,
    lon: float,
    *,
    expected_area_km2: float | None = None,
    snap_reference: MeritSnapReference | None = None,
    allow_offline: bool = False,
    force_offline: bool = False,
) -> MeritFlowdirResult:
    """
    Delineate large basins with MERIT-Basins topology plus terminal raster refinement.

    The vector topology provides the large upstream assembly. The staged local
    MERIT flowdir is used only to refine the terminal catchment portion at the
    requested snapped outlet when feasible.
    """
    import pandas as pd

    started = time.perf_counter()
    rss_before = _rss_memory_mb()
    rss_after_topology_load = rss_before
    rss_after_vector_assembly = rss_before
    rss_after_terminal_refinement = rss_before
    rss_after_merge = rss_before
    fallback_history: list[dict[str, Any]] = []
    quality_flags: list[str] = ["HYBRID_ROUTING_USED"]
    snap_flags: list[str] = []
    validation_sources: list[str] = []

    if snap_reference is None:
        try:
            if force_offline:
                raise RuntimeError("forced offline hybrid routing")
            snap_reference = merit_get_snap_reference(
                lat,
                lon,
                expected_area_km2=expected_area_km2,
            )
        except Exception:
            if not allow_offline:
                raise
            snap_reference = MeritSnapReference(
                snap=MeritSnap(
                    lat=lat,
                    lon=lon,
                    row=-1,
                    col=-1,
                    distance_m=0.0,
                    snapped_upa_km2=None,
                    snap_quality="offline_unsnapped",
                ),
                source="offline",
                cache_key="offline",
                bbox=(lon, lat, lon, lat),
                official_merit_upa_km2=None,
            )
            snap_flags.append("GEE_VALIDATION_UNAVAILABLE")

    if snap_reference.official_merit_upa_km2 is not None:
        validation_sources.append("gee_official_upa")

    snap = snap_reference.snap
    pfaf = merit_resolve_pfaf_region(snap.lat, snap.lon)
    basins_status = merit_check_basins_region_cache(pfaf)
    if not basins_status.get("catchments_ready"):
        raise RuntimeError(
            "MERIT_BASINS_NOT_STAGED HYBRID_ROUTING_REQUIRED: MERIT-Basins "
            f"catchments are not cached for Pfaf {pfaf}."
        )

    catchments = merit_load_catchment_topology(pfaf)
    rss_after_topology_load = _rss_memory_mb()
    terminal_id = merit_identify_terminal_catchment(snap, catchments)
    upstream_ids = merit_traverse_upstream_catchments(terminal_id, catchments)
    upstream = catchments[catchments["_aihydro_catchment_id"].isin(upstream_ids)].copy()
    if upstream.empty:
        raise RuntimeError(f"Hybrid topology returned no upstream catchments for {terminal_id}.")

    terminal = upstream[upstream["_aihydro_catchment_id"] == str(terminal_id)].copy()
    nonterminal = upstream[upstream["_aihydro_catchment_id"] != str(terminal_id)].copy()
    vector_gdf = upstream[["_aihydro_catchment_id", "geometry"]].dissolve().reset_index(drop=True)
    vector_gdf = vector_gdf.set_crs(catchments.crs).to_crs(4326)
    vector_area = area_km2(vector_gdf)
    rss_after_vector_assembly = _rss_memory_mb()

    terminal_refinement_used = False
    terminal_component = terminal[["geometry"]].copy()
    refinement_area: float | None = None
    try:
        terminal_component = _refine_terminal_catchment_with_flowdir(
            terminal[["geometry"]].to_crs(catchments.crs),
            snap_reference=snap_reference,
            pfaf_region=pfaf,
        )
        terminal_refinement_used = True
        refinement_area = area_km2(terminal_component.to_crs(4326))
        rss_after_terminal_refinement = _rss_memory_mb()
        fallback_history.append(
            {"method": "terminal_local_merit_flowdir_refinement", "outcome": "succeeded"}
        )
    except Exception as exc:
        if "TERMINAL_CATCHMENT_REFINEMENT_FAILED" not in quality_flags:
            quality_flags.append("TERMINAL_CATCHMENT_REFINEMENT_FAILED")
        fallback_history.append(
            {
                "method": "terminal_local_merit_flowdir_refinement",
                "outcome": "failed",
                "reason": str(exc),
            }
        )

    pieces = []
    if not nonterminal.empty:
        pieces.append(nonterminal[["geometry"]].to_crs(catchments.crs))
    pieces.append(terminal_component.to_crs(catchments.crs))
    merged = gpd.GeoDataFrame(
        geometry=gpd.GeoSeries(
            [gpd.GeoDataFrame(pd.concat(pieces, ignore_index=True), crs=catchments.crs).union_all()],
            crs=catchments.crs,
        )
    ).to_crs(4326)
    rss_after_merge = _rss_memory_mb()
    polygon_area = area_km2(merged)
    rss_peak = max(
        rss_before,
        rss_after_topology_load,
        rss_after_vector_assembly,
        rss_after_terminal_refinement,
        rss_after_merge,
    )

    area_validation, area_flags = validate_area(
        polygon_area,
        snapped_upa_km2=snap_reference.official_merit_upa_km2,
        expected_area_km2=expected_area_km2,
        touches_edge=False,
    )
    snap_validation, more_snap_flags = validate_snap(snap, expected_area_km2=expected_area_km2)
    for flag in area_flags + more_snap_flags + snap_flags:
        if flag not in quality_flags:
            quality_flags.append(flag)
    if "MERIT_UPA_AREA_MISMATCH" in quality_flags and "HYBRID_AREA_VALIDATION_MISMATCH" not in quality_flags:
        quality_flags.append("HYBRID_AREA_VALIDATION_MISMATCH")

    cache_key = hashlib.sha256(
        f"hybrid:{pfaf}:{terminal_id}:{snap_reference.cache_key}:{terminal_refinement_used}".encode(
            "utf-8"
        )
    ).hexdigest()
    return MeritFlowdirResult(
        gdf=merged,
        area_km2=polygon_area,
        outlet_lat=snap.lat,
        outlet_lon=snap.lon,
        snap_distance_m=snap.distance_m,
        snapped_upa_km2=snap_reference.official_merit_upa_km2,
        snap_quality=snap.snap_quality,
        snap_validation=snap_validation,
        cache_key=cache_key,
        bbox=tuple(merged.total_bounds),
        scout_box_maxed=False,
        touches_edge=False,
        area_validation=area_validation,
        quality_flags=quality_flags,
        source="merit_basins_hybrid",
        routing_dataset="MERIT-Basins + MERIT Hydro flowdir",
        routing_resolution_m=MERIT_SCALE_M,
        pfaf_region=pfaf,
        routing_data_source="local_merit_basins_vectors_and_flowdir",
        vector_dataset_version=MERIT_BASINS_VECTOR_VERSION,
        raster_dataset_version=MERIT_FLOWDIR_RASTER_VERSION.format(pfaf=pfaf),
        vector_data_source=MERIT_BASINS_VECTOR_SOURCE,
        raster_data_source=MERIT_FLOWDIR_RASTER_SOURCE.format(pfaf=pfaf),
        snap_source=snap_reference.source,
        official_merit_upa_km2=snap_reference.official_merit_upa_km2,
        polygon_area_km2=polygon_area,
        validation_sources=validation_sources,
        regional_flowdir_cached=bool(merit_check_routing_region_cache(pfaf).get("flowdir_ready")),
        execution_mode="local_vector_topology_terminal_raster_refinement",
        peak_memory_mb=round(_peak_memory_mb(), 3),
        runtime_seconds=round(time.perf_counter() - started, 3),
        fallback_history=fallback_history,
        memory_telemetry={
            "rss_before_mb": round(rss_before, 3),
            "rss_after_topology_load_mb": round(rss_after_topology_load, 3),
            "rss_after_vector_assembly_mb": round(rss_after_vector_assembly, 3),
            "rss_after_terminal_refinement_mb": round(rss_after_terminal_refinement, 3),
            "rss_after_merge_mb": round(rss_after_merge, 3),
            "rss_peak_mb": round(rss_peak, 3),
            "rss_delta_mb": round(max(0.0, rss_peak - rss_before), 3),
        },
        safe_envelope_version=MERIT_SAFE_ENVELOPE_VERSION,
        terminal_catchment_id=terminal_id,
        upstream_catchment_count=len(upstream_ids),
        terminal_refinement_used=terminal_refinement_used,
        vector_assembly_area_km2=vector_area,
        refined_polygon_area_km2=refinement_area if refinement_area is not None else polygon_area,
    )


def merit_build_local_upstream_area_cache(pfaf_region: str) -> dict[str, Any]:
    """Build an optional local upstream-area cache for offline/benchmark workflows."""
    started = time.perf_counter()
    rss_before = _rss_memory_mb()
    import rasterio

    from ai_hydro.data.merit_manager import MeritDataManager

    mgr = MeritDataManager()
    flowdir = mgr.flowdir_path(pfaf_region)
    if not flowdir:
        raise FileNotFoundError(f"Local MERIT flow direction raster missing for basin {pfaf_region}.")
    try:
        import pyflwdir
    except Exception as exc:
        raise RuntimeError("pyflwdir is required to build local upstream-area cache.") from exc
    with rasterio.open(flowdir) as src:
        cells = int(src.width * src.height)
        if cells > _MAX_LOCAL_FLOWDIR_CELLS:
            raise RuntimeError(
                "LOCAL_ROUTING_MEMORY_RISK: offline upstream-area cache build would "
                f"process {cells} cells, exceeding the safe limit {_MAX_LOCAL_FLOWDIR_CELLS}."
            )
        dir_arr = src.read(1)
        rss_after_flowdir_read = _rss_memory_mb()
        profile = src.profile.copy()
        transform = src.transform
        crs = src.crs.to_string() if src.crs else "EPSG:4326"
    d8 = merit_dir_to_pyflwdir(dir_arr)
    flw = pyflwdir.from_array(
        d8,
        ftype="d8",
        mask=np.isin(d8, [0, 1, 2, 4, 8, 16, 32, 64, 128]),
        transform=transform,
        latlon=crs.upper() in ("EPSG:4326", "OGC:CRS84"),
    )
    rss_after_pyflwdir_build = _rss_memory_mb()
    uparea = flw.upstream_area(unit="km2").astype("float32")
    rss_after_upstream_area = _rss_memory_mb()
    dest = mgr._local_upstream_area_path(pfaf_region)  # internal path by design
    dest.parent.mkdir(parents=True, exist_ok=True)
    profile.update(dtype="float32", count=1, nodata=-9999.0)
    with rasterio.open(dest, "w", **profile) as dst:
        dst.write(uparea, 1)
    rss_after_write = _rss_memory_mb()
    rss_peak = max(
        rss_before,
        rss_after_flowdir_read,
        rss_after_pyflwdir_build,
        rss_after_upstream_area,
        rss_after_write,
    )
    return {
        "pfaf_region": pfaf_region.zfill(2),
        "local_upstream_area_path": str(dest),
        "build_runtime_seconds": round(time.perf_counter() - started, 3),
        "memory_telemetry": {
            "rss_before_mb": round(rss_before, 3),
            "rss_after_flowdir_read_mb": round(rss_after_flowdir_read, 3),
            "rss_after_pyflwdir_build_mb": round(rss_after_pyflwdir_build, 3),
            "rss_after_upstream_area_mb": round(rss_after_upstream_area, 3),
            "rss_after_write_mb": round(rss_after_write, 3),
            "rss_peak_mb": round(rss_peak, 3),
            "rss_delta_mb": round(max(0.0, rss_peak - rss_before), 3),
        },
    }


def merit_build_offline_snap_cache(
    pfaf_region: str,
    *,
    offline_snap_asset: str = "published_accum",
) -> dict[str, Any]:
    """
    Build explicit offline snap support for a staged flowdir region.

    The default expert/offline route uses a staged published accumulation
    product. Full-Pfaf local upstream-area derivation remains opt-in only.
    Online delineation does not require either cache.
    """
    from ai_hydro.data.merit_manager import MeritDataManager

    mgr = MeritDataManager()
    if offline_snap_asset == "published_accum":
        status = mgr.routing_region_cache(pfaf_region)
        if not status.accum_ready:
            return {
                "pfaf_region": pfaf_region.zfill(2),
                "snap_cache_type": "published_accum",
                "ready": False,
                "quality_flags": ["REGIONAL_FLOWDIR_STAGING_REQUIRED"],
                "message": (
                    "Published MERIT accumulation is not staged. This expert/offline "
                    "snap cache is optional and is never downloaded for normal online routing."
                ),
            }
        return {
            "pfaf_region": pfaf_region.zfill(2),
            "snap_cache_type": "published_accum",
            "ready": True,
            "accum_path": status.accum_path,
            "usage": "offline-prepared mode only; online mode uses tiny GEE upa/wth.",
        }
    if offline_snap_asset != "local_upstream_area":
        raise ValueError("offline_snap_asset must be 'published_accum' or 'local_upstream_area'.")
    result = merit_build_local_upstream_area_cache(pfaf_region)
    return {
        **result,
        "snap_cache_type": "local_upstream_area",
        "usage": "offline-prepared mode only; online mode uses tiny GEE upa/wth.",
    }
