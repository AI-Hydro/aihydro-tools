"""
Integration tests for ai_hydro.mcp.tools_indices.

Covers:
  - list_spectral_indices: structure, required fields, sensors list
  - compute_spectral_index: mocked fetch → correct response shape, GeoTIFF written,
    session slot registered, cache reuse returns _cached=True
  - Time-series helpers: _monthly_periods, _yearly_periods
  - Mann-Kendall helper: fallback to linear regression when pymannkendall absent
  - Tool tier / domain registration sanity checks

All tests are marked "not live" — no real data backends are called.
"""
from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import xarray as xr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_index_da(h: int = 40, w: int = 40, fill: float = 0.4) -> xr.DataArray:
    """Synthetic index DataArray (no real CRS)."""
    x = np.linspace(-1.0, 1.0, w)
    y = np.linspace(1.0, -1.0, h)
    data = np.full((h, w), fill, dtype="float32")
    return xr.DataArray(data, dims=("y", "x"), coords={"y": y, "x": x}, name="ndwi")


def _make_fake_session(session_id: str, workspace: str) -> MagicMock:
    """Minimal HydroSession mock."""
    sess = MagicMock()
    sess.session_id = session_id
    sess.workspace_dir = workspace
    sess.watershed = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-1.0, -1.0], [1.0, -1.0], [1.0, 1.0], [-1.0, 1.0], [-1.0, -1.0],
            ]],
        },
        "properties": {},
    }
    # HydroSession.get(slot) returns None by default (no cache)
    sess.get.return_value = None
    return sess


def _fake_fetch_result(index_da: xr.DataArray):
    """Fake aihydro_data.fetch() return value wrapping a DataArray."""
    result = MagicMock()
    result.data = index_da
    return result


# ---------------------------------------------------------------------------
# list_spectral_indices
# ---------------------------------------------------------------------------

class TestListSpectralIndices:
    def test_returns_dict_with_indices_key(self):
        from ai_hydro.mcp.tools_indices import list_spectral_indices
        result = list_spectral_indices()
        assert isinstance(result, dict)
        assert "indices" in result

    def test_n_indices_matches_indices_dict(self):
        from ai_hydro.mcp.tools_indices import list_spectral_indices
        result = list_spectral_indices()
        if "error" not in result:
            assert result["n_indices"] == len(result["indices"])

    def test_each_entry_has_required_fields(self):
        from ai_hydro.mcp.tools_indices import list_spectral_indices
        result = list_spectral_indices()
        if "error" in result:
            pytest.skip("aihydro_data not installed")
        for name, entry in result["indices"].items():
            assert "bands" in entry, f"Missing 'bands' in {name}"
            assert "colormap" in entry, f"Missing 'colormap' in {name}"
            assert "range" in entry, f"Missing 'range' in {name}"
            assert "citation" in entry, f"Missing 'citation' in {name}"
            assert "use_case" in entry, f"Missing 'use_case' in {name}"

    def test_sensors_list_present(self):
        from ai_hydro.mcp.tools_indices import list_spectral_indices
        result = list_spectral_indices()
        if "error" in result:
            pytest.skip("aihydro_data not installed")
        assert "sensors" in result
        assert isinstance(result["sensors"], list)
        assert len(result["sensors"]) > 0

    def test_ndwi_entry_has_blues_colormap(self):
        from ai_hydro.mcp.tools_indices import list_spectral_indices
        result = list_spectral_indices()
        if "error" in result:
            pytest.skip("aihydro_data not installed")
        assert "NDWI" in result["indices"]
        assert result["indices"]["NDWI"]["colormap"] == "Blues"

    def test_graceful_importerror(self):
        """If aihydro_data is absent, returns error dict (not exception)."""
        import sys
        import importlib
        from unittest.mock import patch

        # Temporarily make aihydro_data unavailable
        saved = sys.modules.get("aihydro_data.transforms.indices")
        sys.modules["aihydro_data.transforms.indices"] = None  # type: ignore

        try:
            from ai_hydro.mcp.tools_indices import list_spectral_indices
            # Force re-import by patching the import inside the function
            with patch.dict("sys.modules", {"aihydro_data.transforms.indices": None}):
                result = list_spectral_indices()
                # Either returns with "error" key or the real data (if already imported)
                assert isinstance(result, dict)
        finally:
            if saved is not None:
                sys.modules["aihydro_data.transforms.indices"] = saved
            elif "aihydro_data.transforms.indices" in sys.modules:
                del sys.modules["aihydro_data.transforms.indices"]


