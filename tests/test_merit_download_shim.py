"""``ai_hydro.data.merit_download`` re-exports the deprecated aihydro-watershed 0.4.0 downloader names."""

from __future__ import annotations

import importlib
import warnings
from unittest.mock import patch


def test_import_is_warning_free():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        import ai_hydro.data.merit_download as shim

        importlib.reload(shim)
    for name in ("ensure_level2_index", "download_river_shapefile", "download_catchment_shapefile"):
        assert callable(getattr(shim, name))


def test_call_emits_deprecation_warning_without_breaking():
    import pytest

    import ai_hydro.data.merit_download as shim

    # Stage nothing: replace the staging step so no network or disk work happens.
    with patch("aihydro_watershed.merit.merit_download._stage_region_db", return_value=True):
        with pytest.warns(DeprecationWarning):
            assert shim.download_river_shapefile("46") is True
