"""
Fluvial flood inundation via HAND + synthetic rating curve (Manning).

Phase 1 core: conditioned DEM → HAND grid → stage from discharge → depth/extent
with low/likely/high uncertainty band (Manning's n sweep).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ai_hydro.analysis.inundation_spike import (
    build_stage_extent_lookup,
    compute_hand_from_pysheds,
    depth_at_stage,
    pysheds_flowdir_to_pyflwdir,
)

log = logging.getLogger(__name__)

__all__ = [
    "INUNDATION_SCOPE",
    "INUNDATION_CAVEAT",
    "manning_stage_rectangular",
    "prepare_hand_stack",
    "compute_inundation",
    "compute_inundation_from_stack",
    "compute_inundation_result",
]

INUNDATION_SCOPE: dict[str, Any] = {
    "flood_type": "fluvial",
    "hydraulics": "steady_state_level_pool",
    "hand_variant": "single_source",
    "dem_resolution_m": None,
    "not_applicable": ["pluvial", "coastal", "compound", "dam_break", "backwater"],
}

INUNDATION_CAVEAT = (
    "Terrain-index inundation (HAND + synthetic rating curve). "
    "Not validated for life-safety decisions. "
    "Small streams and urban areas may be unreliable at coarse DEM resolution."
)

DEFAULT_MANNING_N = 0.035
MANNING_N_LOW = 0.030
MANNING_N_HIGH = 0.050


@dataclass
class InundationBand:
    label: str
    manning_n: float
    stage_m: float
    depth: np.ndarray
    inundated_mask: np.ndarray
    area_km2: float
    max_depth_m: float


@dataclass
class InundationResult:
    discharge_m3s: float
    hand: np.ndarray
    bands: dict[str, InundationBand]
    stage_lookup: dict[float, int]
    bounds: list[float]
    crs: str
    cell_size_m: float
    scope: dict[str, Any] = field(default_factory=dict)
    dem_product: str | None = None
    dem_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        likely = self.bands.get("likely")
        return {
            "discharge_m3s": float(self.discharge_m3s),
            "stage_likely_m": float(likely.stage_m) if likely else None,
            "area_km2_likely": float(likely.area_km2) if likely else None,
            "max_depth_likely_m": float(likely.max_depth_m) if likely else None,
            "area_km2_low": float(self.bands["low"].area_km2) if "low" in self.bands else None,
            "area_km2_high": float(self.bands["high"].area_km2) if "high" in self.bands else None,
            "stage_lookup": self.stage_lookup,
            "bounds": self.bounds,
            "crs": self.crs,
            "cell_size_m": float(self.cell_size_m),
            "scope": self.scope,
            "caveat": INUNDATION_CAVEAT,
            "dem_product": self.dem_product,
            "dem_source": self.dem_source,
        }


def manning_stage_rectangular(
    discharge_m3s: float,
    *,
    width_m: float,
    slope: float,
    manning_n: float = DEFAULT_MANNING_N,
    h_max: float = 50.0,
) -> float:
    """
    Invert Manning's equation for a wide rectangular channel (level-pool stage).

    Q = (1/n) * A * R^(2/3) * sqrt(S),  A = w*h,  R ≈ h for wide channels.
    """
    q = float(discharge_m3s)
    w = max(float(width_m), 1.0)
    s = max(float(slope), 1e-6)
    n = max(float(manning_n), 0.01)
    if q <= 0:
        return 0.0

    def _q_at_h(h: float) -> float:
        a = w * h
        p = w + 2.0 * h
        r = a / p
        return (1.0 / n) * a * (r ** (2.0 / 3.0)) * (s ** 0.5)

    lo, hi = 0.0, h_max
    if _q_at_h(hi) < q:
        return hi
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if _q_at_h(mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _estimate_channel_geometry(
    elev: np.ndarray,
    acc: np.ndarray,
    *,
    cell_size_m: float,
    stream_acc_threshold: int,
) -> tuple[float, float, float]:
    """Return (width_m, slope, outlet_stage_m) heuristics at main-stem outlet."""
    acc_a = np.asarray(acc, dtype=float)
    elev_a = np.asarray(elev, dtype=float)
    stream = acc_a >= float(stream_acc_threshold)
    if not np.any(stream):
        stream = acc_a >= max(5.0, float(np.percentile(acc_a, 95)))

    outlet_idx = np.unravel_index(int(np.argmax(np.where(stream, acc_a, 0))), acc_a.shape)
    acc_out = max(float(acc_a[outlet_idx]), 1.0)
    width_m = max(cell_size_m * (acc_out ** 0.5) * 0.5, cell_size_m * 2.0)

    gy, gx = np.gradient(elev_a, cell_size_m, cell_size_m)
    slope = float(np.hypot(gy[outlet_idx], gx[outlet_idx]))
    slope = max(slope, 0.001)

    return width_m, slope, float(elev_a[outlet_idx])


def _clip_array_to_watershed(
    arr: np.ndarray,
    watershed_geom,
    transform,
    dem_crs,
) -> np.ndarray:
    """Mask *arr* to watershed polygon (NaN outside)."""
    try:
        from rasterio.features import geometry_mask
        from rasterio.transform import Affine
        from shapely.ops import transform as shp_transform
        import pyproj
        from shapely.geometry import mapping

        rows, cols = arr.shape
        if hasattr(transform, "a"):
            affine = transform
        else:
            affine = Affine(*transform[:6]) if len(transform) >= 6 else transform

        src_crs = pyproj.CRS("EPSG:4326")
        dem_crs_obj = pyproj.CRS.from_user_input(dem_crs) if dem_crs else src_crs
        transformer = pyproj.Transformer.from_crs(src_crs, dem_crs_obj, always_xy=True)
        geom_proj = shp_transform(transformer.transform, watershed_geom)
        outside = geometry_mask(
            [mapping(geom_proj)],
            transform=affine,
            invert=False,
            out_shape=(rows, cols),
        )
        out = np.asarray(arr, dtype=float)
        out[outside] = np.nan
        return out
    except Exception as exc:
        log.warning("Watershed clip failed (%s); using full raster.", exc)
        return np.asarray(arr, dtype=float)


def _prepare_hand_stack(
    watershed_geom,
    *,
    resolution: int = 30,
    stream_acc_threshold: int = 100,
) -> dict[str, Any]:
    """DEM → conditioned flow → HAND (mirrors TWI pipeline)."""
    from ai_hydro.analysis._dem import fetch_dem, _geom_in_conus, slope_from_dem
    import tempfile
    import os
    from pysheds.grid import Grid

    if resolution not in (10, 30, 60):
        raise ValueError(f"resolution must be 10, 30, or 60; got {resolution}")

    _is_conus = _geom_in_conus(watershed_geom)
    dem, dem_product, dem_source = fetch_dem(watershed_geom, resolution=resolution, prefer="auto")

    if _is_conus:
        try:
            dem = dem.rio.reproject("EPSG:5070")
        except Exception as exc:
            log.warning("DEM reproject to EPSG:5070 failed: %s", exc)
    else:
        try:
            _centroid = watershed_geom.centroid if hasattr(watershed_geom, "centroid") else watershed_geom
            lat0 = float(getattr(_centroid, "y", 0.0))
            lon0 = float(getattr(_centroid, "x", 0.0))
            zone = int((lon0 + 180) // 6) + 1
            epsg = 32600 + zone if lat0 >= 0 else 32700 + zone
            dem = dem.rio.reproject(f"EPSG:{epsg}")
            if dem.ndim == 3:
                dem = dem.squeeze(drop=True)
        except Exception as exc:
            log.warning("UTM reproject failed: %s", exc)
            if dem.ndim == 3:
                dem = dem.squeeze(drop=True)

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        dem.rio.to_raster(tmp_path)
        grid = Grid.from_raster(tmp_path)
        dem_data = grid.read_raster(tmp_path)
        pit = grid.fill_pits(dem_data)
        flooded = grid.fill_depressions(pit)
        inflated = grid.resolve_flats(flooded)
        fdir = grid.flowdir(inflated)
        acc = grid.accumulation(fdir)
        elev = np.asarray(inflated, dtype=np.float32)

        if _is_conus:
            try:
                import py3dep
                slope_deg = py3dep.get_map(
                    "Slope Degrees",
                    watershed_geom,
                    resolution=resolution,
                    geo_crs=4326,
                    crs=5070,
                )
            except Exception:
                slope_deg = slope_from_dem(dem)
        else:
            slope_deg = slope_from_dem(dem)

        hand = compute_hand_from_pysheds(
            elev,
            fdir,
            acc,
            stream_acc_threshold=stream_acc_threshold,
            transform=grid.affine,
        )
        hand = _clip_array_to_watershed(hand, watershed_geom, grid.affine, str(dem.rio.crs))

        cell_size = abs(float(dem.rio.resolution()[0]))
        bounds = [float(x) for x in dem.rio.bounds()]

        return {
            "hand": hand,
            "elev": elev,
            "acc": np.asarray(acc),
            "fdir": fdir,
            "grid_affine": grid.affine,
            "cell_size_m": cell_size,
            "bounds": bounds,
            "crs": str(dem.rio.crs),
            "dem_product": dem_product,
            "dem_source": dem_source,
        }
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


prepare_hand_stack = _prepare_hand_stack


def _band_stats(
    depth: np.ndarray,
    inundated: np.ndarray,
    cell_size_m: float,
) -> tuple[float, float]:
    if not np.any(inundated):
        return 0.0, 0.0
    area_km2 = float(inundated.sum()) * (cell_size_m ** 2) / 1e6
    max_depth = float(np.nanmax(depth[inundated]))
    return area_km2, max_depth


def compute_inundation(
    watershed_geom,
    discharge_m3s: float,
    *,
    resolution: int = 30,
    manning_n: float = DEFAULT_MANNING_N,
    manning_n_low: float = MANNING_N_LOW,
    manning_n_high: float = MANNING_N_HIGH,
    stream_acc_threshold: int = 100,
    hand_stack: dict[str, Any] | None = None,
) -> InundationResult:
    """
    Compute HAND-based inundation depth/extent for a discharge (m³/s).

    Returns low/likely/high bands from Manning's n uncertainty sweep.
    Pass ``hand_stack`` from a prior call to avoid re-fetching the DEM.
    """
    stack = hand_stack or _prepare_hand_stack(
        watershed_geom,
        resolution=resolution,
        stream_acc_threshold=stream_acc_threshold,
    )
    return compute_inundation_from_stack(
        stack,
        discharge_m3s,
        resolution=resolution,
        manning_n=manning_n,
        manning_n_low=manning_n_low,
        manning_n_high=manning_n_high,
        stream_acc_threshold=stream_acc_threshold,
    )


def compute_inundation_from_stack(
    stack: dict[str, Any],
    discharge_m3s: float,
    *,
    resolution: int = 30,
    manning_n: float = DEFAULT_MANNING_N,
    manning_n_low: float = MANNING_N_LOW,
    manning_n_high: float = MANNING_N_HIGH,
    stream_acc_threshold: int = 100,
) -> InundationResult:
    """Compute inundation bands from a pre-built HAND stack (no DEM I/O)."""
    hand = stack["hand"]
    valid = np.isfinite(hand)
    if not np.any(valid):
        raise ValueError("HAND grid has no valid cells inside watershed.")

    width_m, slope, _ = _estimate_channel_geometry(
        stack["elev"],
        stack["acc"],
        cell_size_m=stack["cell_size_m"],
        stream_acc_threshold=stream_acc_threshold,
    )

    q = float(discharge_m3s)
    n_triple = {
        "low": float(manning_n_low),
        "likely": float(manning_n),
        "high": float(manning_n_high),
    }
    bands: dict[str, InundationBand] = {}
    for label, n_val in n_triple.items():
        stage = manning_stage_rectangular(q, width_m=width_m, slope=slope, manning_n=n_val)
        depth, mask = depth_at_stage(hand, stage)
        depth = np.where(valid, depth, np.nan)
        mask = mask & valid
        area_km2, max_depth = _band_stats(depth, mask, stack["cell_size_m"])
        bands[label] = InundationBand(
            label=label,
            manning_n=n_val,
            stage_m=stage,
            depth=depth,
            inundated_mask=mask,
            area_km2=area_km2,
            max_depth_m=max_depth,
        )

    lookup = build_stage_extent_lookup(hand)

    scope = {
        **INUNDATION_SCOPE,
        "dem_resolution_m": resolution,
        "manning_n_likely": manning_n,
        "channel_width_m_est": width_m,
        "channel_slope_est": slope,
    }

    return InundationResult(
        discharge_m3s=q,
        hand=hand,
        bands=bands,
        stage_lookup=lookup,
        bounds=stack["bounds"],
        crs=stack["crs"],
        cell_size_m=stack["cell_size_m"],
        scope=scope,
        dem_product=stack.get("dem_product"),
        dem_source=stack.get("dem_source"),
    )


def extent_geojson_from_mask(
    mask: np.ndarray,
    *,
    transform,
    crs: str,
) -> dict[str, Any]:
    """Polygonize inundated cells to a GeoJSON FeatureCollection (WGS84)."""
    from rasterio.features import shapes
    from rasterio.transform import Affine
    from shapely.geometry import mapping, shape
    from shapely.ops import transform as shp_transform
    import pyproj

    if not np.any(mask):
        return {"type": "FeatureCollection", "features": []}

    if hasattr(transform, "a"):
        affine = transform
    else:
        affine = Affine(*transform[:6])

    feats = []
    for geom, val in shapes(mask.astype(np.uint8), mask=mask, transform=affine):
        if int(val) != 1:
            continue
        shp = shape(geom)
        if shp.is_empty:
            continue
        try:
            src = pyproj.CRS.from_user_input(crs)
            dst = pyproj.CRS.from_epsg(4326)
            if not src.is_geographic:
                transformer = pyproj.Transformer.from_crs(src, dst, always_xy=True)
                shp = shp_transform(transformer.transform, shp)
            shp = shp.simplify(tolerance=0.0001, preserve_topology=True)
            if shp.is_empty:
                continue
            feats.append({"type": "Feature", "geometry": mapping(shp), "properties": {}})
        except Exception as exc:
            log.debug("Skip polygon feature: %s", exc)
            continue

    return {"type": "FeatureCollection", "features": feats}


def compute_inundation_result(
    watershed_geojson: dict,
    discharge_m3s: float,
    *,
    resolution: int = 30,
    manning_n: float = DEFAULT_MANNING_N,
) -> "HydroResult":
    """Standard HydroResult wrapper for MCP / claims."""
    from shapely.geometry import shape
    from ai_hydro.core import DataSource, HydroMeta, HydroResult, ToolError

    _TOOL = "ai_hydro.analysis.inundation.compute_inundation_result"
    try:
        from shapely.geometry import shape

        if isinstance(watershed_geojson, dict):
            gtype = watershed_geojson.get("type")
            if gtype == "FeatureCollection":
                feats = watershed_geojson.get("features") or []
                if not feats:
                    raise ValueError("Empty FeatureCollection")
                geom = shape(feats[0]["geometry"])
            elif gtype == "Feature":
                geom = shape(watershed_geojson["geometry"])
            else:
                geom = shape(watershed_geojson)
        else:
            raise ValueError("watershed_geojson must be a GeoJSON dict")

        raw = compute_inundation(
            geom,
            discharge_m3s,
            resolution=resolution,
            manning_n=manning_n,
        )
        data = raw.to_dict()
        sources = [
            DataSource(
                name="HAND inundation (terrain index)",
                url="https://github.com/NOAA-OWP/inundation-mapping",
                citation="@misc{noaa_owp_hand, title={NOAA OWP HAND FIM}, year={2023}}",
            ),
        ]
        if raw.dem_product:
            sources.append(
                DataSource(name=raw.dem_product, url="", citation="")
            )
        from ai_hydro import __version__
        return HydroResult(
            data=data,
            meta=HydroMeta(
                tool=_TOOL,
                version=__version__,
                gauge_id=None,
                sources=sources,
                params={
                    "discharge_m3s": discharge_m3s,
                    "resolution": resolution,
                    "manning_n": manning_n,
                },
            ),
        )
    except Exception as exc:
        raise ToolError(
            code="INUNDATION_FAILED",
            message=str(exc),
            tool=_TOOL,
            recovery="Delineate watershed first; ensure geomorphic extras installed.",
        ) from exc
