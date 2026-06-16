"""
Tiered watershed delineation router for global pour points.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

import geopandas as gpd

from ai_hydro.analysis.delineation.nldi_point import is_conus
from ai_hydro.analysis.delineation.types import FastDelineationResult
from ai_hydro.core import DataSource, HydroMeta, HydroResult, ToolError

log = logging.getLogger(__name__)

Method = Literal[
    "auto",
    "nldi",
    "merit_gee",
    "local_merit",
    "merit_basins",
    "dem_raw_fallback",
    "fast",
]

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

_SOURCES_MERIT_GEE = [
    DataSource(
        name="MERIT Hydro via Google Earth Engine",
        url="https://developers.google.com/earth-engine/datasets/catalog/MERIT_Hydro_v1_0_1",
        citation="@article{Yamazaki2019MERITHydro}",
    ),
    DataSource(
        name="pyflwdir",
        url="https://deltares.github.io/pyflwdir/",
    ),
]

_SOURCES_MERIT = [
    DataSource(
        name="MERIT-Basins",
        url="https://www.reachhydro.org/home/params/merit-basins",
    ),
    DataSource(
        name="MERIT Hydro regional flow-direction rasters",
        url="https://mghydro.com/watersheds/raster/",
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


def _workflow_steps(
    method_used: str,
    *,
    routing_dataset: str | None = None,
    expected_area_km2: float | None = None,
    escalation_reason: str | None = None,
) -> list[dict[str, Any]]:
    if method_used == "nldi_comid":
        return [
            {
                "step": "nldi_comid_lookup",
                "data": "USGS NLDI / NHDPlus",
                "purpose": "Find upstream basin polygon for the outlet COMID.",
            },
            {
                "step": "area_validation",
                "data": "NLDI basin area",
                "purpose": "Accept basin when area is plausible for the request.",
            },
        ]
    if method_used == "merit_gee_pyflwdir":
        steps = [
            {
                "step": "gee_fetch_merit_hydro",
                "data": routing_dataset or "MERIT/Hydro/v1_0_1",
                "purpose": "Fetch conditioned MERIT flow direction, upstream area, and river-width bands.",
            },
            {
                "step": "outlet_snap",
                "data": "MERIT upa/wth",
                "purpose": "Move the pour point to a nearby MERIT drainage cell.",
            },
            {
                "step": "local_flow_routing",
                "data": "MERIT dir + pyflwdir",
                "purpose": "Trace the upstream basin on a conditioned global flow-direction raster.",
            },
            {
                "step": "polygonize_and_validate",
                "data": "Basin mask, MERIT upa, expected_area_km2",
                "purpose": "Vectorize the watershed and report area/quality checks.",
            },
        ]
        if expected_area_km2:
            steps[1]["purpose"] = "Use expected area as a prior while snapping to a MERIT drainage cell."
        return steps
    if method_used == "local_merit_pyflwdir":
        return [
            {
                "step": "gee_snap_reference",
                "data": "MERIT upa/wth",
                "purpose": "Snap outlet and obtain official MERIT upstream area for validation.",
            },
            {
                "step": "load_regional_flowdir",
                "data": "Cached regional MERIT flowdir",
                "purpose": "Use a staged Pfaf-region flow-direction raster without requiring accumulation.",
            },
            {
                "step": "local_flow_routing",
                "data": "MERIT flowdir + pyflwdir",
                "purpose": "Trace the upstream basin on conditioned local flow direction.",
            },
            {
                "step": "polygonize_and_validate",
                "data": "Basin polygon + official MERIT upa",
                "purpose": "Vectorize the watershed and validate primarily against GEE MERIT upa.",
            },
        ]
    if method_used == "merit_basins_hybrid":
        return [
            {
                "step": "gee_snap_reference",
                "data": "MERIT upa/wth",
                "purpose": "Snap outlet and obtain official MERIT upstream area for validation.",
            },
            {
                "step": "load_merit_basins_topology",
                "data": "Cached regional MERIT-Basins catchments",
                "purpose": "Resolve terminal catchment and traverse upstream unit-catchment topology.",
            },
            {
                "step": "vector_topology_assembly",
                "data": "MERIT-Basins catchment polygons",
                "purpose": "Assemble and dissolve upstream vector catchments for large-basin routing.",
            },
            {
                "step": "terminal_raster_refinement",
                "data": "Cached regional MERIT flowdir",
                "purpose": "Refine only the terminal outlet catchment when raster routing is safe.",
            },
            {
                "step": "polygonize_and_validate",
                "data": "Hybrid polygon + official MERIT upa",
                "purpose": "Validate hybrid area and return overflow provenance.",
            },
        ]
    if method_used == "dem_raw_fallback":
        return [
            {
                "step": "raw_dem_fetch",
                "data": "NASADEM / Copernicus DEM",
                "purpose": "Fetch elevation tiles for fallback routing.",
            },
            {
                "step": "pysheds_conditioning",
                "data": "Raw DEM",
                "purpose": "Fill pits/depressions and derive flow direction locally.",
            },
            {
                "step": "fallback_warning",
                "data": escalation_reason or "RAW_DEM_EXPERIMENTAL_FALLBACK",
                "purpose": "Flag that this is not the production global default.",
            },
        ]
    if method_used in ("merit_basins", "merit_basins_hybrid"):
        return [
            {
                "step": "load_merit_basins",
                "data": "Local MERIT-Basins vectors and flowdir",
                "purpose": "Use the expert local MERIT-Basins/upstream-delineator workflow.",
            }
        ]
    return []


def _attach_workflow_steps(
    result: HydroResult,
    *,
    method_used: str,
    routing_dataset: str | None = None,
    expected_area_km2: float | None = None,
    escalation_reason: str | None = None,
) -> HydroResult:
    result.data.setdefault(
        "workflow_steps",
        _workflow_steps(
            method_used,
            routing_dataset=routing_dataset,
            expected_area_km2=expected_area_km2,
            escalation_reason=escalation_reason,
        ),
    )
    result.data.setdefault("routing_dataset", routing_dataset or result.data.get("source"))
    result.data.setdefault("quality_flags", [])
    return result


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
        ``auto`` (NLDI in CONUS, MERIT GEE globally), ``nldi``, ``merit_gee``,
        ``local_merit``, ``merit_basins``, or ``dem_raw_fallback``. ``fast`` is
        accepted as a backward-compatible alias for ``dem_raw_fallback``.
    """
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ToolError(
            code="INVALID_COORDS",
            message="lat/lon out of valid WGS84 range.",
            tool=_TOOL_PATH,
        )

    if method == "fast":
        method = "dem_raw_fallback"

    method_used = method
    escalation_reason: str | None = None
    pfaf_code: str | None = None
    snap_distance_m: float | None = None
    gdf: gpd.GeoDataFrame | None = None
    area: float = 0.0
    routing_dataset: str | None = None
    routing_resolution_m: float | None = None
    snap_quality: str | None = None
    snap_validation: dict[str, Any] | None = None
    snapped_upa_km2: float | None = None
    official_merit_upa_km2: float | None = None
    local_upstream_area_km2: float | None = None
    polygon_area_km2: float | None = None
    area_validation: dict[str, Any] | None = None
    quality_flags: list[str] = []
    cache_key: str | None = None
    license_note: str | None = None
    citation: str | None = None
    routing_data_source: str | None = None
    snap_source: str | None = None
    validation_sources: list[str] = []
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
    memory_telemetry: dict[str, float] | None = None
    fallback_history: list[dict[str, Any]] = []
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

    run_nldi = method in ("auto", "nldi")
    run_merit_gee = method in ("auto", "merit_gee")
    run_local_merit = method == "local_merit"
    run_fast = method == "dem_raw_fallback"
    run_merit = method in ("auto", "merit_basins")

    # CONUS auto with known drainage area: NLDI COMID search vs expected_area_km2.
    if run_nldi and is_conus(lat, lon) and expected_area_km2 and expected_area_km2 > 0:
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
                return _attach_workflow_steps(
                    nldi_result,
                    method_used="nldi_comid",
                    routing_dataset="USGS NLDI / NHDPlus",
                    expected_area_km2=expected_area_km2,
                )
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
            if method == "nldi":
                raise ToolError(
                    code="DELINEATION_FAILED",
                    message=str(e),
                    tool=_TOOL_PATH,
                    recovery="NLDI is CONUS-only and network-dependent; try method='merit_gee'.",
                ) from e

    # CONUS auto without expected area: fast NLDI (mainstem COMID), then cloud DEM if out of range.
    if (
        run_nldi
        and is_conus(lat, lon)
        and not (expected_area_km2 and expected_area_km2 > 0)
    ):
        try:
            from ai_hydro.analysis.delineation.nldi_point import delineate_nldi_at_point

            nldi_result = delineate_nldi_at_point(lat, lon)
            nldi_area = float(nldi_result.data.get("area_km2", 0))
            if _NLDI_QUICK_MIN_KM2 <= nldi_area <= _NLDI_QUICK_MAX_KM2:
                return _attach_workflow_steps(
                    nldi_result,
                    method_used="nldi_comid",
                    routing_dataset="USGS NLDI / NHDPlus",
                    expected_area_km2=expected_area_km2,
                )
            escalation_reason = (
                f"NLDI quick area {nldi_area:.0f} km² outside "
                f"{_NLDI_QUICK_MIN_KM2:.0f}–{_NLDI_QUICK_MAX_KM2:.0f} km²"
            )
            log.info("NLDI quick tier skipped: %s", escalation_reason)
        except Exception as e:
            log.info("NLDI quick tier unavailable: %s", e)
            escalation_reason = str(e)
            if method == "nldi":
                raise ToolError(
                    code="DELINEATION_FAILED",
                    message=str(e),
                    tool=_TOOL_PATH,
                    recovery="NLDI is CONUS-only and network-dependent; try method='merit_gee'.",
                ) from e

    if method == "nldi":
        raise ToolError(
            code="DELINEATION_FAILED",
            message="NLDI could not return a valid watershed for this point.",
            tool=_TOOL_PATH,
            recovery="For global or non-NLDI points, call method='merit_gee' or method='auto'.",
        )

    snap_reference = None
    local_cache_status: dict[str, Any] | None = None
    if (method in ("auto", "local_merit", "merit_gee", "merit_basins")) and gdf is None:
        try:
            from ai_hydro.analysis.delineation.merit_flowdir_pipeline import (
                merit_get_snap_reference,
            )

            snap_reference = merit_get_snap_reference(
                lat,
                lon,
                expected_area_km2=expected_area_km2,
            )
        except Exception as e:
            log.info("MERIT GEE snap-reference unavailable: %s", e)
            if method == "merit_gee":
                # Full GEE delineation can still initialize/fail with a more specific error below.
                pass

        try:
            from ai_hydro.analysis.delineation.merit_flowdir_pipeline import (
                merit_check_routing_region_cache,
                merit_resolve_pfaf_region,
            )

            pfaf_code = merit_resolve_pfaf_region(lat, lon)
            local_cache_status = merit_check_routing_region_cache(pfaf_code)
            regional_flowdir_cached = bool(local_cache_status.get("flowdir_ready"))
        except Exception as e:
            log.info("MERIT regional cache status unavailable: %s", e)

    run_cached_local_merit = (
        (method in ("auto", "local_merit"))
        and gdf is None
        and bool(local_cache_status and local_cache_status.get("flowdir_ready"))
    )

    def _apply_merit_result(local: Any, *, used_method: str) -> None:
        nonlocal gdf, area, snap_distance_m, snap_quality, snap_validation
        nonlocal snapped_upa_km2, official_merit_upa_km2, local_upstream_area_km2
        nonlocal polygon_area_km2, area_validation, quality_flags, cache_key
        nonlocal pfaf_code, routing_dataset, routing_resolution_m, routing_data_source
        nonlocal snap_source, validation_sources, regional_flowdir_cached
        nonlocal execution_mode, regional_flowdir_file_size_bytes
        nonlocal window_expansion_iterations, final_window_bounds, final_window_cell_count
        nonlocal basin_touched_window_boundary, window_complete, peak_memory_mb, runtime_seconds
        nonlocal memory_telemetry
        nonlocal method_used
        nonlocal safe_envelope_version, terminal_catchment_id, upstream_catchment_count
        nonlocal terminal_refinement_used, vector_assembly_area_km2, refined_polygon_area_km2
        nonlocal vector_dataset_version, raster_dataset_version
        nonlocal vector_data_source, raster_data_source

        gdf = local.gdf
        area = local.area_km2
        snap_distance_m = local.snap_distance_m
        snap_quality = local.snap_quality
        snap_validation = local.snap_validation
        snapped_upa_km2 = local.snapped_upa_km2
        official_merit_upa_km2 = local.official_merit_upa_km2
        local_upstream_area_km2 = local.local_upstream_area_km2
        polygon_area_km2 = local.polygon_area_km2
        area_validation = local.area_validation
        quality_flags = local.quality_flags
        cache_key = local.cache_key
        pfaf_code = local.pfaf_region or pfaf_code
        routing_dataset = local.routing_dataset
        routing_resolution_m = local.routing_resolution_m
        routing_data_source = local.routing_data_source
        snap_source = local.snap_source
        validation_sources = local.validation_sources or []
        regional_flowdir_cached = local.regional_flowdir_cached
        execution_mode = local.execution_mode
        regional_flowdir_file_size_bytes = local.regional_flowdir_file_size_bytes
        window_expansion_iterations = local.window_expansion_iterations
        final_window_bounds = local.final_window_bounds
        final_window_cell_count = local.final_window_cell_count
        basin_touched_window_boundary = local.basin_touched_window_boundary
        window_complete = local.window_complete
        peak_memory_mb = local.peak_memory_mb
        runtime_seconds = local.runtime_seconds
        memory_telemetry = local.memory_telemetry
        safe_envelope_version = local.safe_envelope_version
        terminal_catchment_id = local.terminal_catchment_id
        upstream_catchment_count = local.upstream_catchment_count
        terminal_refinement_used = local.terminal_refinement_used
        vector_assembly_area_km2 = local.vector_assembly_area_km2
        refined_polygon_area_km2 = local.refined_polygon_area_km2
        vector_dataset_version = local.vector_dataset_version
        raster_dataset_version = local.raster_dataset_version
        vector_data_source = local.vector_data_source
        raster_data_source = local.raster_data_source
        method_used = used_method

    def _try_hybrid_result(reason: str) -> bool:
        nonlocal license_note, citation, run_fast, quality_flags
        try:
            from ai_hydro.analysis.delineation.merit_flowdir_pipeline import (
                MERIT_CITATION,
                MERIT_LICENSE,
                merit_basins_hybrid_delineate,
            )

            hybrid = merit_basins_hybrid_delineate(
                lat,
                lon,
                expected_area_km2=expected_area_km2,
                snap_reference=snap_reference,
                allow_offline=True,
            )
            _apply_merit_result(hybrid, used_method="merit_basins_hybrid")
            license_note = MERIT_LICENSE
            citation = MERIT_CITATION
            fallback_history.append(
                {
                    "method": "merit_basins_hybrid",
                    "outcome": "succeeded",
                    "reason": reason,
                }
            )
            run_fast = False
            return True
        except Exception as hybrid_exc:
            if (
                "MERIT_BASINS_NOT_STAGED" in str(hybrid_exc)
                and "MERIT_BASINS_NOT_STAGED" not in quality_flags
            ):
                quality_flags.append("MERIT_BASINS_NOT_STAGED")
            if (
                "HYBRID_ROUTING_REQUIRED" in str(hybrid_exc)
                and "HYBRID_ROUTING_REQUIRED" not in quality_flags
            ):
                quality_flags.append("HYBRID_ROUTING_REQUIRED")
            fallback_history.append(
                {
                    "method": "merit_basins_hybrid",
                    "outcome": "failed",
                    "reason": str(hybrid_exc),
                }
            )
            return False

    if run_cached_local_merit or (run_local_merit and gdf is None):
        try:
            from ai_hydro.analysis.delineation.merit_flowdir_pipeline import (
                MERIT_CITATION,
                MERIT_GEE_DATASET,
                MERIT_LICENSE,
                local_merit_flowdir_pyflwdir,
            )

            local = local_merit_flowdir_pyflwdir(
                lat,
                lon,
                expected_area_km2=expected_area_km2,
                snap_reference=snap_reference,
                allow_offline=True,
            )
            _apply_merit_result(local, used_method="local_merit_pyflwdir")
            license_note = MERIT_LICENSE
            citation = MERIT_CITATION
            fallback_history.append(
                {"method": "local_merit_flowdir_pyflwdir", "outcome": "succeeded"}
            )
        except Exception as e:
            if "HYBRID_ROUTING_REQUIRED" in str(e) and method in ("auto", "merit_basins"):
                if _try_hybrid_result(str(e)):
                    pass
                else:
                    raise ToolError(
                        code="HYBRID_ROUTING_REQUIRED",
                        message=str(e),
                        tool=_TOOL_PATH,
                        recovery="Stage MERIT-Basins vectors for this Pfaf region and retry method='merit_basins'.",
                    ) from e
            elif "LOCAL_MEMORY_THRESHOLD_EXCEEDED" in str(e):
                raise ToolError(
                    code="LARGE_BASIN_HYBRID_REQUIRED",
                    message="Cached regional flowdir exceeds the raster-only local processing envelope.",
                    tool=_TOOL_PATH,
                    recovery="Use the future MERIT-Basins hybrid vector traversal tier for this basin.",
                ) from e
            elif "ADAPTIVE_WINDOW_LIMIT_REACHED" in str(e) or "LARGE_BASIN_HYBRID_REQUIRED" in str(e):
                raise ToolError(
                    code="LARGE_BASIN_HYBRID_REQUIRED",
                    message=str(e),
                    tool=_TOOL_PATH,
                    recovery="Use the future MERIT-Basins hybrid vector traversal tier for this basin.",
                ) from e
            elif gdf is None:
                raise ToolError(
                    code="DELINEATION_FAILED",
                    message=str(e),
                    tool=_TOOL_PATH,
                    recovery="Install local MERIT flowdir raster or use method='merit_gee'.",
                ) from e

    if run_merit_gee and gdf is None:
        try:
            from ai_hydro.analysis.delineation.merit_flowdir_pipeline import (
                MERIT_CITATION,
                MERIT_GEE_DATASET,
                MERIT_LICENSE,
                delineate_merit_gee,
            )

            merit_gee = delineate_merit_gee(
                lat,
                lon,
                expected_area_km2=expected_area_km2,
            )
            gdf = merit_gee.gdf
            area = merit_gee.area_km2
            snap_distance_m = merit_gee.snap_distance_m
            snap_quality = merit_gee.snap_quality
            snap_validation = merit_gee.snap_validation
            snapped_upa_km2 = merit_gee.snapped_upa_km2
            official_merit_upa_km2 = merit_gee.official_merit_upa_km2
            local_upstream_area_km2 = merit_gee.local_upstream_area_km2
            polygon_area_km2 = merit_gee.polygon_area_km2
            area_validation = merit_gee.area_validation
            quality_flags = merit_gee.quality_flags
            cache_key = merit_gee.cache_key
            routing_dataset = MERIT_GEE_DATASET
            routing_resolution_m = merit_gee.routing_resolution_m
            routing_data_source = merit_gee.routing_data_source
            snap_source = merit_gee.snap_source
            validation_sources = merit_gee.validation_sources or []
            regional_flowdir_cached = merit_gee.regional_flowdir_cached
            if local_cache_status and not local_cache_status.get("flowdir_ready"):
                quality_flags.append("REGIONAL_FLOWDIR_NOT_CACHED")
            license_note = MERIT_LICENSE
            citation = MERIT_CITATION
            method_used = "merit_gee_pyflwdir"

            # Quality gate on the GEE result. delineate_merit_gee can return a
            # degenerate basin WITHOUT raising — e.g. for a very large river it
            # may snap onto a tiny tributary/coastal cell and yield an area
            # below the minimum (flagged VERY_SMALL_BASIN). With no exception,
            # escalation_reason/run_fast are never set, so the run would
            # dead-end at the final "no valid polygon" error with no fallback.
            # In auto mode, reject it and fall through to the MERIT-Basins
            # hybrid (vector topology — assembles the full upstream network)
            # and ultimately raw DEM. Mirrors _should_escalate for the fast
            # tier. Pinned method="merit_gee" keeps the strict raise downstream.
            gee_degenerate = (
                gdf is None
                or getattr(gdf, "empty", False)
                or (area is not None and area < _MIN_AREA_KM2)
            )
            if gee_degenerate and method == "auto":
                log.info(
                    "MERIT GEE result degenerate (area=%.3f km2); escalating "
                    "to MERIT-Basins hybrid.",
                    area or 0.0,
                )
                fallback_history.append(
                    {
                        "method": "merit_gee_pyflwdir",
                        "outcome": "rejected",
                        "reason": (
                            f"area {area or 0.0:.3f} km2 below "
                            f"{_MIN_AREA_KM2} km2 (likely snap failure)"
                        ),
                    }
                )
                escalation_reason = escalation_reason or "merit_gee_degenerate_result"
                gdf = None
                method_used = None
                run_fast = True
        except Exception as e:
            log.warning("MERIT GEE tier failed: %s", e)
            escalation_reason = str(e)
            if not regional_flowdir_cached:
                quality_flags.append("REGIONAL_FLOWDIR_STAGING_REQUIRED")
            reason = (
                "GEE_MEMORY_LIMIT"
                if re.search(r"memory limit|User memory limit|HTTP 400", str(e), re.I)
                else str(e)
            )
            fallback_history.append(
                {"method": "merit_gee_pyflwdir", "outcome": "failed", "reason": reason}
            )
            if local_cache_status and local_cache_status.get("flowdir_ready"):
                try:
                    from ai_hydro.analysis.delineation.merit_flowdir_pipeline import (
                        MERIT_CITATION,
                        MERIT_LICENSE,
                        local_merit_flowdir_pyflwdir,
                    )

                    local = local_merit_flowdir_pyflwdir(
                        lat,
                        lon,
                        expected_area_km2=expected_area_km2,
                        snap_reference=snap_reference,
                        allow_offline=True,
                    )
                    _apply_merit_result(local, used_method="local_merit_pyflwdir")
                    license_note = MERIT_LICENSE
                    citation = MERIT_CITATION
                    fallback_history.append(
                        {"method": "local_merit_flowdir_pyflwdir", "outcome": "succeeded"}
                    )
                    run_fast = False
                except Exception as local_exc:
                    fallback_history.append(
                        {
                            "method": "local_merit_flowdir_pyflwdir",
                            "outcome": "failed",
                            "reason": str(local_exc),
                        }
                    )
                    if "HYBRID_ROUTING_REQUIRED" in str(local_exc):
                        run_fast = not _try_hybrid_result(str(local_exc))
                    else:
                        run_fast = True
            else:
                # Design B: auto-stage MERIT flowdir on first encounter,
                # then retry local_merit_pyflwdir (tiered policy — never
                # blocks on >2 GB without user confirmation).
                #
                # Guard: only auto-stage for non-CONUS points.  CONUS has
                # adequate NLDI + raw-DEM fallbacks; staging Rhine/Ganges/etc.
                # on a CONUS failure would waste bandwidth and surprise users.
                run_fast = True  # default, overridden on successful stage
                if pfaf_code and not is_conus(lat, lon):
                    try:
                        from ai_hydro.data.merit_manager import MeritDataManager

                        mgr = MeritDataManager()
                        stage_status = mgr.auto_stage_flowdir(
                            pfaf_code,
                            progress_cb=lambda msg, done, tot: log.info(
                                "MERIT staging [Pfaf %s] %s", pfaf_code, msg
                            ),
                        )
                        if stage_status.flowdir_ready:
                            from ai_hydro.analysis.delineation.merit_flowdir_pipeline import (
                                MERIT_CITATION,
                                MERIT_LICENSE,
                                local_merit_flowdir_pyflwdir,
                            )

                            local2 = local_merit_flowdir_pyflwdir(
                                lat,
                                lon,
                                expected_area_km2=expected_area_km2,
                                snap_reference=snap_reference,
                                allow_offline=True,
                            )
                            _apply_merit_result(local2, used_method="local_merit_pyflwdir")
                            license_note = MERIT_LICENSE
                            citation = MERIT_CITATION
                            fallback_history.append(
                                {
                                    "method": "local_merit_flowdir_pyflwdir",
                                    "outcome": "succeeded_after_auto_stage",
                                    "pfaf": pfaf_code,
                                }
                            )
                            run_fast = False
                        elif hasattr(stage_status, "action_required"):
                            # Large region — surface info for agent to confirm
                            quality_flags.append("MERIT_STAGE_CONFIRM_REQUIRED")
                            fallback_history.append(
                                {
                                    "method": "merit_auto_stage",
                                    "outcome": "confirm_required",
                                    "message": stage_status.message,
                                    "size_gb": getattr(stage_status, "size_gb", None),
                                    "next_tool": "merit_ensure_routing_region",
                                }
                            )
                        else:
                            fallback_history.append(
                                {
                                    "method": "merit_auto_stage",
                                    "outcome": "failed",
                                    "reason": stage_status.message,
                                }
                            )
                    except Exception as _stage_exc:
                        log.warning(
                            "MERIT auto-staging failed for Pfaf %s: %s — "
                            "continuing to raw DEM fallback.",
                            pfaf_code, _stage_exc,
                        )
                        fallback_history.append(
                            {
                                "method": "merit_auto_stage",
                                "outcome": "exception",
                                "reason": str(_stage_exc),
                            }
                        )

            if method == "merit_gee" and gdf is None:
                raise ToolError(
                    code="DELINEATION_FAILED",
                    message=str(e),
                    tool=_TOOL_PATH,
                    recovery=(
                        "Run delineation_doctor() to check GEE auth and pyflwdir, "
                        "or use method='dem_raw_fallback' for experimental DEM routing."
                    ),
                ) from e

    # Non-CONUS accurate fallback: prefer the MERIT-Basins hybrid (vector
    # topology, ~MERIT accuracy) over raw DEM when GEE + local_merit couldn't
    # serve. This restores the documented tier order
    # (… → merit_basins → dem_raw_fallback) for the no-expected-area case, where
    # escalation-from-fast never fires (it requires expected_area_km2). CONUS
    # deliberately stays on NLDI + raw DEM — no MERIT machinery there.
    # _try_hybrid_result sets gdf + method_used + run_fast=False on success; on
    # failure (e.g. basins not staged → MERIT_BASINS_NOT_STAGED) run_fast stays
    # True so raw DEM remains the final fallback.
    if method == "auto" and gdf is None and run_fast and not is_conus(lat, lon):
        _try_hybrid_result(escalation_reason or "merit_gee_unavailable")

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
            if method == "dem_raw_fallback" or not escalation_reason:
                gdf = fast.gdf
                area = fast.area_km2
                method_used = "dem_raw_fallback"
                routing_dataset = "raw DEM (NASADEM/Copernicus)"
                routing_resolution_m = 30.0
                quality_flags.append("RAW_DEM_EXPERIMENTAL_FALLBACK")
            elif method == "auto":
                log.info("Escalating to merit_basins: %s", escalation_reason)
                run_merit = True
        except Exception as e:
            log.warning("Fast tier failed: %s", e)
            if method == "dem_raw_fallback":
                raise ToolError(
                    code="DELINEATION_FAILED",
                    message=str(e),
                    tool=_TOOL_PATH,
                ) from e
            run_merit = True
            escalation_reason = str(e)

    if run_merit and gdf is None:
        if not _try_hybrid_result(escalation_reason or "requested_merit_basins"):
            # The local MERIT-Basins hybrid (merit_basins_hybrid_delineate) IS the
            # accurate tier. If it can't run (e.g. basin vectors/flowdir not staged
            # locally), there is no further MERIT fallback to attempt — surface a
            # clear, actionable error. (A former second attempt via the optional
            # `upstream_delineator` GitHub adapter was removed: it depended on the
            # SAME local staging the hybrid requires, only stricter, so it could
            # never succeed where the hybrid failed — it only ever produced a
            # duplicate failure.)
            hybrid_reason = next(
                (
                    h.get("reason")
                    for h in reversed(fallback_history)
                    if h.get("method") == "merit_basins_hybrid"
                    and h.get("outcome") == "failed"
                ),
                "MERIT-Basins hybrid unavailable",
            )
            if method == "merit_basins":
                raise ToolError(
                    code="DELINEATION_FAILED",
                    message=hybrid_reason,
                    tool=_TOOL_PATH,
                    recovery="Run merit_ensure_basin to stage MERIT-Basins data, or nudge the outlet onto the main channel.",
                )
            raise ToolError(
                code="DELINEATION_FAILED",
                message=(
                    f"All tiers failed. Fast: {escalation_reason or 'skipped'}. "
                    f"MERIT-Basins: {hybrid_reason}"
                ),
                tool=_TOOL_PATH,
                recovery="Run merit_ensure_basin or nudge the outlet onto the main channel.",
            )

    if gdf is None or gdf.empty or area < _MIN_AREA_KM2:
        raise ToolError(
            code="DELINEATION_FAILED",
            message="No valid watershed polygon produced. Try moving the outlet slightly.",
            tool=_TOOL_PATH,
        )

    geojson = _gdf_to_geojson(gdf)
    # Surface the headline delineation stats on the Feature itself so the map's
    # click popup (FeatureIdentifier reads feature.properties) shows area/method
    # etc. — same UX as the MERIT-river popups. Keep the 6 most useful first;
    # the popup caps at 6 and rolls the rest into "+N more properties". Skip
    # None/empty values so the popup stays clean.
    _feature_props = {
        "area_km2": round(area, 1) if area is not None else None,
        "method": method_used,
        "pfaf": pfaf_code,
        "routing": routing_dataset,
        "snap_quality": snap_quality,
        "snap_distance_m": (
            round(snap_distance_m, 1) if snap_distance_m is not None else None
        ),
        "outlet": f"{lat:.5f}, {lon:.5f}",
        "name": name,
    }
    # Put curated stats FIRST so they win the popup's 6-property cap, then keep
    # any geometry-derived columns after them.
    _existing_props = geojson.get("properties") or {}
    geojson["properties"] = {
        **{k: v for k, v in _feature_props.items() if v not in (None, "")},
        **_existing_props,
    }
    if method_used in ("merit_gee_pyflwdir", "local_merit_pyflwdir"):
        sources = _SOURCES_MERIT_GEE
    elif method_used in ("merit_basins", "merit_basins_hybrid"):
        sources = _SOURCES_MERIT
    else:
        sources = _SOURCES_FAST

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
            "routing_dataset": routing_dataset,
        },
        sources=sources,
    )

    return HydroResult(
        data={
            "geometry_geojson": geojson,
            "area_km2": round(area, 3),
            "polygon_area_km2": round(polygon_area_km2 if polygon_area_km2 is not None else area, 3),
            "outlet_lat": lat,
            "outlet_lon": lon,
            "method_used": method_used,
            "pfaf_code": pfaf_code,
            "pfaf_region": pfaf_code,
            "snap_distance_m": snap_distance_m,
            "snapped_upa_km2": snapped_upa_km2,
            "official_merit_upa_km2": official_merit_upa_km2,
            "local_upstream_area_km2": local_upstream_area_km2,
            "snap_quality": snap_quality,
            "snap_source": snap_source,
            "snap_validation": snap_validation,
            "routing_dataset": routing_dataset,
            "routing_data_source": routing_data_source,
            "vector_dataset_version": vector_dataset_version,
            "raster_dataset_version": raster_dataset_version,
            "vector_data_source": vector_data_source,
            "raster_data_source": raster_data_source,
            "routing_resolution_m": routing_resolution_m,
            "area_validation": area_validation,
            "validation_sources": validation_sources,
            "regional_flowdir_cached": regional_flowdir_cached,
            "execution_mode": execution_mode,
            "regional_flowdir_file_size_bytes": regional_flowdir_file_size_bytes,
            "window_expansion_iterations": window_expansion_iterations,
            "final_window_bounds": final_window_bounds,
            "final_window_cell_count": final_window_cell_count,
            "basin_touched_window_boundary": basin_touched_window_boundary,
            "window_complete": window_complete,
            "peak_memory_mb": peak_memory_mb,
            "runtime_seconds": runtime_seconds,
            "memory_telemetry": memory_telemetry,
            "safe_envelope_version": safe_envelope_version,
            "terminal_catchment_id": terminal_catchment_id,
            "upstream_catchment_count": upstream_catchment_count,
            "terminal_refinement_used": terminal_refinement_used,
            "vector_assembly_area_km2": (
                round(vector_assembly_area_km2, 3)
                if vector_assembly_area_km2 is not None
                else None
            ),
            "refined_polygon_area_km2": (
                round(refined_polygon_area_km2, 3)
                if refined_polygon_area_km2 is not None
                else None
            ),
            "fallback_history": fallback_history,
            "quality_flags": quality_flags,
            "cache_key": cache_key,
            "license": license_note,
            "citation": citation,
            "workflow_steps": _workflow_steps(
                method_used,
                routing_dataset=routing_dataset,
                expected_area_km2=expected_area_km2,
                escalation_reason=escalation_reason,
            ),
            "escalation_reason": escalation_reason,
            "name": name or "watershed",
        },
        meta=meta,
    )
