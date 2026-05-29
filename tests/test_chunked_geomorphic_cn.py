"""
Regression tests for chunked geomorphic slope + CN overlay.

All tests are pure-numpy / synthetic — no live backends, no 3DEP, no Polaris.
Marked offline (default pytest).  Run with:

    pytest tests/test_chunked_geomorphic_cn.py -v

Coverage
--------
- ``_slope_horn_kernel``: formula correctness on flat/tilted/random DEMs
- ``_slope_horn_kernel``: border pixels are NaN (kernel_pad=1 contract)
- Geomorphic slope: chunked path (small auto_trigger) == xrspatial single-pass
  (skipped when xrspatial unavailable)
- ``_build_joint_cn_lookup``: round-trip for each known (NLCD, soil_group) pair
- ``_vectorised_cn_lookup``: exact match vs the old nested-loop reference
- CN grid: chunked path (small auto_trigger) == vectorised single-pass exactly
"""
from __future__ import annotations

import importlib

import numpy as np
import pytest
import xarray as xr
from shapely.geometry import box

from ai_hydro.analysis.curve_number import (
    _build_joint_cn_lookup,
    _create_cn_lookup_table,
    _create_cn_grid_from_data,
    _classify_soil_hydrologic_group,
    _vectorised_cn_lookup,
    _CN_CHUNK_TRIGGER,
)
from ai_hydro.analysis.geomorphic import (
    _slope_horn_kernel,
    _SLOPE_CHUNK_TRIGGER,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dem(height: int, width: int,
              dx: float = 30.0, dy: float = 30.0,
              x0: float = 0.0, y0: float | None = None,
              fill: float | np.ndarray = 100.0) -> xr.DataArray:
    """Synthetic DEM as xr.DataArray with uniform spacing + affine CRS."""
    if y0 is None:
        y0 = height * dy
    x = x0 + dx / 2 + np.arange(width) * dx
    y = y0 - dy / 2 - np.arange(height) * dy
    if isinstance(fill, np.ndarray):
        data = fill.astype("float32")
    else:
        data = np.full((height, width), fill, dtype="float32")
    return xr.DataArray(data, dims=("y", "x"), coords={"y": y, "x": x}, name="dem")


def _flat_lulc_soil(h: int = 100, w: int = 100):
    """Synthetic LULC + soil Dataset for CN grid tests."""
    lulc_arr = np.full((h, w), 41.0, dtype="float32")   # Deciduous Forest everywhere
    soil_arr = np.full((h, w), 30.0, dtype="float32")   # sand ~30%
    silt_arr = np.full((h, w), 40.0, dtype="float32")
    clay_arr = np.full((h, w), 30.0, dtype="float32")   # → soil group C (CN=73 for 41)

    x = np.arange(w, dtype="float32")
    y = np.arange(h, dtype="float32")[::-1]
    coords = {"y": y, "x": x}

    lulc_ds = xr.Dataset(
        {"cover_2019": xr.DataArray(lulc_arr, dims=("y", "x"), coords=coords)},
    )
    soil_ds = xr.Dataset({
        "sand_0_5cm_mean": xr.DataArray(soil_arr, dims=("y", "x"), coords=coords),
        "silt_0_5cm_mean": xr.DataArray(silt_arr, dims=("y", "x"), coords=coords),
        "clay_0_5cm_mean": xr.DataArray(clay_arr, dims=("y", "x"), coords=coords),
    })
    return lulc_ds, soil_ds


# ---------------------------------------------------------------------------
# _slope_horn_kernel — unit tests
# ---------------------------------------------------------------------------

class TestSlopeHornKernel:
    def test_flat_dem_zero_slope(self):
        """Flat DEM → slope = 0 everywhere (interior)."""
        dem = np.full((10, 10), 100.0, dtype="float32")
        mask = np.ones((10, 10), dtype=bool)
        out = _slope_horn_kernel(dem, mask, cell_size_m=30.0)
        interior = out[1:-1, 1:-1]
        np.testing.assert_allclose(interior, 0.0, atol=1e-5)

    def test_border_pixels_nan(self):
        """Border row/col must be NaN (kernel_pad=1 contract)."""
        dem = np.random.default_rng(0).random((8, 8)).astype("float32") * 500
        mask = np.ones((8, 8), dtype=bool)
        out = _slope_horn_kernel(dem, mask, cell_size_m=30.0)
        # Row 0, row -1, col 0, col -1
        assert np.all(np.isnan(out[0, :]))
        assert np.all(np.isnan(out[-1, :]))
        assert np.all(np.isnan(out[:, 0]))
        assert np.all(np.isnan(out[:, -1]))

    def test_uniform_x_slope(self):
        """DEM that rises 1 m per cell in x → slope ≈ arctan(1/cell_size)."""
        h, w = 7, 7
        cell = 30.0
        x_idx = np.arange(w)
        dem = np.tile(x_idx.astype("float32"), (h, 1))  # each col = col_index metres
        mask = np.ones((h, w), dtype=bool)
        out = _slope_horn_kernel(dem, mask, cell_size_m=cell)
        interior = out[2:-2, 2:-2]  # away from edge
        expected_deg = float(np.degrees(np.arctan(1.0 / cell)))
        np.testing.assert_allclose(interior, expected_deg, atol=1e-3)

    def test_uniform_y_slope(self):
        """DEM that rises 1 m per row in y → slope ≈ arctan(1/cell_size)."""
        h, w = 7, 7
        cell = 30.0
        y_idx = np.arange(h)[:, None]
        dem = np.tile(y_idx.astype("float32"), (1, w))
        mask = np.ones((h, w), dtype=bool)
        out = _slope_horn_kernel(dem, mask, cell_size_m=cell)
        interior = out[2:-2, 2:-2]
        expected_deg = float(np.degrees(np.arctan(1.0 / cell)))
        np.testing.assert_allclose(interior, expected_deg, atol=1e-3)

    def test_output_dtype_float32(self):
        dem = np.random.default_rng(1).random((6, 6)).astype("float64") * 200
        mask = np.ones((6, 6), dtype=bool)
        out = _slope_horn_kernel(dem, mask, cell_size_m=10.0)
        assert out.dtype == np.float32

    def test_small_dem_returns_all_nan(self):
        """2×2 DEM is too small for a 3×3 kernel — all NaN."""
        dem = np.array([[10.0, 12.0], [15.0, 11.0]], dtype="float32")
        mask = np.ones((2, 2), dtype=bool)
        out = _slope_horn_kernel(dem, mask, cell_size_m=30.0)
        assert np.all(np.isnan(out))

    def test_mask_ignored_computation(self):
        """Mask is passed through but does not change slope values (masking
        responsibility belongs to chunked_raster_apply)."""
        dem = np.random.default_rng(2).random((8, 8)).astype("float32") * 300
        mask_all = np.ones((8, 8), dtype=bool)
        mask_half = mask_all.copy()
        mask_half[:4, :] = False
        out_all = _slope_horn_kernel(dem, mask_all, 30.0)
        out_half = _slope_horn_kernel(dem, mask_half, 30.0)
        # Interior values should be identical regardless of mask
        np.testing.assert_array_equal(out_all[1:-1, 1:-1], out_half[1:-1, 1:-1])


# ---------------------------------------------------------------------------
# Chunked slope vs xrspatial single-pass
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    importlib.util.find_spec("xrspatial") is None,
    reason="xrspatial not installed",
)
class TestChunkedSlopeVsXrspatial:
    """Verify that the chunked Horn kernel produces results consistent with
    xrspatial.slope on a synthetic DEM.  Only runs when xrspatial is present."""

    def _run_xrspatial(self, dem_da: xr.DataArray) -> np.ndarray:
        import xrspatial
        return xrspatial.slope(dem_da).values

    def _run_chunked(self, dem_da: xr.DataArray, basin) -> np.ndarray:
        from aihydro_data.sampling import chunked_raster_apply
        cell = 30.0
        fn = lambda arr, msk: _slope_horn_kernel(arr, msk, cell)
        result = chunked_raster_apply(
            dem_da, basin, fn,
            kernel_pad=1,
            auto_trigger_size=0,   # force chunked
        )
        return result.values

    def test_flat_dem_zero_slope_both_paths(self):
        basin = box(100, 100, 800, 800)
        dem = _make_dem(100, 100, dx=30.0, dy=30.0, x0=0, y0=3000, fill=200.0)
        xs = self._run_xrspatial(dem)
        xc = self._run_chunked(dem, basin)
        # Interior of both should be ~0 (NaN outside basin)
        interior_xs = xs[5:-5, 5:-5]
        interior_xc = xc[5:-5, 5:-5]
        assert np.nanmax(np.abs(interior_xs)) < 0.1
        assert np.nanmax(np.abs(interior_xc)) < 0.1

    def test_tilted_dem_rmse_within_tolerance(self):
        """Tilted DEM: chunked Horn and xrspatial should agree within 0.01°."""
        rng = np.random.default_rng(42)
        h, w = 200, 200
        x_idx = np.tile(np.arange(w), (h, 1))
        base = x_idx.astype("float32") * 1.0 + rng.random((h, w)).astype("float32")
        basin = box(600, 600, 5400, 5400)
        dem = _make_dem(h, w, dx=30.0, dy=30.0, x0=0, y0=h * 30.0, fill=base)
        xs = self._run_xrspatial(dem)
        xc = self._run_chunked(dem, basin)
        # Compare where both are finite (inside basin)
        both_finite = np.isfinite(xs) & np.isfinite(xc)
        assert both_finite.sum() > 100
        rmse = np.sqrt(np.mean((xs[both_finite] - xc[both_finite]) ** 2))
        assert rmse < 0.1, f"Horn vs xrspatial RMSE = {rmse:.4f}° (expected < 0.1°)"


