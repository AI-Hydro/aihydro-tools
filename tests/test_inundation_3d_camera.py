"""Tests for Phase 4 camera path export."""
from __future__ import annotations

from ai_hydro.analysis.inundation_3d import bench_camera_path_contract, build_camera_path


def test_camera_path_length():
    path = build_camera_path([-68.0, 44.0, -67.5, 44.5], 4)
    assert len(path) == 4


def test_bench_camera_path():
    out = bench_camera_path_contract()
    assert out["n_keyframes"] == 5
    assert out["monotonic_lon"] is True
