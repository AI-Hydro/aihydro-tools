"""Phase 0 — HAND + stage lookup spike (RALP gate)."""
from __future__ import annotations

import pytest

pytest.importorskip("pyflwdir")
pytest.importorskip("pysheds")


def test_synthetic_hand_spike_produces_monotonic_lookup():
    from ai_hydro.analysis.inundation_spike import run_synthetic_hand_spike

    result = run_synthetic_hand_spike(stream_acc_threshold=5)
    assert result["hand_max_m"] > result["hand_min_m"]
    assert result["inundated_cells_2m"] > 0
    assert result["max_depth_2m"] >= 2.0
    assert result["lookup_monotonic"] is True
    counts = list(result["stage_lookup"].values())
    assert counts == sorted(counts)


def test_depth_at_stage_zero_is_empty_or_shallow():
    import numpy as np
    from ai_hydro.analysis.inundation_spike import depth_at_stage

    hand = np.array([[0.0, 1.0], [2.0, 3.0]], dtype=np.float32)
    depth, mask = depth_at_stage(hand, 0.0)
    assert not mask.any()
    assert float(depth.max()) == 0.0
