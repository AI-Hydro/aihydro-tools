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
    assert out["reference_label"] == "Synthetic GFM-like fixture"
    assert out["geojson"]["features"]


def test_bench_gfm_hindcast_has_csi():
    m = bench_gfm_hindcast_validation()
    assert m["reference_label"] == "Synthetic GFM-like fixture"
    assert 0.0 <= m["csi"] <= 1.0
    assert "skill_tier" in m


def test_legacy_backend_fixture_rejected(monkeypatch):
    import aihydro_data.flood.gfm as backend
    import pytest

    from ai_hydro.analysis.inundation_gfm import fetch_gfm_extent
    monkeypatch.setattr(backend, "fetch_gfm_extent", lambda *a, **kw: {
        "live": False, "source": "gfm_fixture_fallback", "geojson": {}})
    with pytest.raises(RuntimeError, match="non-observational"):
        fetch_gfm_extent([0, 0, 1, 1], "2024-01-01")


def test_extent_without_validity_support_has_no_skill_metrics():
    from ai_hydro.analysis.inundation_gfm import gfm_validation_readiness
    result = gfm_validation_readiness({"live": True, "status": "no_observations"})
    assert result["status"] == "not_assessed"
    assert not ({"csi", "pod", "far", "skill_tier"} & result.keys())


def test_missing_data_package_does_not_generate_reference(monkeypatch):
    import builtins

    import pytest

    from ai_hydro.analysis.inundation_gfm import fetch_gfm_extent
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "aihydro_data.flood.gfm":
            raise ImportError("fixture missing optional dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(RuntimeError, match="require aihydro-data"):
        fetch_gfm_extent([0, 0, 1, 1], "2024-01-01")
    fixture = fetch_gfm_extent([0, 0, 1, 1], "2024-01-01", use_fixture=True)
    assert fixture["synthetic"] and fixture["citation"] == ""
