"""Tests for Phase 3 inundation physics validation."""
from __future__ import annotations

import numpy as np

from ai_hydro.analysis.inundation_physics import (
    bench_physics_backend_check,
    bench_physics_benchmark_identity,
    bench_physics_synthetic_job,
    benchmark_inundation_methods,
    check_physics_backend,
    execute_physics_validation,
    proxy_physics_mask,
)


def test_benchmark_identity_perfect_csi():
    out = bench_physics_benchmark_identity()
    assert out["csi"] == 1.0
    assert out["pod"] == 1.0
    assert out["far"] == 0.0


def test_proxy_mask_expands_extent():
    hand = np.zeros((5, 5), dtype=bool)
    hand[2, 2] = True
    proxy = proxy_physics_mask(hand, iterations=1)
    assert proxy.sum() > hand.sum()
    assert proxy[2, 2]


def test_synthetic_job_contract():
    report = bench_physics_synthetic_job()
    assert report["validation_tier"] == "physics"
    assert report["physics_method"] == "morphological_proxy"
    assert report["benchmark"] is not None
    assert 0.0 < report["csi"] <= 1.0
    assert report["scope"]["method_tier"] == "validate_physics"


def test_backend_check_shape():
    out = bench_physics_backend_check()
    assert out["engine"] == "sfincs"
    assert "available" in out


def test_execute_synthetic_mode():
    report = execute_physics_validation({"synthetic_mode": True, "discharge_m3s": 250.0})
    assert report["hand"]["discharge_m3s"] == 250.0
    assert "proxy_note" in report


def test_partial_benchmark_skill_tier():
    hand = np.array([[1, 1, 0], [1, 0, 0], [0, 0, 0]], dtype=bool)
    physics = proxy_physics_mask(hand, iterations=1)
    m = benchmark_inundation_methods(hand, physics, cell_size_m=30.0)
    assert 0.0 < m["csi"] <= 1.0
    assert m["hand_area_km2"] <= m["physics_area_km2"]


def test_check_physics_backend_unknown_engine():
    out = check_physics_backend("not_a_real_engine")
    assert out["available"] is False