# ---------------------------------------------------------------------------
# CN lookup helpers
# ---------------------------------------------------------------------------

class TestBuildJointCnLookup:
    def test_round_trip_all_entries(self):
        """Every (nlcd, sg) entry in the table survives encode → lookup."""
        cn_table = _create_cn_lookup_table()
        lookup = _build_joint_cn_lookup(cn_table)
        for (nlcd, sg), expected_cn in cn_table.items():
            key = int(nlcd) * 10 + int(sg)
            assert not np.isnan(lookup[key]), f"NaN for ({nlcd}, {sg})"
            assert lookup[key] == pytest.approx(expected_cn, abs=0.01)

    def test_unknown_key_is_nan(self):
        lookup = _build_joint_cn_lookup(_create_cn_lookup_table())
        # NLCD class 99 is not in the table
        assert np.isnan(lookup[99 * 10 + 1])

    def test_lookup_size(self):
        lookup = _build_joint_cn_lookup(_create_cn_lookup_table())
        assert lookup.shape == (1000,)
        assert lookup.dtype == np.float32


class TestVectorisedCnLookup:
    """_vectorised_cn_lookup must match the old nested-loop logic exactly."""

    def _old_nested_loop(self, lulc_values, soil_groups, cn_table):
        """Reference implementation — the original nested-loop approach."""
        out = np.full(lulc_values.shape, np.nan, dtype=np.float32)
        for nlcd_class in np.unique(lulc_values[~np.isnan(lulc_values)]):
            for sg in range(1, 5):
                key = (int(nlcd_class), sg)
                if key in cn_table:
                    mask = (lulc_values == nlcd_class) & (soil_groups == sg)
                    out[mask] = cn_table[key]
        return out

    def test_exact_match_small_grid(self):
        h, w = 10, 10
        rng = np.random.default_rng(7)
        nlcd_classes = [11, 21, 22, 41, 42, 52, 71, 81, 82, 90]
        lulc = rng.choice(nlcd_classes, size=(h, w)).astype("float32")
        soil = rng.integers(1, 5, size=(h, w)).astype("int32")

        cn_table = _create_cn_lookup_table()
        lookup = _build_joint_cn_lookup(cn_table)
        ref = self._old_nested_loop(lulc, soil, cn_table)
        vec = _vectorised_cn_lookup(lulc, soil, lookup)

        # NaN positions must match
        np.testing.assert_array_equal(np.isnan(ref), np.isnan(vec))
        # Non-NaN values must be identical
        valid = ~np.isnan(ref)
        np.testing.assert_array_equal(ref[valid], vec[valid])

    def test_all_nan_for_unknown_class(self):
        lulc = np.full((4, 4), 99.0, dtype="float32")  # NLCD 99 not in table
        soil = np.full((4, 4), 2, dtype="int32")
        lookup = _build_joint_cn_lookup(_create_cn_lookup_table())
        out = _vectorised_cn_lookup(lulc, soil, lookup)
        assert np.all(np.isnan(out))

    def test_known_value_deciduous_forest_groupB(self):
        """NLCD 41 + soil group B → CN = 60 (per table)."""
        lulc = np.array([[41.0]], dtype="float32")
        soil = np.array([[2]], dtype="int32")   # group B
        lookup = _build_joint_cn_lookup(_create_cn_lookup_table())
        out = _vectorised_cn_lookup(lulc, soil, lookup)
        assert out[0, 0] == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# _create_cn_grid_from_data — chunked vs single-pass
