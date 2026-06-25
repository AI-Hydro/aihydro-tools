"""Tests for GFM hindcast reference helpers."""
from __future__ import annotations

from ai_hydro.analysis.inundation_gfm import (
    bench_gfm_hindcast_validation,
    fixture_gfm_extent_geojson,
    resolve_gfm_reference,
)


def test_fixture_gfm_geojson_has_polygon():
    gj = fixture_gfm_extent_geojson([-72.0, 44.0, -71.0, 45.0], "2023-07-15")
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) == 1
    assert gj["features"][0]["geometry"]["type"] == "Polygon"


def test_resolve_gfm_reference_uses_fixture():
    out = resolve_gfm_reference([-72.0, 44.0, -71.0, 45.0], "2023-07-15", use_fixture=True)
    assert out["live"] is False
    assert out["reference_label"] == "GFM"
    assert out["geojson"]["features"]


def test_bench_gfm_hindcast_has_csi():
    m = bench_gfm_hindcast_validation()
    assert m["reference_label"] == "GFM"
    assert 0.0 <= m["csi"] <= 1.0
    assert "skill_tier" in m
