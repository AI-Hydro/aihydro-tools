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


def contingency_metrics(
    model_mask: np.ndarray,
    reference_mask: np.ndarray,
) -> dict[str, float | int]:
    """
    Binary contingency table for modeled vs reference inundation extent.

    Returns CSI (critical success index), POD (probability of detection),
    FAR (false alarm ratio), plus raw cell counts.
    """
    model = _as_bool_mask(model_mask)
    ref = _as_bool_mask(reference_mask)
    if model.shape != ref.shape:
        raise ValueError(
            f"Mask shape mismatch: model {model.shape} vs reference {ref.shape}"
        )

    hits = int(np.logical_and(model, ref).sum())
    misses = int(np.logical_and(~model, ref).sum())
    false_alarms = int(np.logical_and(model, ~ref).sum())
    correct_negatives = int(np.logical_and(~model, ~ref).sum())

    ref_positive = hits + misses
    model_positive = hits + false_alarms

    csi = hits / (hits + misses + false_alarms) if (hits + misses + false_alarms) else 1.0
    pod = hits / ref_positive if ref_positive else 1.0
    far = false_alarms / model_positive if model_positive else 0.0
    bias = model_positive / ref_positive if ref_positive else 1.0

    return {
        "hits": hits,
        "misses": misses,
        "false_alarms": false_alarms,
        "correct_negatives": correct_negatives,
        "csi": float(csi),
        "pod": float(pod),
        "far": float(far),
        "bias": float(bias),
    }


def validate_extent_masks(
    model_mask: np.ndarray,
    reference_mask: np.ndarray,
    *,
    reference_label: str = "observed",
) -> dict[str, Any]:
    """Wrap contingency metrics with interpretation for tool responses."""
    metrics = contingency_metrics(model_mask, reference_mask)
    csi = metrics["csi"]
    if csi >= 0.7:
        skill = "good"
    elif csi >= 0.4:
        skill = "moderate"
    else:
        skill = "poor"
    return {
        **metrics,
        "reference_label": reference_label,
        "skill_tier": skill,
        "interpretation": (
            f"CSI={csi:.2f} vs {reference_label} "
            f"(POD={metrics['pod']:.2f}, FAR={metrics['far']:.2f})"
        ),
    }


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
) -> dict[str, Any]:
    """Compare modeled extent mask to a reference GeoJSON polygon."""
    ref_mask = rasterize_geojson_to_mask(
        reference_geojson,
        out_shape=model_mask.shape,
        transform=transform,
    )
    return validate_extent_masks(model_mask, ref_mask, reference_label=reference_label)


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
