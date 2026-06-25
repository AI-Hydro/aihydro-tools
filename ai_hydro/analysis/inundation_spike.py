"""
HAND inundation spike utilities — Phase 0 proof of pyflwdir HAND + stage lookup.

These functions will grow into ``ai_hydro/analysis/inundation.py`` (Phase 1).
"""
from __future__ import annotations

from typing import Any

import numpy as np

__all__ = [
    "pysheds_flowdir_to_pyflwdir",
    "compute_hand_from_pysheds",
    "depth_at_stage",
    "build_stage_extent_lookup",
    "run_synthetic_hand_spike",
]


def pysheds_flowdir_to_pyflwdir(fdir: np.ndarray, *, transform=None):
    """Convert pysheds D8 flow direction to a pyflwdir FlwdirRaster."""
    import pyflwdir

    fdir_u8 = np.asarray(fdir, dtype=np.uint8)
    kwargs: dict[str, Any] = {"ftype": "d8", "check_ftype": False}
    if transform is not None:
        kwargs["transform"] = transform
    return pyflwdir.from_array(fdir_u8, **kwargs)


def compute_hand_from_pysheds(
    elev: np.ndarray,
    fdir: np.ndarray,
    acc: np.ndarray,
    *,
    stream_acc_threshold: int = 5,
    transform=None,
) -> np.ndarray:
    """Compute HAND (m) from conditioned elevation, flow direction, and accumulation."""
    elev_f = np.asarray(elev, dtype=np.float32)
    acc_a = np.asarray(acc)
    drain = acc_a >= int(stream_acc_threshold)
    flw = pysheds_flowdir_to_pyflwdir(fdir, transform=transform)
    hand = flw.hand(drain, elev_f)
    return np.asarray(hand, dtype=np.float32)


def depth_at_stage(hand: np.ndarray, stage_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (depth raster, inundated boolean mask) for a uniform stage (m)."""
    hand_f = np.asarray(hand, dtype=np.float32)
    depth = np.maximum(float(stage_m) - hand_f, 0.0)
    inundated = depth > 0
    return depth, inundated


def build_stage_extent_lookup(
    hand: np.ndarray,
    stages_m: np.ndarray | None = None,
    *,
    n_steps: int = 11,
) -> dict[float, int]:
    """Map stage (m) -> count of inundated cells (monotonic extent proxy)."""
    hand_f = np.asarray(hand, dtype=np.float32)
    valid = np.isfinite(hand_f)
    if not np.any(valid):
        return {}
    if stages_m is None:
        max_hand = float(np.nanmax(hand_f))
        stages_m = np.linspace(0.0, max_hand, max(2, n_steps))
    lookup: dict[float, int] = {}
    prev = -1
    for s in stages_m:
        count = int((hand_f <= float(s)).sum())
        if count < prev:
            raise ValueError(f"Non-monotonic extent at stage {s}m: {count} < {prev}")
        prev = count
        lookup[round(float(s), 4)] = count
    return lookup


def run_synthetic_hand_spike(*, stream_acc_threshold: int = 5) -> dict[str, Any]:
    """
    End-to-end spike on a tiny synthetic DEM (no network).

    Returns HAND stats, sample depths, and stage lookup for bench/smoke tests.
    """
    # pysheds 0.5 still calls np.in1d, which was removed in NumPy 2.4+.
    # Alias locally before importing pysheds so this offline benchmark remains
    # installable on current Python/NumPy without pinning the whole ecosystem to
    # older NumPy wheels.
    if not hasattr(np, "in1d"):
        np.in1d = np.isin  # type: ignore[attr-defined]

    from pysheds.grid import Grid
    import rasterio
    from rasterio.transform import from_origin
    import tempfile
    import os

    dem = np.linspace(20, 5, 100, dtype=np.float32).reshape(10, 10)
    dem[:, 4:6] -= 3.0

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        path = tmp.name
    transform = from_origin(0, 100, 10, 10)
    try:
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=10,
            width=10,
            count=1,
            dtype="float32",
            crs="EPSG:5070",
            transform=transform,
            nodata=-9999.0,
        ) as dst:
            dst.write(dem, 1)

        grid = Grid.from_raster(path)
        d = grid.read_raster(path)
        for step in (grid.fill_pits, grid.fill_depressions, grid.resolve_flats):
            d = step(d)
        elev = np.asarray(d, dtype=np.float32)
        fdir = grid.flowdir(d)
        acc = grid.accumulation(fdir)

        hand = compute_hand_from_pysheds(
            elev,
            fdir,
            acc,
            stream_acc_threshold=stream_acc_threshold,
            transform=grid.affine,
        )
        lookup = build_stage_extent_lookup(hand)
        depth_2m, mask_2m = depth_at_stage(hand, 2.0)

        return {
            "hand_min_m": float(np.nanmin(hand)),
            "hand_max_m": float(np.nanmax(hand)),
            "inundated_cells_2m": int(mask_2m.sum()),
            "max_depth_2m": float(depth_2m.max()),
            "stage_lookup": lookup,
            "lookup_monotonic": True,
        }
    finally:
        if os.path.exists(path):
            os.unlink(path)