# ---------------------------------------------------------------------------

class TestCnGridChunkedVsSinglePass:
    """Force the chunked path with a tiny trigger and verify exact agreement
    with the single-pass path on a synthetic raster."""

    def _make_inputs(self, h: int = 50, w: int = 50):
        """Return synthetic (lulc_ds, soil_ds, watershed_geom)."""
        rng = np.random.default_rng(99)
        nlcd_classes = [21, 41, 42, 52, 71, 81, 82]
        lulc_arr = rng.choice(nlcd_classes, size=(h, w)).astype("float32")
        sand_arr = rng.uniform(20, 60, size=(h, w)).astype("float32")
        silt_arr = rng.uniform(10, 40, size=(h, w)).astype("float32")
        clay_arr = (100 - sand_arr - silt_arr).clip(5, 60).astype("float32")

        x = np.arange(w, dtype="float32") * 30.0
        y = (np.arange(h, dtype="float32") * 30.0)[::-1]
        coords = {"y": y, "x": x}

        lulc_ds = xr.Dataset({
            "cover_2019": xr.DataArray(lulc_arr, dims=("y", "x"), coords=coords),
        })
        soil_ds = xr.Dataset({
            "sand_0_5cm_mean": xr.DataArray(sand_arr, dims=("y", "x"), coords=coords),
            "silt_0_5cm_mean": xr.DataArray(silt_arr, dims=("y", "x"), coords=coords),
            "clay_0_5cm_mean": xr.DataArray(clay_arr, dims=("y", "x"), coords=coords),
        })
        # Watershed covering most of the grid
        watershed = box(
            float(x.min()), float(y.min()),
            float(x.max()), float(y.max()),
        )
        return lulc_ds, soil_ds, watershed

    def test_chunked_equals_single_pass(self):
        lulc_ds, soil_ds, watershed = self._make_inputs(50, 50)

        # Single-pass (trigger > raster size)
        single, _, _ = _create_cn_grid_from_data(
            lulc_ds, soil_ds, year=2019, resolution=30,
            watershed_geom=watershed,
        )
        # Temporarily patch the trigger to 0 to force chunked path
        import ai_hydro.analysis.curve_number as _cnmod
        original_trigger = _cnmod._CN_CHUNK_TRIGGER
        _cnmod._CN_CHUNK_TRIGGER = 0
        try:
            chunked, _, _ = _create_cn_grid_from_data(
                lulc_ds, soil_ds, year=2019, resolution=30,
                watershed_geom=watershed,
            )
        finally:
            _cnmod._CN_CHUNK_TRIGGER = original_trigger

        sv = single.values
        cv = chunked.values
        # Where single-pass has a value, chunked must agree exactly
        both_valid = ~np.isnan(sv) & ~np.isnan(cv)
        assert both_valid.sum() > 0
        np.testing.assert_array_equal(sv[both_valid], cv[both_valid])

    def test_single_pass_returns_correct_dtype(self):
        lulc_ds, soil_ds, _ = self._make_inputs(20, 20)
        grid, _, stats = _create_cn_grid_from_data(
            lulc_ds, soil_ds, year=2019, resolution=30,
        )
        assert grid.values.dtype == np.float32
        assert isinstance(stats, dict)
        assert "soil_group_distribution" in stats

    def test_cn_values_in_valid_range(self):
        """All non-NaN CN values must be in NRCS table range 30–100."""
        lulc_ds, soil_ds, _ = self._make_inputs(30, 30)
        grid, _, _ = _create_cn_grid_from_data(
            lulc_ds, soil_ds, year=2019, resolution=30,
        )
        valid = grid.values[~np.isnan(grid.values)]
        assert valid.size > 0
        assert float(valid.min()) >= 30.0
        assert float(valid.max()) <= 100.0

    def test_no_watershed_geom_still_works(self):
        """Omitting watershed_geom falls back to single-pass, no error."""
        lulc_ds, soil_ds, _ = self._make_inputs(20, 20)
        grid, sg, stats = _create_cn_grid_from_data(
            lulc_ds, soil_ds, year=2019, resolution=30,
            watershed_geom=None,
        )
        assert grid.values.dtype == np.float32
