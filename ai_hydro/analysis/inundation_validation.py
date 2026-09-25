"""
Flood inundation validation, exposure, and UX summary helpers (Phase 1).

Contingency metrics (CSI, POD, FAR) for modeled vs observed extent masks.
Bench helpers support HRB tasks B-061–B-065 without network access.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from ai_hydro.analysis.inundation import INUNDATION_CAVEAT, INUNDATION_SCOPE

DEFAULT_POPULATION_DENSITY_PER_KM2 = 45.0
WORLDPOP_LICENSE_NOTE = (
    "Population estimate uses default density placeholder; "
    "WorldPop/HRSL zonal stats require Phase 2 raster fetch."
)

__all__ = [
    "contingency_metrics",
    "validate_extent_masks",
    "build_summary_card",
    "build_exposure_summary",
    "rasterize_geojson_to_mask",
    "validate_inundation_against_geojson",
    "bench_inundation_scope",
    "bench_contingency_perfect",
    "bench_contingency_partial",
    "bench_summary_card_synthetic",
    "bench_stage_lookup_monotonic",
    "bench_exposure_with_population",
]


def _as_bool_mask(arr: np.ndarray) -> np.ndarray:
    return np.asarray(arr, dtype=bool)


def _binary_support(values, validity, name):
    arr = np.ma.asarray(values)
    if arr.dtype.kind not in "biuf":
        raise ValueError(f"{name} must contain binary numeric values")
    raw = np.asarray(arr.data)
    valid = ~np.ma.getmaskarray(arr) & np.isfinite(raw)
    if validity is not None:
        if np.ma.getmaskarray(validity).any():
            raise ValueError(f"{name} validity cannot itself contain masked values")
        supplied = np.asarray(validity)
        if supplied.shape != raw.shape or supplied.dtype.kind != "b":
            raise ValueError(f"{name} validity must be a boolean array of matching shape")
        valid &= supplied
    if np.any(valid & ~np.isin(raw, [0, 1])):
        raise ValueError(f"{name} contains nonbinary codes; supply an explicit validity mask for nodata")
    return raw == 1, valid


def contingency_metrics(
    model_mask: np.ndarray,
    reference_mask: np.ndarray,
    *,
    model_valid_mask: np.ndarray | None = None,
    reference_valid_mask: np.ndarray | None = None,
) -> dict[str, Any]:
    """Cell-count ratios on the jointly valid domain of caller-aligned grids."""
    model, model_valid = _binary_support(model_mask, model_valid_mask, "model")
    ref, ref_valid = _binary_support(reference_mask, reference_valid_mask, "reference")
    if model.shape != ref.shape:
        raise ValueError(f"Mask shape mismatch: model {model.shape} vs reference {ref.shape}")
    joint = model_valid & ref_valid
    hits = int((joint & model & ref).sum())
    misses = int((joint & ~model & ref).sum())
    false_alarms = int((joint & model & ~ref).sum())
    correct_negatives = int((joint & ~model & ~ref).sum())
    numerators = {"csi": hits, "pod": hits, "far": false_alarms, "bias": hits + false_alarms}
    denominators = {"csi": hits + misses + false_alarms, "pod": hits + misses,
                    "far": hits + false_alarms, "bias": hits + misses}
    return {
        "hits": hits, "misses": misses, "false_alarms": false_alarms,
        "correct_negatives": correct_negatives,
        **{key: numerators[key] / count if count else None for key, count in denominators.items()},
        "undefined_metrics": {key: "zero_denominator" for key, count in denominators.items() if not count},
        "valid_cells": int(joint.sum()), "excluded_cells": int(joint.size - joint.sum()),
        "total_cells": int(joint.size),
        "status": "computed" if joint.any() else "not_assessed",
        "support_basis": "caller_aligned_grid; cell_counts; finite_binary_joint_support",
    }


def validate_extent_masks(
    model_mask: np.ndarray,
    reference_mask: np.ndarray,
    *,
    reference_label: str = "observed",
    model_valid_mask: np.ndarray | None = None,
    reference_valid_mask: np.ndarray | None = None,
) -> dict[str, Any]:
    """Report overlap without universal skill thresholds or undefined perfect scores."""
    metrics = contingency_metrics(model_mask, reference_mask,
                                  model_valid_mask=model_valid_mask,
                                  reference_valid_mask=reference_valid_mask)
    def display(key):
        return "undefined" if metrics[key] is None else f"{metrics[key]:.2f}"
    return {**metrics, "reference_label": reference_label, "skill_tier": "not_assessed",
            "interpretation": f"CSI={display('csi')} vs {reference_label} "
                              f"(POD={display('pod')}, FAR={display('far')}); "
                              f"{metrics['valid_cells']} jointly valid cells. "
                              "Scientific adequacy requires study-specific acceptance criteria."}


def build_summary_card(inundation_data: dict[str, Any]) -> dict[str, Any]:
    """Structured card for map panel / agent narration."""
    scope = inundation_data.get("scope") or {}
    return {
        "title": "Flood inundation (HAND + rating curve)",
        "discharge_m3s": inundation_data.get("discharge_m3s"),
        "stage_likely_m": inundation_data.get("stage_likely_m"),
        "area_km2": {
            "low": inundation_data.get("area_km2_low"),
            "likely": inundation_data.get("area_km2_likely"),
            "high": inundation_data.get("area_km2_high"),
        },
        "max_depth_likely_m": inundation_data.get("max_depth_likely_m"),
        "caveat": inundation_data.get("caveat") or INUNDATION_CAVEAT,
        "scope": {
            "flood_type": scope.get("flood_type"),
            "hand_variant": scope.get("hand_variant"),
            "dem_resolution_m": scope.get("dem_resolution_m"),
        },
        "stage_lookup": inundation_data.get("stage_lookup"),
    }


def build_exposure_summary(
    inundated_mask: np.ndarray,
    *,
    cell_size_m: float,
    bounds: list[float] | None = None,
    population_raster: np.ndarray | None = None,
    population_density_per_km2: float | None = None,
) -> dict[str, Any]:
    """
    Zonal exposure summary from inundated cells.

    When ``population_raster`` is aligned to the mask, sums exposed population.
    Otherwise uses ``population_density_per_km2`` (default placeholder) × area.
    """
    mask = _as_bool_mask(inundated_mask)
    n_cells = int(mask.sum())
    cell_area_m2 = float(cell_size_m) ** 2
    area_km2 = n_cells * cell_area_m2 / 1e6

    pop_exposed: float | None = None
    pop_method = None
    data_gaps: list[str] = []

    if population_raster is not None:
        pop_arr = np.asarray(population_raster, dtype=np.float64)
        if pop_arr.shape == mask.shape:
            pop_exposed = float(np.nansum(pop_arr[mask]))
            pop_method = "zonal_sum"
        else:
            data_gaps.append("population_raster_shape_mismatch")
    else:
        density = (
            float(population_density_per_km2)
            if population_density_per_km2 is not None
            else DEFAULT_POPULATION_DENSITY_PER_KM2
        )
        pop_exposed = round(area_km2 * density, 1)
        pop_method = "density_placeholder"
        data_gaps.extend(["buildings", "roads"])

    out: dict[str, Any] = {
        "inundated_cells": n_cells,
        "area_km2": round(area_km2, 4),
        "cell_size_m": float(cell_size_m),
        "population_exposed": pop_exposed,
        "population_method": pop_method,
        "population_density_per_km2": population_density_per_km2,
        "population_license_note": WORLDPOP_LICENSE_NOTE if pop_method == "density_placeholder" else None,
        "buildings_exposed": None,
        "roads_km_exposed": None,
        "data_gaps": data_gaps,
    }
    if bounds and len(bounds) >= 4:
        out["bounds"] = bounds
    return out


def rasterize_geojson_to_mask(
    geojson: dict[str, Any],
    *,
    out_shape: tuple[int, int],
    transform,
    all_touched: bool = True,
) -> np.ndarray:
    """Burn GeoJSON polygons into a boolean mask aligned to a raster grid."""
    from rasterio.features import rasterize
    from rasterio.transform import Affine
    from shapely.geometry import shape

    if hasattr(transform, "a"):
        affine = transform
    else:
        affine = Affine(*transform[:6])

    geoms = []
    gtype = geojson.get("type")
    if gtype == "FeatureCollection":
        for feat in geojson.get("features") or []:
            if feat.get("geometry"):
                geoms.append(shape(feat["geometry"]))
    elif gtype == "Feature":
        geoms.append(shape(geojson["geometry"]))
    elif gtype in ("Polygon", "MultiPolygon"):
        geoms.append(shape(geojson))

    if not geoms:
        return np.zeros(out_shape, dtype=bool)

    burned = rasterize(
        [(g, 1) for g in geoms],
        out_shape=out_shape,
        transform=affine,
        fill=0,
        dtype=np.uint8,
        all_touched=all_touched,
    )
    return burned.astype(bool)


def validate_inundation_against_geojson(
    model_mask: np.ndarray,
    *,
    transform,
    reference_geojson: dict[str, Any],
    reference_label: str = "observed",
    reference_valid_mask: np.ndarray | None = None,
    model_valid_mask: np.ndarray | None = None,
    model_crs: str | None = None,
) -> dict[str, Any]:
    """Score WGS84 extent only with explicitly supplied observation support."""
    if reference_valid_mask is None or model_crs is None:
        return {"status": "not_assessed", "reference_label": reference_label,
                "reason": "Observation validity footprint and model CRS must be supplied; polygons alone do not establish observed dry cells."}
    from pyproj import CRS
    if CRS.from_user_input(model_crs) != CRS.from_epsg(4326) or reference_geojson.get("crs"):
        raise ValueError("Reference must be WGS84 GeoJSON without a legacy CRS; reproject the comparison grid to EPSG:4326 explicitly")
    from rasterio.transform import Affine
    affine = transform if isinstance(transform, Affine) else Affine(*transform[:6])
    if not np.isfinite(tuple(affine)).all() or affine.determinant == 0:
        raise ValueError("Comparison transform must be finite and invertible")
    if np.ndim(model_mask) != 2 or not all(model_mask.shape):
        raise ValueError("GeoJSON comparison requires a nonempty two-dimensional grid")
    if reference_geojson.get("type") not in {"FeatureCollection", "Feature", "Polygon", "MultiPolygon"}:
        raise ValueError("Reference must contain GeoJSON polygon features")
    ref_mask = rasterize_geojson_to_mask(reference_geojson, out_shape=model_mask.shape,
                                       transform=transform, all_touched=False)
    result = validate_extent_masks(model_mask, ref_mask, reference_label=reference_label,
                                   model_valid_mask=model_valid_mask,
                                   reference_valid_mask=reference_valid_mask)
    result["rasterization"] = "pixel_center"
    result["model_crs"] = "EPSG:4326"
    return result


# ---------------------------------------------------------------------------
# Bench helpers (HRB B-061–B-065)
# ---------------------------------------------------------------------------


def bench_inundation_scope() -> dict[str, Any]:
    """Return canonical scope metadata for bench assertions."""
    return dict(INUNDATION_SCOPE)


def bench_contingency_perfect() -> dict[str, float | int]:
    """Identical masks → perfect skill scores."""
    mask = np.array([[1, 1, 0], [1, 0, 0], [0, 0, 0]], dtype=bool)
    return contingency_metrics(mask, mask)


def bench_contingency_partial() -> dict[str, float | int]:
    """Known partial overlap: 3 hits, 2 misses, 2 false alarms."""
    model = np.array(
        [
            [1, 1, 0, 0],
            [1, 0, 0, 0],
            [1, 1, 0, 0],
            [0, 0, 0, 0],
        ],
        dtype=bool,
    )
    ref = np.array(
        [
            [1, 1, 1, 0],
            [1, 1, 0, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 0],
        ],
        dtype=bool,
    )
    return contingency_metrics(model, ref)


def bench_summary_card_synthetic() -> dict[str, Any]:
    """Summary card from minimal inundation-like payload."""
    return build_summary_card(
        {
            "discharge_m3s": 250.0,
            "stage_likely_m": 1.8,
            "area_km2_low": 0.5,
            "area_km2_likely": 1.2,
            "area_km2_high": 2.1,
            "max_depth_likely_m": 3.5,
            "caveat": INUNDATION_CAVEAT,
            "scope": INUNDATION_SCOPE,
            "stage_lookup": {0.0: 0, 1.0: 12, 2.0: 28},
        }
    )


def bench_stage_lookup_monotonic() -> dict[str, Any]:
    """Run synthetic HAND spike and verify monotonic stage lookup."""
    from ai_hydro.analysis.inundation_spike import run_synthetic_hand_spike

    spike = run_synthetic_hand_spike()
    lookup = spike.get("stage_lookup") or {}
    counts = list(lookup.values())
    monotonic = all(counts[i] <= counts[i + 1] for i in range(len(counts) - 1))
    return {
        "monotonic": monotonic,
        "n_stages": len(counts),
        "inundated_cells_2m": spike.get("inundated_cells_2m"),
    }


def bench_exposure_with_population() -> dict[str, Any]:
    """Exposure summary includes population placeholder estimate."""
    mask = np.array([[1, 1], [0, 0]], dtype=bool)
    exp = build_exposure_summary(mask, cell_size_m=1000.0, population_density_per_km2=100.0)
    return exp
