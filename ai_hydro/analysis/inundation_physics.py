"""
2D physics validation tier for flood inundation (Phase 3).

Provides backend availability checks, HAND vs physics benchmarking, and a
shared output contract for async validate-tier jobs. When SFINCS/HydroMT is
not installed, jobs complete with HAND baseline plus an explicit proxy
benchmark (morphological dilation) — never mislabeled as full physics.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from ai_hydro.analysis.inundation import INUNDATION_CAVEAT, INUNDATION_SCOPE
from ai_hydro.analysis.inundation_validation import validate_extent_masks

PHYSICS_SCOPE: dict[str, Any] = {
    "flood_type": "fluvial",
    "method_tier": "validate_physics",
    "hand_variant": INUNDATION_SCOPE.get("hand_variant", "single_source"),
    "physics_engines": ["sfincs", "lisflood_fp"],
    "proxy_when_unavailable": "morphological_dilation",
}

PHYSICS_CAVEAT = (
    "Physics-tier validation compares HAND extent to a 2D solver or an explicit "
    "morphological proxy when SFINCS/LISFLOOD-FP is unavailable. "
    "Proxy benchmarks are not operational flood maps."
)

__all__ = [
    "PHYSICS_SCOPE",
    "PHYSICS_CAVEAT",
    "check_physics_backend",
    "proxy_physics_mask",
    "benchmark_inundation_methods",
    "build_physics_benchmark_report",
    "execute_physics_validation",
    "compute_physics_validation_artifacts",
    "bench_physics_benchmark_identity",
    "bench_physics_backend_check",
    "bench_physics_synthetic_job",
]


def check_physics_backend(engine: str = "sfincs") -> dict[str, Any]:
    """Return availability and install hints for a 2D inundation engine."""
    eng = (engine or "sfincs").lower().replace("-", "_")

    if eng in ("sfincs", "hydromt_sfincs"):
        try:
            import sfincs  # noqa: F401

            return {
                "engine": "sfincs",
                "available": True,
                "package": "sfincs",
                "message": "SFINCS Python bindings detected.",
                "install_hint": None,
            }
        except ImportError:
            pass
        try:
            import hydromt_sfincs  # noqa: F401

            return {
                "engine": "sfincs",
                "available": True,
                "package": "hydromt_sfincs",
                "message": "HydroMT SFINCS plugin detected.",
                "install_hint": None,
            }
        except ImportError:
            return {
                "engine": "sfincs",
                "available": False,
                "package": None,
                "message": "SFINCS not installed.",
                "install_hint": "pip install sfincs hydromt-sfincs (requires SFINCS binary)",
            }

    if eng in ("lisflood_fp", "lisflood", "lisfloodfp"):
        try:
            import lisfloodfp  # noqa: F401

            return {
                "engine": "lisflood_fp",
                "available": True,
                "package": "lisfloodfp",
                "message": "LISFLOOD-FP bindings detected.",
                "install_hint": None,
            }
        except ImportError:
            return {
                "engine": "lisflood_fp",
                "available": False,
                "package": None,
                "message": "LISFLOOD-FP not installed.",
                "install_hint": "Install LISFLOOD-FP and Python bindings per project docs.",
            }

    return {
        "engine": eng,
        "available": False,
        "package": None,
        "message": f"Unknown physics engine: {engine!r}",
        "install_hint": "Use engine='sfincs' or engine='lisflood_fp'.",
    }


def proxy_physics_mask(hand_mask: np.ndarray, *, iterations: int = 2) -> np.ndarray:
    """
    Morphological dilation proxy when a 2D solver is unavailable.

    Explicitly labeled in benchmark reports — not a substitute for SFINCS.
    """
    mask = _as_bool_mask(hand_mask)
    out = mask.copy()
    for _ in range(max(int(iterations), 1)):
        padded = np.pad(out, 1, mode="constant", constant_values=False)
        dilated = np.zeros_like(out)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                dilated |= padded[1 + dy : 1 + dy + out.shape[0], 1 + dx : 1 + dx + out.shape[1]]
        out = dilated
    return out


def _as_bool_mask(arr: np.ndarray) -> np.ndarray:
    return np.asarray(arr, dtype=bool)


def _mask_area_km2(mask: np.ndarray, cell_size_m: float) -> float:
    return float(mask.sum()) * (float(cell_size_m) ** 2) / 1e6


def benchmark_inundation_methods(
    hand_mask: np.ndarray,
    physics_mask: np.ndarray,
    *,
    cell_size_m: float = 30.0,
    reference_label: str = "physics",
) -> dict[str, Any]:
    """Compare HAND likely extent to physics (or proxy) extent."""
    hand = _as_bool_mask(hand_mask)
    physics = _as_bool_mask(physics_mask)
    metrics = validate_extent_masks(hand, physics, reference_label=reference_label)
    return {
        **metrics,
        "hand_area_km2": _mask_area_km2(hand, cell_size_m),
        "physics_area_km2": _mask_area_km2(physics, cell_size_m),
        "cell_size_m": float(cell_size_m),
        "reference_label": reference_label,
    }


def build_physics_benchmark_report(
    *,
    hand_summary: dict[str, Any],
    physics_summary: dict[str, Any],
    benchmark: dict[str, Any] | None,
    backend: dict[str, Any],
) -> dict[str, Any]:
    """Shared job/tool output contract for validate-tier inundation."""
    physics_method = physics_summary.get("method", "unknown")
    report: dict[str, Any] = {
        "scope": PHYSICS_SCOPE,
        "caveat": PHYSICS_CAVEAT,
        "hand": hand_summary,
        "physics": physics_summary,
        "backend": backend,
        "benchmark": benchmark,
        "physics_method": physics_method,
        "validation_tier": "physics",
    }
    if benchmark:
        report["csi"] = benchmark.get("csi")
        report["skill_tier"] = benchmark.get("skill_tier")
        report["interpretation"] = benchmark.get("interpretation")
    if physics_method == "morphological_proxy":
        report["proxy_note"] = (
            "Physics engine unavailable; benchmark uses dilated HAND proxy only."
        )
    return report


def _synthetic_masks(size: int = 32) -> tuple[np.ndarray, np.ndarray]:
    hand = np.zeros((size, size), dtype=bool)
    hand[10:22, 8:24] = True
    physics = proxy_physics_mask(hand, iterations=2)
    return hand, physics


def _watershed_geom_from_session(session) -> Any:
    from shapely.geometry import shape

    watershed_geojson = session.watershed
    gtype = watershed_geojson.get("type") if isinstance(watershed_geojson, dict) else None
    if gtype == "FeatureCollection":
        return shape(watershed_geojson["features"][0]["geometry"])
    if gtype == "Feature":
        return shape(watershed_geojson["geometry"])
    return shape(watershed_geojson)


def _run_hand_from_session(cfg: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any], float]:
    from ai_hydro.analysis.inundation import compute_inundation
    from ai_hydro.analysis.inundation_drivers import resolve_inundation_discharge
    from ai_hydro.session import HydroSession

    session = HydroSession.load(cfg["session_id"])
    if session.watershed is None:
        raise ValueError("Session missing watershed — run delineate_watershed first.")

    q, discharge_source, discharge_err = resolve_inundation_discharge(
        session,
        discharge_m3s=cfg.get("discharge_m3s"),
        return_period=cfg.get("return_period"),
        use_design_peak=cfg.get("use_design_peak", False),
        use_session_peak=cfg.get("use_session_peak", False),
    )
    if discharge_err:
        msg = discharge_err.get("message") if isinstance(discharge_err, dict) else str(discharge_err)
        raise ValueError(msg or "Could not resolve discharge.")
    if q is None or float(q) <= 0:
        raise ValueError("Could not resolve positive discharge for physics validation.")

    watershed_geom = _watershed_geom_from_session(session)
    result = compute_inundation(watershed_geom, float(q))
    likely = result.bands.get("likely")
    if likely is None:
        raise ValueError("HAND computation returned no likely band.")

    hand_summary = {
        "method": "hand_src",
        "discharge_m3s": float(q),
        "discharge_source": discharge_source,
        "stage_likely_m": result.to_dict().get("stage_likely_m"),
        "area_km2_likely": likely.area_km2,
        "caveat": INUNDATION_CAVEAT,
        "scope": result.scope or INUNDATION_SCOPE,
    }
    return likely.inundated_mask, hand_summary, float(result.cell_size_m)


def _attempt_physics_run(
    engine: str,
    hand_mask: np.ndarray,
    cell_size_m: float,
    cfg: dict[str, Any],
) -> tuple[np.ndarray | None, dict[str, Any]]:
    backend = check_physics_backend(engine)
    if not backend["available"]:
        proxy = proxy_physics_mask(hand_mask)
        return proxy, {
            "method": "morphological_proxy",
            "status": "proxy",
            "area_km2": _mask_area_km2(proxy, cell_size_m),
            "reason": backend["message"],
            "install_hint": backend.get("install_hint"),
        }

    # Package present but automated mesh build from session DEM is Phase 3+ follow-up.
    proxy = proxy_physics_mask(hand_mask)
    return proxy, {
        "method": "morphological_proxy",
        "status": "engine_present_mesh_deferred",
        "area_km2": _mask_area_km2(proxy, cell_size_m),
        "reason": (
            "SFINCS/LISFLOOD-FP package detected but session→mesh automation is not "
            "wired yet; proxy benchmark supplied for validate-tier contract."
        ),
        "install_hint": None,
    }


def execute_physics_validation(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Core validate-tier logic (callable from subprocess runner and bench tests).

    synthetic_mode: offline masks, no DEM/network.
    Otherwise: HAND from session watershed + physics/proxy path.
    """
    (
        hand_mask,
        physics_mask,
        hand_summary,
        physics_summary,
        cell_size_m,
        backend,
    ) = compute_physics_validation_artifacts(cfg)

    benchmark = benchmark_inundation_methods(
        hand_mask,
        physics_mask,
        cell_size_m=cell_size_m,
        reference_label="physics",
    )
    return build_physics_benchmark_report(
        hand_summary=hand_summary,
        physics_summary=physics_summary,
        benchmark=benchmark,
        backend=backend,
    )