# ---------------------------------------------------------------------------
# compute_spectral_index — mocked fetch path
# ---------------------------------------------------------------------------

class TestComputeSpectralIndexMocked:
    """All live backends replaced with MagicMocks."""

    def _run(self, **kwargs):
        """Synchronously invoke the async compute_spectral_index tool."""
        import asyncio
        from ai_hydro.mcp.tools_indices import compute_spectral_index
        return asyncio.run(compute_spectral_index(**kwargs))

    @pytest.fixture(autouse=True)
    def _patch_registry(self):
        """Ensure INDEX_REGISTRY has at least NDWI + NDVI for all tests."""
        fake_registry = {
            "NDWI": {
                "fn": lambda green, nir: green,
                "bands": ["green", "nir"],
                "colormap": "Blues",
                "range": (-1, 1),
                "citation": "McFeeters 1996",
                "use_case": "surface water mapping",
                "threshold_hint": 0.3,
            },
            "NDVI": {
                "fn": lambda red, nir: red,
                "bands": ["red", "nir"],
                "colormap": "RdYlGn",
                "range": (-1, 1),
                "citation": "Rouse 1974",
                "use_case": "vegetation health",
                "threshold_hint": 0.4,
            },
        }
        fake_band_maps = {
            "sentinel2": {"green": "B3", "nir": "B8", "red": "B4"},
            "landsat8": {"green": "B3", "nir": "B5", "red": "B4"},
        }
        fake_module = types.ModuleType("aihydro_data.transforms.indices")
        fake_module.INDEX_REGISTRY = fake_registry
        fake_module.SENSOR_BAND_MAPS = fake_band_maps
        fake_module.compute_index = lambda name, ds, **kw: ds[list(ds.data_vars)[0]]

        fake_cm_module = types.ModuleType("aihydro_data.transforms.cloud_mask")
        fake_cm_module.mask_clouds = lambda ds, sensor="sentinel2", **kw: ds

        fake_aihydro_data = types.ModuleType("aihydro_data")

        import sys
        with patch.dict("sys.modules", {
            "aihydro_data": fake_aihydro_data,
            "aihydro_data.transforms": types.ModuleType("aihydro_data.transforms"),
            "aihydro_data.transforms.indices": fake_module,
            "aihydro_data.transforms.cloud_mask": fake_cm_module,
        }):
            yield fake_registry, fake_aihydro_data, fake_module

    def test_unknown_index_returns_error(self, tmp_path):
        """Passing an unrecognised index_name returns an error dict, not an exception."""
        index_da = _make_index_da()
        fake_session = _make_fake_session("test01", str(tmp_path))

        with patch("ai_hydro.mcp.helpers._resolve_session", return_value="test01"), \
             patch("ai_hydro.mcp.helpers._maybe_set_workspace"), \
             patch("ai_hydro.session.HydroSession.load", return_value=fake_session):
            result = self._run(index_name="FOOBAR", session_id="test01",
                               start="2024-01-01", end="2024-06-30")

        assert "error" in result
        assert result.get("code") == "UNKNOWN_INDEX"

    def test_correct_response_shape(self, tmp_path):
        """Mocked fetch → response has all documented keys."""
        index_da = _make_index_da()
        fake_session = _make_fake_session("test01", str(tmp_path))

        fake_ds = xr.Dataset({"ndwi": index_da})
        fake_fetch_result = _fake_fetch_result(fake_ds)

        import aihydro_data as _aihd  # the patched module from fixture
        _aihd.fetch = MagicMock(return_value=fake_fetch_result)

        import sys
        from importlib import import_module

        with patch("ai_hydro.mcp.helpers._resolve_session", return_value="test01"), \
             patch("ai_hydro.mcp.helpers._maybe_set_workspace"), \
             patch("ai_hydro.session.HydroSession.load", return_value=fake_session), \
             patch("ai_hydro.mcp.tools_analysis._resolve_session_geometry",
                   return_value=fake_session.watershed["geometry"]), \
             patch("ai_hydro.mcp.tools_indices._fetch_and_compute_index",
                   return_value={"index_da": index_da, "time_axis": None, "period_stats": []}), \
             patch("ai_hydro.mcp.helpers._canonical_prefix",
                   return_value="test01_index_ndwi"), \
             patch("ai_hydro.mcp.helpers._session_store"):
            result = self._run(index_name="NDWI", session_id="test01",
                               start="2024-01-01", end="2024-06-30",
                               create_map=False)

        required_keys = {"index_name", "data", "colormap", "citation", "use_case",
                         "threshold_hint", "_files_saved", "_map_layer", "next_steps"}
        assert required_keys.issubset(result.keys()), (
            f"Missing keys: {required_keys - result.keys()}"
        )
        assert result["index_name"] == "NDWI"
        assert "mean" in result["data"]
        assert "valid_px" in result["data"]

    def test_session_slot_registered(self, tmp_path):
        """compute_spectral_index must store result in session under 'index_ndwi'."""
        index_da = _make_index_da()
        fake_session = _make_fake_session("test01", str(tmp_path))

        stored = {}

        def _fake_store(sid, slot, data, tool_name=None):
            stored[slot] = data

        with patch("ai_hydro.mcp.helpers._resolve_session", return_value="test01"), \
             patch("ai_hydro.mcp.helpers._maybe_set_workspace"), \
             patch("ai_hydro.session.HydroSession.load", return_value=fake_session), \
             patch("ai_hydro.mcp.tools_analysis._resolve_session_geometry",
                   return_value=fake_session.watershed["geometry"]), \
             patch("ai_hydro.mcp.tools_indices._fetch_and_compute_index",
                   return_value={"index_da": index_da, "time_axis": None, "period_stats": []}), \
             patch("ai_hydro.mcp.helpers._canonical_prefix",
                   return_value="test01_index_ndwi"), \
             patch("ai_hydro.mcp.helpers._session_store", side_effect=_fake_store):
            result = self._run(index_name="NDWI", session_id="test01",
                               start="2024-01-01", end="2024-06-30",
                               create_map=False)

        assert "index_ndwi" in stored, "Expected slot 'index_ndwi' to be written"

    def test_cache_reuse_returns_cached_flag(self, tmp_path):
        """Second call with same dates returns _cached=True from session slot."""
        index_da = _make_index_da()
        cached_result = {
            "index_name": "NDWI",
            "data": {"mean": 0.4, "valid_px": 1600},
            "colormap": "Blues",
        }
        fake_session = _make_fake_session("test01", str(tmp_path))
        fake_session.get.return_value = cached_result  # simulate cache hit (HydroSession.get)

        with patch("ai_hydro.mcp.helpers._resolve_session", return_value="test01"), \
             patch("ai_hydro.mcp.helpers._maybe_set_workspace"), \
             patch("ai_hydro.session.HydroSession.load", return_value=fake_session):
            result = self._run(index_name="NDWI", session_id="test01",
                               start="2024-01-01", end="2024-06-30")

        assert result.get("_cached") is True, "Expected _cached=True on cache hit"

    def test_no_exception_when_geotiff_write_fails(self, tmp_path):
        """GeoTIFF write failure (e.g. no CRS) is non-fatal — result dict returned."""
        index_da = _make_index_da()  # no CRS → rio.to_raster raises AttributeError
        fake_session = _make_fake_session("test01", str(tmp_path))

        # Don't mock to_raster — the plain DataArray has no .rio accessor,
        # so the write attempt inside the tool naturally raises AttributeError.
        # The tool wraps it in try/except, so result must still arrive.
        with patch("ai_hydro.mcp.helpers._resolve_session", return_value="test01"), \
             patch("ai_hydro.mcp.helpers._maybe_set_workspace"), \
             patch("ai_hydro.session.HydroSession.load", return_value=fake_session), \
             patch("ai_hydro.mcp.tools_analysis._resolve_session_geometry",
                   return_value=fake_session.watershed["geometry"]), \
             patch("ai_hydro.mcp.tools_indices._fetch_and_compute_index",
                   return_value={"index_da": index_da, "time_axis": None, "period_stats": []}), \
             patch("ai_hydro.mcp.helpers._canonical_prefix",
                   return_value="test01_index_ndwi"), \
             patch("ai_hydro.mcp.helpers._session_store"):
            result = self._run(index_name="NDWI", session_id="test01",
                               start="2024-01-01", end="2024-06-30",
                               create_map=False)

        # GeoTIFF write failed silently — index stats must still be present
        assert "index_name" in result
        assert result["index_name"] == "NDWI"

    def test_hydrosession_uses_get_not_get_slot(self):
        """Regression: compute_spectral_index reads the cache via HydroSession.get(),
        NOT get_slot() (which never existed). Reverting reintroduces the crash bug.
        """
        from ai_hydro.session import HydroSession
        assert hasattr(HydroSession, "get"), "HydroSession.get must exist"
        assert not hasattr(HydroSession, "get_slot"), (
            "HydroSession has no get_slot — tools_indices must call session.get()"
        )


