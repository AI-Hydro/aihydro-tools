"""
Hydrograph-driven inundation frames for map time-slider animation (Phase 2).

Builds one HAND stack, then evaluates likely-band depth/extent at each
hydrograph timestep with ``time_start`` / ``time_end`` metadata for the map.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from ai_hydro.analysis.inundation import (
    DEFAULT_MANNING_N,
    _band_stats,
    _estimate_channel_geometry,
    manning_stage_rectangular,
    prepare_hand_stack,
)
from ai_hydro.analysis.inundation_spike import depth_at_stage

__all__ = [
    "InundationFrame",
    "subsample_hydrograph_indices",
    "compute_inundation_hydrograph_frames",
    "bench_hydrograph_frame_monotonic",
]


@dataclass
class InundationFrame:
    index: int
    time_hr: float
    discharge_m3s: float
    stage_m: float
    depth: np.ndarray
    inundated_mask: np.ndarray
    area_km2: float
    max_depth_m: float
    time_start: str
    time_end: str


def subsample_hydrograph_indices(n: int, max_frames: int) -> list[int]:
    """Pick <= max_frames indices, always retaining first, peak-Q, and last."""
    if n <= 0:
        return []
    if n <= max_frames:
        return list(range(n))
    peak_idx = 0
    return sorted(set(np.linspace(0, n - 1, max_frames, dtype=int).tolist() + [0, n - 1, peak_idx]))


def _parse_event_start(event_start_iso: str | None) -> datetime:
    if not event_start_iso:
        return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    raw = event_start_iso.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _frame_time_window(
    event_start: datetime,
    time_hr: list[float],
    index: int,
) -> tuple[str, str]:
    t0_h = float(time_hr[index])
    start = event_start + timedelta(hours=t0_h)
    if index + 1 < len(time_hr):
        end = event_start + timedelta(hours=float(time_hr[index + 1]))
    else:
        end = start + timedelta(minutes=15)
    return start.isoformat(), end.isoformat()


def compute_inundation_hydrograph_frames(
    watershed_geom,
    time_hr: list[float],
    discharge_cms: list[float],
    *,
    event_start_iso: str | None = None,
    max_frames: int = 12,
    resolution: int = 30,
    manning_n: float = DEFAULT_MANNING_N,
    stream_acc_threshold: int = 100,
    hand_stack: dict[str, Any] | None = None,
) -> tuple[list[InundationFrame], dict[str, Any]]:
    """
    Return animation frames + HAND stack metadata.

    Subsamples the hydrograph to ``max_frames`` while preserving peak discharge.
    """
    if len(time_hr) != len(discharge_cms):
        raise ValueError("time_hr and discharge_cms must have the same length")
    if not time_hr:
        raise ValueError("Empty hydrograph")

    q_arr = np.asarray(discharge_cms, dtype=float)
    peak_idx = int(np.argmax(q_arr))
    n = len(time_hr)
    if n <= max_frames:
        indices = list(range(n))
    else:
        indices = sorted(
            set(np.linspace(0, n - 1, max(3, max_frames - 1), dtype=int).tolist())
            | {0, n - 1, peak_idx}
        )[:max_frames]

    stack = hand_stack or prepare_hand_stack(
        watershed_geom,
        resolution=resolution,
        stream_acc_threshold=stream_acc_threshold,
    )
    hand = stack["hand"]
    valid = np.isfinite(hand)
    width_m, slope, _ = _estimate_channel_geometry(
        stack["elev"],
        stack["acc"],
        cell_size_m=stack["cell_size_m"],
        stream_acc_threshold=stream_acc_threshold,
    )
    event_start = _parse_event_start(event_start_iso)

    frames: list[InundationFrame] = []
    for out_i, src_i in enumerate(indices):
        q = float(q_arr[src_i])
        if q <= 0:
            depth = np.zeros_like(hand, dtype=np.float32)
            mask = np.zeros_like(hand, dtype=bool)
            stage = 0.0
            area_km2, max_depth = 0.0, 0.0
        else:
            stage = manning_stage_rectangular(
                q, width_m=width_m, slope=slope, manning_n=float(manning_n),
            )
            depth, mask = depth_at_stage(hand, stage)
            depth = np.where(valid, depth, np.nan)
            mask = mask & valid
            area_km2, max_depth = _band_stats(depth, mask, stack["cell_size_m"])

        t_start, t_end = _frame_time_window(event_start, time_hr, src_i)
        frames.append(
            InundationFrame(
                index=out_i,
                time_hr=float(time_hr[src_i]),
                discharge_m3s=q,
                stage_m=float(stage),
                depth=depth,
                inundated_mask=mask,
                area_km2=area_km2,
                max_depth_m=max_depth,
                time_start=t_start,
                time_end=t_end,
            )
        )

    return frames, stack


def bench_hydrograph_frame_monotonic() -> dict[str, Any]:
    """Synthetic HAND stack + rising Q series → non-decreasing inundated area."""
    hand = np.array(
        [[5.0, 4.0, 3.0], [4.0, 3.0, 2.0], [3.0, 2.0, 1.0]],
        dtype=np.float32,
    )
    stack = {
        "hand": hand,
        "elev": hand + 10.0,
        "acc": np.array([[100, 80, 60], [90, 70, 50], [80, 60, 40]], dtype=float),
        "cell_size_m": 30.0,
        "bounds": [0.0, 0.0, 90.0, 90.0],
        "crs": "EPSG:5070",
    }
    time_hr = [0.0, 1.0, 2.0, 3.0, 4.0]
    discharge = [0.0, 20.0, 50.0, 100.0, 80.0]

    frames, _ = compute_inundation_hydrograph_frames(
        None,
        time_hr,
        discharge,
        max_frames=5,
        hand_stack=stack,
    )
    areas = [f.area_km2 for f in frames]
    peak_idx = int(np.argmax([f.discharge_m3s for f in frames]))
    monotonic_to_peak = all(areas[i] <= areas[i + 1] for i in range(peak_idx))
    return {
        "n_frames": len(frames),
        "peak_frame_index": peak_idx,
        "areas_km2": areas,
        "monotonic_to_peak": monotonic_to_peak,
    }
