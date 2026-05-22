"""
Tiered watershed delineation router for global pour points.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

import geopandas as gpd

from ai_hydro.analysis.delineation.nldi_point import is_conus
from ai_hydro.analysis.delineation.types import FastDelineationResult
from ai_hydro.core import DataSource, HydroMeta, HydroResult, ToolError

log = logging.getLogger(__name__)

Method = Literal["auto", "fast", "merit_basins"]

_TOOL_PATH = "ai_hydro.analysis.delineation.router.delineate_from_point"
_AREA_MISMATCH_FRAC = 0.25
_MIN_AREA_KM2 = 1.0
_LARGE_SNAP_M = 5000.0
_NLDI_QUICK_MIN_KM2 = 1.0
_NLDI_QUICK_MAX_KM2 = 15_000.0

_SOURCES_FAST = [
    DataSource(
        name="MERIT-Hydro (Planetary Computer)",
        url="https://planetarycomputer.microsoft.com/dataset/merit-hydro",
        citation="@dataset{MERITHydro2019}",
    ),
    DataSource(
        name="Copernicus DEM GLO-30",
        url="https://planetarycomputer.microsoft.com/dataset/cop-dem-glo-30",
    ),
]

_SOURCES_MERIT = [
    DataSource(
        name="MERIT-Basins",
        url="https://www.reachhydro.org/home/params/merit-basins",
    ),
    DataSource(
        name="Upstream Delineator",
        url="https://github.com/Upstream-Tech/delineator",
    ),
]


def _gdf_to_geojson(gdf: gpd.GeoDataFrame) -> dict[str, Any]:
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(4326)
    subset = gdf[~gdf.geometry.is_empty]
    if subset.empty:
        raise ToolError(
            code="DELINEATION_FAILED",
            message="Empty geometry after delineation.",
            tool=_TOOL_PATH,
        )
    fc = json.loads(subset.to_json())
    feat = fc["features"][0]
    feat.setdefault("properties", {})
    return feat


def _should_escalate(
    fast_result,
    *,
    expected_area_km2: float | None,
) -> str | None:
    if fast_result.area_km2 < _MIN_AREA_KM2:
        return f"area {fast_result.area_km2:.2f} km² below minimum"
    if fast_result.scout_box_maxed:
        return "scout extent reached maximum (possible truncation)"
    if (
        expected_area_km2
        and expected_area_km2 > 0
        and abs(fast_result.area_km2 - expected_area_km2) / expected_area_km2
        > _AREA_MISMATCH_FRAC
    ):
        return (
            f"area {fast_result.area_km2:.0f} km² differs from expected "
            f"{expected_area_km2:.0f} km² by >{_AREA_MISMATCH_FRAC:.0%}"
        )
    if getattr(fast_result, "used_nldi_basin", False):
        return None
    if (
        fast_result.merit_snap_distance_m is None
        or fast_result.merit_snap_distance_m > _LARGE_SNAP_M
    ):
        if expected_area_km2 and expected_area_km2 > 10:
            return "MERIT vector snap unavailable or outlet far from river network"
        if fast_result.area_km2 >= max(_MIN_AREA_KM2 * 5, 10.0):
            return None
        return "MERIT vector snap unavailable or outlet far from river network"
    return None


def delineate_from_point(
    lat: float,
    lon: float,
    *,
    expected_area_km2: float | None = None,
    method: Method = "auto",
    verbose: bool = False,
    name: str | None = None,
) -> HydroResult:
    """
    Delineate a watershed from a pour point (WGS84 decimal degrees).

    Parameters
    ----------
    lat, lon : float
        Outlet coordinates (EPSG:4326).
    expected_area_km2 : float, optional
        Prior drainage area for validation / adaptive snapping.
    method : str
        ``auto`` (fast then merit_basins on failure), ``fast``, or ``merit_basins``.
    """
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ToolError(
            code="INVALID_COORDS",
            message="lat/lon out of valid WGS84 range.",
            tool=_TOOL_PATH,
        )

    method_used = method
    escalation_reason: str | None = None
    pfaf_code: str | None = None
    snap_distance_m: float | None = None
    gdf: gpd.GeoDataFrame | None = None
    area: float = 0.0

    run_fast = method in ("auto", "fast")
    run_merit = method in ("auto", "merit_basins")

    # CONUS auto with known drainage area: NLDI COMID search vs expected_area_km2.
    if method == "auto" and is_conus(lat, lon) and expected_area_km2 and expected_area_km2 > 0:
        try:
            from ai_hydro.analysis.delineation.nldi_point import delineate_nldi_at_point

            nldi_result = delineate_nldi_at_point(
                lat, lon, expected_area_km2=expected_area_km2
            )
            nldi_area = nldi_result.data.get("area_km2", 0)
            nldi_ok = nldi_area >= _MIN_AREA_KM2
            if expected_area_km2 and expected_area_km2 > 0:
                nldi_ok = nldi_ok and (
                    abs(nldi_area - expected_area_km2) / expected_area_km2
                    <= _AREA_MISMATCH_FRAC
                )
            if nldi_ok:
                return nldi_result
            escalation_reason = (
                f"NLDI COMID area {nldi_area:.0f} km² "
                f"vs expected {expected_area_km2:.0f} km²"
                if expected_area_km2
                else f"NLDI COMID area {nldi_area:.0f} km² too small"
            )
            log.info("NLDI COMID tier skipped: %s", escalation_reason)
        except Exception as e:
            log.info("NLDI COMID tier unavailable: %s", e)
            escalation_reason = str(e)

    # CONUS auto without expected area: fast NLDI (mainstem COMID), then cloud DEM if out of range.
    if (
        method == "auto"
        and is_conus(lat, lon)
        and not (expected_area_km2 and expected_area_km2 > 0)
    ):
        try:
            from ai_hydro.analysis.delineation.nldi_point import delineate_nldi_at_point

            nldi_result = delineate_nldi_at_point(lat, lon)
            nldi_area = float(nldi_result.data.get("area_km2", 0))
            if _NLDI_QUICK_MIN_KM2 <= nldi_area <= _NLDI_QUICK_MAX_KM2:
                return nldi_result
            escalation_reason = (
                f"NLDI quick area {nldi_area:.0f} km² outside "
                f"{_NLDI_QUICK_MIN_KM2:.0f}–{_NLDI_QUICK_MAX_KM2:.0f} km²"
            )
            log.info("NLDI quick tier skipped: %s", escalation_reason)
        except Exception as e:
            log.info("NLDI quick tier unavailable: %s", e)
            escalation_reason = str(e)

    if run_fast:
        try:
            from ai_hydro.analysis.delineation.pysheds_pipeline import delineate_fast

            fast = delineate_fast(
                lat,
                lon,
                expected_area_km2=expected_area_km2,
                verbose=verbose,
            )
            pfaf_code = fast.pfaf_code
            snap_distance_m = fast.merit_snap_distance_m
            escalation_reason = _should_escalate(
                fast, expected_area_km2=expected_area_km2
            )
            if method == "fast" or not escalation_reason:
                gdf = fast.gdf
                area = fast.area_km2
                method_used = "fast"
            elif method == "auto":
                log.info("Escalating to merit_basins: %s", escalation_reason)
                run_merit = True
        except Exception as e:
            log.warning("Fast tier failed: %s", e)
            if method == "fast":
                raise ToolError(
                    code="DELINEATION_FAILED",
                    message=str(e),
                    tool=_TOOL_PATH,
                ) from e
            run_merit = True
            escalation_reason = str(e)

    if run_merit and gdf is None:
        try:
            from ai_hydro.analysis.delineation.delineator_adapter import delineate_merit_basins

            merit = delineate_merit_basins(
                lat,
                lon,
                outlet_id=(name or "outlet").replace(" ", "_")[:32],
                verbose=verbose,
            )
            gdf = merit.gdf
            area = merit.area_km2
            pfaf_code = merit.pfaf_code
            method_used = "merit_basins"
        except Exception as e:
            if method == "merit_basins":
                raise ToolError(
                    code="DELINEATION_FAILED",
                    message=str(e),
                    tool=_TOOL_PATH,
                ) from e
            raise ToolError(
                code="DELINEATION_FAILED",
                message=(
                    f"All tiers failed. Fast: {escalation_reason or 'skipped'}. "
                    f"MERIT-Basins: {e}"
                ),
                tool=_TOOL_PATH,
                recovery="Run merit_ensure_basin or nudge the outlet onto the main channel.",
            ) from e

    if gdf is None or gdf.empty or area < _MIN_AREA_KM2:
        raise ToolError(
            code="DELINEATION_FAILED",
            message="No valid watershed polygon produced. Try moving the outlet slightly.",
            tool=_TOOL_PATH,
        )

    geojson = _gdf_to_geojson(gdf)
    sources = _SOURCES_MERIT if method_used == "merit_basins" else _SOURCES_FAST

    import ai_hydro

    meta = HydroMeta(
        tool=_TOOL_PATH,
        version=getattr(ai_hydro, "__version__", "unknown"),
        params={
            "lat": lat,
            "lon": lon,
            "method": method,
            "method_used": method_used,
            "expected_area_km2": expected_area_km2,
            "name": name,
        },
        sources=sources,
    )

    return HydroResult(
        data={
            "geometry_geojson": geojson,
            "area_km2": round(area, 3),
            "outlet_lat": lat,
            "outlet_lon": lon,
            "method_used": method_used,
            "pfaf_code": pfaf_code,
            "snap_distance_m": snap_distance_m,
            "escalation_reason": escalation_reason,
            "name": name or "watershed",
        },
        meta=meta,
    )