# ---------------------------------------------------------------------------
# Time-series helpers
# ---------------------------------------------------------------------------

class TestPeriodHelpers:
    def test_monthly_periods_count(self):
        from ai_hydro.mcp.tools_indices import _monthly_periods
        periods = _monthly_periods("2024-01-01", "2024-03-31")
        assert len(periods) == 3
        assert periods[0][0] == "2024-01-01"
        assert periods[2][1] == "2024-03-31"

    def test_monthly_periods_single_month(self):
        from ai_hydro.mcp.tools_indices import _monthly_periods
        periods = _monthly_periods("2024-06-10", "2024-06-20")
        assert len(periods) == 1
        assert periods[0] == ("2024-06-10", "2024-06-20")

    def test_yearly_periods_two_years(self):
        from ai_hydro.mcp.tools_indices import _yearly_periods
        periods = _yearly_periods("2022-06-01", "2023-09-30")
        assert len(periods) == 2
        assert periods[0][0] == "2022-06-01"
        assert periods[0][1] == "2022-12-31"
        assert periods[1][0] == "2023-01-01"
        assert periods[1][1] == "2023-09-30"

    def test_yearly_periods_single_year(self):
        from ai_hydro.mcp.tools_indices import _yearly_periods
        periods = _yearly_periods("2023-03-01", "2023-11-30")
        assert len(periods) == 1
        assert periods[0] == ("2023-03-01", "2023-11-30")

    def test_monthly_periods_year_boundary(self):
        """December 2023 → January 2024 spans the year boundary."""
        from ai_hydro.mcp.tools_indices import _monthly_periods
        periods = _monthly_periods("2023-12-01", "2024-01-31")
        assert len(periods) == 2
        assert periods[0][0] == "2023-12-01"
        assert periods[1][1] == "2024-01-31"


