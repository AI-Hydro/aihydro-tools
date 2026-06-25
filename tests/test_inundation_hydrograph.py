"""Tests for hydrograph inundation animation frames."""
from __future__ import annotations

import numpy as np

from ai_hydro.analysis.inundation_hydrograph import (
    bench_hydrograph_frame_monotonic,
    compute_inundation_hydrograph_frames,
)


def _synthetic_stack():
    hand = np.array([[5.0, 4.0, 3.0], [4.0, 3.0, 2.0], [3.0, 2.0, 1.0]], dtype=np.float32)
    return {
        "hand": hand,
        "elev": hand + 10.0,
        "acc": np.array([[100, 80, 60], [90, 70, 50], [80, 60, 40]], dtype=float),
        "cell_size_m": 30.0,
        "bounds": [0.0, 0.0, 90.0, 90.0],
        "crs": "EPSG:5070",
    }


def test_hydrograph_frames_have_time_metadata():
    frames, _ = compute_inundation_hydrograph_frames(
        None,
        [0.0, 1.0, 2.0],
        [0.0, 50.0, 100.0],
        event_start_iso="2023-07-15T00:00:00+00:00",
        max_frames=3,
        hand_stack=_synthetic_stack(),
    )
    assert len(frames) == 3
    assert frames[0].time_start.startswith("2023-07-15")
    assert frames[-1].discharge_m3s == 100.0


def test_hydrograph_area_rises_to_peak():
    out = bench_hydrograph_frame_monotonic()
    assert out["monotonic_to_peak"] is True
    assert out["n_frames"] >= 3
