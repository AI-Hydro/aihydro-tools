"""Tests for inundation exposure / WorldPop zonal helpers."""
from __future__ import annotations

import numpy as np

from ai_hydro.analysis.inundation_exposure import (
    bench_zonal_population,
    enrich_exposure_summary,
    zonal_population_from_raster,
)


def test_zonal_population_sum():
    mask = np.array([[1, 1], [0, 0]], dtype=bool)
    pop = np.array([[10.0, 20.0], [5.0, 100.0]], dtype=float)
    assert zonal_population_from_raster(mask, pop) == 30.0


def test_enrich_with_zonal_raster():
    mask = np.array([[1, 0], [1, 0]], dtype=bool)
    pop = np.array([[5.0, 1.0], [7.0, 2.0]], dtype=float)
    base = {"area_km2": 1.0, "data_gaps": ["population"]}
    out = enrich_exposure_summary(base, mask, population_raster=pop)
    assert out["population_exposed"] == 12.0
    assert out["population_method"] == "zonal_sum"
    assert "population" not in out["data_gaps"]


def test_bench_zonal_population():
    out = bench_zonal_population()
    assert out["population_exposed"] == 30.0