# ---------------------------------------------------------------------------
# Mann-Kendall helper
# ---------------------------------------------------------------------------

class TestMannKendallSlope:
    def test_returns_tuple_of_two(self):
        from ai_hydro.mcp.tools_indices import _mann_kendall_slope
        result = _mann_kendall_slope([0.1, 0.2, 0.3, 0.4, 0.5])
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_positive_trend_positive_slope(self):
        from ai_hydro.mcp.tools_indices import _mann_kendall_slope
        slope, _ = _mann_kendall_slope([0.1, 0.2, 0.3, 0.4, 0.5])
        assert slope is not None
        assert slope > 0

    def test_constant_series_zero_slope(self):
        from ai_hydro.mcp.tools_indices import _mann_kendall_slope
        slope, _ = _mann_kendall_slope([0.5, 0.5, 0.5, 0.5, 0.5])
        assert slope is not None
        assert abs(slope) < 1e-8

    def test_short_series_handled(self):
        """Series with 2 elements should not raise."""
        from ai_hydro.mcp.tools_indices import _mann_kendall_slope
        slope, _ = _mann_kendall_slope([0.3, 0.6])
        # May return None or a value — just must not raise
        assert slope is None or isinstance(slope, float)

    def test_fallback_when_pymannkendall_absent(self):
        """If pymannkendall is not installed, linear regression is used."""
        import sys
        with patch.dict("sys.modules", {"pymannkendall": None}):
            from ai_hydro.mcp.tools_indices import _mann_kendall_slope
            slope, p = _mann_kendall_slope([0.1, 0.2, 0.3, 0.4, 0.5])
        assert slope is not None
        assert p is None  # linear fallback returns None for p-value


# ---------------------------------------------------------------------------
# Tier / domain registration sanity
# ---------------------------------------------------------------------------

class TestToolRegistration:
    def test_compute_spectral_index_in_tool_tiers(self):
        from ai_hydro.mcp.app import TOOL_TIERS
        assert "compute_spectral_index" in TOOL_TIERS
        assert TOOL_TIERS["compute_spectral_index"] == 2

    def test_list_spectral_indices_in_tool_tiers(self):
        from ai_hydro.mcp.app import TOOL_TIERS
        assert "list_spectral_indices" in TOOL_TIERS
        assert TOOL_TIERS["list_spectral_indices"] == 3

    def test_analysis_domain_in_domain_prefixes(self):
        from ai_hydro.mcp.tools_discovery import _DOMAIN_PREFIXES
        assert "analysis" in _DOMAIN_PREFIXES
        prefixes = _DOMAIN_PREFIXES["analysis"]
        assert "compute_spectral_index" in prefixes or any(
            "compute_spectral_index".startswith(p) for p in prefixes
        )
