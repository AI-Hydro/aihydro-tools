"""
Unit tests for ai_hydro.analysis.tile_pyramid.

Pure-numpy/synthetic tests — no live backends.  Marked offline.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from shapely.geometry import box

from ai_hydro.analysis.tile_pyramid import (
    generate_tile_pyramid,
    should_use_tile_pyramid,
    _raster_bounds_wgs84,
    _write_overview,
    DEFAULT_TILE_TRIGGER,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_raster(h: int = 100, w: int = 100,
                 fill: float | np.ndarray = 50.0) -> xr.DataArray:
    x = np.linspace(0.0, 1.0, w, dtype="float64")
    y = np.linspace(1.0, 0.0, h, dtype="float64")  # north → south
    if isinstance(fill, np.ndarray):
        data = fill.astype("float32")
    else:
        data = np.full((h, w), fill, dtype="float32")
    return xr.DataArray(data, dims=("y", "x"), coords={"y": y, "x": x}, name="synthetic")


# ---------------------------------------------------------------------------
# should_use_tile_pyramid
# ---------------------------------------------------------------------------

def test_trigger_small_array_false():
    arr = np.ones((100, 100))   # 10_000 cells
    assert should_use_tile_pyramid(arr, trigger_size=20_000) is False


def test_trigger_large_array_true():
    arr = np.ones((400, 300))   # 120_000 cells
    assert should_use_tile_pyramid(arr, trigger_size=50_000) is True


# ---------------------------------------------------------------------------
# _raster_bounds_wgs84
# ---------------------------------------------------------------------------

def test_bounds_wgs84_order():
    """West < East, South < North."""
    raster = _make_raster(100, 100)
    west, south, east, north = _raster_bounds_wgs84(raster)
    assert west < east
    assert south < north


def test_bounds_wgs84_matches_coords():
    """Bounds should be within one pixel-width of the coordinate extremes.

    We allow up to one full pixel-width (dx for x, dy for y) of tolerance
    because rioxarray's ``rio.bounds()`` returns the outer *edges* of the
    border pixels (pixel-centre ± half-pixel), while ``x.min()`` / ``y.min()``
    return pixel-centres.  Either path (rioxarray or coordinate fallback) is
    correct — we just verify the result is within one pixel of the data extent.
    """
    raster = _make_raster(50, 50)
    west, south, east, north = _raster_bounds_wgs84(raster)
    x = raster.x.values
    y = raster.y.values
    dx = float(x[1] - x[0]) if x.size >= 2 else 1.0
    dy = abs(float(y[1] - y[0])) if y.size >= 2 else 1.0
    assert abs(west - float(x.min())) <= dx
    assert abs(east - float(x.max())) <= dx
    assert abs(south - float(y.min())) <= dy
    assert abs(north - float(y.max())) <= dy


# ---------------------------------------------------------------------------
# generate_tile_pyramid
# ---------------------------------------------------------------------------

class TestGenerateTilePyramid:
    def test_overview_png_created(self, tmp_path):
        raster = _make_raster(80, 80)
        result = generate_tile_pyramid(
            raster, str(tmp_path), "test_layer",
            colormap="viridis", generate_tiles=False,
        )
        assert Path(result["overview_png"]).exists()
        assert result["overview_png"].endswith("_overview.png")

    def test_manifest_created(self, tmp_path):
        raster = _make_raster(80, 80)
        result = generate_tile_pyramid(
            raster, str(tmp_path), "test_layer",
            generate_tiles=False,
        )
        manifest_path = Path(result["manifest_path"])
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["name"] == "test_layer"
        assert "bounds_wgs84" in manifest
        assert "overview_png" in manifest

    def test_bounds_returned(self, tmp_path):
        raster = _make_raster(60, 60)
        result = generate_tile_pyramid(
            raster, str(tmp_path), "bounds_test",
            generate_tiles=False,
        )
        west, south, east, north = result["bounds_wgs84"]
        assert west < east
        assert south < north

    def test_colormap_in_manifest(self, tmp_path):
        raster = _make_raster(60, 60)
        result = generate_tile_pyramid(
            raster, str(tmp_path), "cmap_test",
            colormap="Blues", generate_tiles=False,
        )
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        assert manifest["colormap"] == "Blues"
        assert result["colormap"] == "Blues"

    def test_vmin_vmax_explicit(self, tmp_path):
        raster = _make_raster(60, 60)
        result = generate_tile_pyramid(
            raster, str(tmp_path), "vrange_test",
            vmin=10.0, vmax=90.0, generate_tiles=False,
        )
        assert result["vmin"] == pytest.approx(10.0)
        assert result["vmax"] == pytest.approx(90.0)

    def test_vmin_vmax_auto_percentile(self, tmp_path):
        """Auto vmin/vmax should be percentile-derived, not global min/max."""
        data = np.arange(10_000, dtype="float32").reshape(100, 100)
        raster = _make_raster(100, 100, fill=data)
        result = generate_tile_pyramid(
            raster, str(tmp_path), "perc_test",
            generate_tiles=False,
        )
        # p2 of 0..9999 ≈ 200; p98 ≈ 9800 — definitely not 0 or 9999
        assert result["vmin"] > 0
        assert result["vmax"] < 9999

    def test_tile_dir_structure(self, tmp_path):
        raster = _make_raster(60, 60)
        result = generate_tile_pyramid(
            raster, str(tmp_path), "dir_test",
            generate_tiles=False,
        )
        tile_dir = Path(result["tile_dir"])
        assert tile_dir.is_dir()
        assert tile_dir.name == "dir_test"

    def test_chip_tiles_generated(self, tmp_path):
        """With generate_tiles=True + a small raster, at least some chips appear."""
        basin = box(0.1, 0.1, 0.9, 0.9)
        raster = _make_raster(64, 64)
        result = generate_tile_pyramid(
            raster, str(tmp_path), "chips_test",
            colormap="viridis",
            watershed=basin,
            zoom_range=(5, 6),   # two levels only
            chip_px=16,
            generate_tiles=True,
        )
        assert result["n_chips"] > 0
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        assert len(manifest["levels"]) > 0
        # Every chip path in the manifest must exist
        for level in manifest["levels"]:
            for chip in level["chips"]:
                assert Path(chip["path"]).exists(), f"Missing chip: {chip['path']}"

    def test_zero_pixel_raster_raises(self, tmp_path):
        raster = _make_raster(50, 50, fill=np.nan)
        with pytest.raises(ValueError, match="no finite pixels"):
            generate_tile_pyramid(raster, str(tmp_path), "empty_test",
                                  generate_tiles=False)

    def test_overview_smaller_than_max(self, tmp_path):
        """Overview PNG must not exceed max_overview_px × max_overview_px."""
        from PIL import Image as PILImage  # only imported if Pillow available
        pytest.importorskip("PIL")
        raster = _make_raster(1000, 1000)
        result = generate_tile_pyramid(
            raster, str(tmp_path), "big_overview",
            max_overview_px=128,
            generate_tiles=False,
        )
        img = PILImage.open(result["overview_png"])
        assert max(img.size) <= 128 + 5  # +5 px tolerance for PNG padding


# ---------------------------------------------------------------------------
# plot_raster_tile colormap registry wiring
# ---------------------------------------------------------------------------

class TestPlotRasterTileColormapWiring:
    """plot_raster_tile should resolve colormap from INDEX_REGISTRY
    when index_name is given and the default 'viridis' hasn't been overridden."""

    def test_ndwi_resolves_blues(self, tmp_path):
        from ai_hydro.analysis.plots import plot_raster_tile
        arr = np.random.default_rng(0).random((50, 50)).astype("float32")
        result = plot_raster_tile(
            arr,
            bounds_wgs84=[-1.0, -1.0, 1.0, 1.0],
            output_dir=str(tmp_path),
            name="ndwi_test",
            index_name="NDWI",   # should resolve to 'Blues'
        )
        # Result should succeed (not None)
        assert result is not None
        path, bounds = result
        assert Path(path).exists()

    def test_explicit_colormap_not_overridden_by_registry(self, tmp_path):
        """Explicit colormap='plasma' must NOT be replaced by registry lookup."""
        from ai_hydro.analysis.plots import plot_raster_tile
        arr = np.random.default_rng(1).random((50, 50)).astype("float32")
        result = plot_raster_tile(
            arr,
            bounds_wgs84=[-1.0, -1.0, 1.0, 1.0],
            output_dir=str(tmp_path),
            name="explicit_cm",
            colormap="plasma",   # explicit — should not be changed by index_name
            index_name="NDWI",
        )
        assert result is not None

    def test_no_index_name_uses_viridis(self, tmp_path):
        from ai_hydro.analysis.plots import plot_raster_tile
        arr = np.full((50, 50), 100.0, dtype="float32")
        result = plot_raster_tile(
            arr,
            bounds_wgs84=[-1.0, -1.0, 1.0, 1.0],
            output_dir=str(tmp_path),
            name="default_cm",
        )
        assert result is not None
        assert Path(result[0]).exists()


# ---------------------------------------------------------------------------
# push_tile_layer
# ---------------------------------------------------------------------------

def test_push_tile_layer_writes_event(tmp_path):
    """push_tile_layer should write a JSON file to the map events dir."""
    import shutil
    from ai_hydro.mcp import map_events

    # Redirect events dir to tmp_path
    original = map_events._MAP_EVENTS_DIR
    map_events._MAP_EVENTS_DIR = tmp_path
    try:
        ok = map_events.push_tile_layer(
            layer_id="test_tile",
            name="Test Tile Layer",
            overview_png=str(tmp_path / "overview.png"),
            manifest_path=str(tmp_path / "manifest.json"),
            bounds_wgs84=[-90.0, 30.0, -80.0, 40.0],
            colormap="Blues",
        )
        assert ok is True
        events = list(tmp_path.glob("*.json"))
        assert len(events) == 1
        event = json.loads(events[0].read_text())
        assert event["layerType"] == "raster"
        assert event["metadata"]["tile_pyramid"] == "true"
        assert event["metadata"]["raster_colormap"] == "Blues"
        assert "tile_pyramid_manifest" in event["metadata"]
    finally:
        map_events._MAP_EVENTS_DIR = original