def compute_physics_validation_artifacts(
    cfg: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray, dict[str, Any], dict[str, Any], float, dict[str, Any]]:
    """Return HAND/physics masks and summaries for validate-tier jobs and surrogate export."""
    engine = cfg.get("engine", "sfincs")
    backend = check_physics_backend(engine)

    if cfg.get("synthetic_mode"):
        hand_mask, physics_mask = _synthetic_masks()
        cell_size_m = float(cfg.get("cell_size_m", 30.0))
        discharge = float(cfg.get("discharge_m3s", 500.0))
        hand_summary = {
            "method": "hand_src",
            "discharge_m3s": discharge,
            "discharge_source": "synthetic",
            "area_km2_likely": _mask_area_km2(hand_mask, cell_size_m),
            "scope": INUNDATION_SCOPE,
        }
        physics_summary = {
            "method": "morphological_proxy",
            "status": "proxy",
            "area_km2": _mask_area_km2(physics_mask, cell_size_m),
            "reason": "synthetic_mode bench fixture",
        }
        return hand_mask, physics_mask, hand_summary, physics_summary, cell_size_m, backend

    hand_mask, hand_summary, cell_size_m = _run_hand_from_session(cfg)
    physics_mask, physics_summary = _attempt_physics_run(
        engine, hand_mask, cell_size_m, cfg
    )
    return hand_mask, physics_mask, hand_summary, physics_summary, cell_size_m, backend


def bench_physics_benchmark_identity() -> dict[str, Any]:
    """Identical HAND vs HAND masks → perfect CSI (B-070)."""
    mask = np.array([[1, 1, 0], [1, 0, 0], [0, 0, 0]], dtype=bool)
    return benchmark_inundation_methods(mask, mask, cell_size_m=30.0)


def bench_physics_backend_check() -> dict[str, Any]:
    """Backend probe shape for bench (B-071)."""
    out = check_physics_backend("sfincs")
    return {
        "engine": out["engine"],
        "available": bool(out["available"]),
        "has_install_hint": out.get("install_hint") is not None or out["available"],
    }


def bench_physics_synthetic_job() -> dict[str, Any]:
    """Full validate-tier report in synthetic mode (B-072)."""
    return execute_physics_validation(
        {"synthetic_mode": True, "discharge_m3s": 800.0, "engine": "sfincs"}
    )
