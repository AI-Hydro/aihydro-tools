"""Unit tests for inundation core (no network)."""
from __future__ import annotations

import numpy as np
import pytest

from ai_hydro.analysis.inundation import (
    INUNDATION_CAVEAT,
    manning_stage_rectangular,
)
from ai_hydro.analysis.inundation_spike import depth_at_stage


def test_manning_stage_increases_with_discharge():
    s1 = manning_stage_rectangular(10.0, width_m=20.0, slope=0.01, manning_n=0.035)
    s2 = manning_stage_rectangular(100.0, width_m=20.0, slope=0.01, manning_n=0.035)
    assert s2 > s1 > 0


def test_manning_n_high_gives_higher_stage_than_n_low():
    q = 50.0
    stage_rough = manning_stage_rectangular(q, width_m=15.0, slope=0.005, manning_n=0.05)
    stage_smooth = manning_stage_rectangular(q, width_m=15.0, slope=0.005, manning_n=0.03)
    assert stage_rough > stage_smooth > 0


def test_depth_at_stage_monotonic_with_hand():
    hand = np.array([[5.0, 3.0], [2.0, 1.0]], dtype=np.float32)
    _, m1 = depth_at_stage(hand, 1.5)
    _, m2 = depth_at_stage(hand, 3.0)
    assert int(m2.sum()) >= int(m1.sum())


def test_caveat_present():
    assert "life-safety" in INUNDATION_CAVEAT.lower()
