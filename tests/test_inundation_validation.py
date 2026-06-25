"""Tests for inundation validation metrics and summary helpers."""
from __future__ import annotations

import numpy as np

from ai_hydro.analysis.inundation_validation import (
    bench_contingency_partial,
    bench_contingency_perfect,
    bench_stage_lookup_monotonic,
    build_exposure_summary,
    build_summary_card,
    contingency_metrics,
    validate_extent_masks,
)


def test_contingency_perfect_overlap():
    mask = np.ones((4, 4), dtype=bool)
    m = contingency_metrics(mask, mask)
    assert m["csi"] == 1.0
    assert m["pod"] == 1.0
    assert m["far"] == 0.0


def test_contingency_partial_known_values():
    m = bench_contingency_partial()
    assert m["hits"] == 3
    assert m["misses"] == 2
    assert m["false_alarms"] == 2
    assert abs(m["csi"] - 3 / 7) < 1e-9
    assert m["pod"] == 0.6
    assert m["far"] == 0.4


def test_validate_extent_masks_skill_tier():
    perfect = bench_contingency_perfect()
    model = np.array([[1, 1], [1, 0]], dtype=bool)
    v = validate_extent_masks(model, model, reference_label="GFM")
    assert v["skill_tier"] == "good"
    assert "GFM" in v["interpretation"]


def test_summary_card_has_caveat_and_bands():
    card = build_summary_card(
        {
            "discharge_m3s": 100.0,
            "stage_likely_m": 2.0,
            "area_km2_low": 1.0,
            "area_km2_likely": 2.0,
            "area_km2_high": 3.0,
            "max_depth_likely_m": 4.0,
            "caveat": "test caveat",
            "scope": {"flood_type": "fluvial", "hand_variant": "single_source"},
        }
    )
    assert card["caveat"] == "test caveat"
    assert card["area_km2"]["likely"] == 2.0


def test_exposure_summary_area():
    mask = np.array([[1, 1], [0, 0]], dtype=bool)
    exp = build_exposure_summary(mask, cell_size_m=30.0)
    assert exp["inundated_cells"] == 2
    assert exp["population_exposed"] is not None
    assert exp["population_method"] == "density_placeholder"
    assert abs(exp["area_km2"] - 2 * 900 / 1e6) < 1e-6


def test_bench_stage_lookup_monotonic():
    out = bench_stage_lookup_monotonic()
    assert out["monotonic"] is True
    assert out["n_stages"] >= 2
