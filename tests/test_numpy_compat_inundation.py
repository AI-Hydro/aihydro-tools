"""Direct pysheds users in the tools pack must survive NumPy without ``np.in1d``."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("pyflwdir")
pytest.importorskip("pysheds")
pytest.importorskip("rioxarray")

# Import first: package imports may apply the shim as a side effect, which would mask
# a missing explicit call. The fixture removes ``np.in1d`` only after these imports.
import rioxarray  # noqa: E402,F401
import xarray as xr  # noqa: E402
from shapely.geometry import box  # noqa: E402

from ai_hydro.analysis import _dem, inundation, inundation_spike  # noqa: E402

_MISSING = object()


@pytest.fixture
def no_native_in1d():
    """Remove ``np.in1d`` (as in NumPy >= 2.5); save and restore it for order independence."""
    saved = np.__dict__.get("in1d", _MISSING)
    np.__dict__.pop("in1d", None)
    yield
    if saved is _MISSING:
        np.__dict__.pop("in1d", None)
    else:
        np.in1d = saved


def test_hand_spike_runs_without_native_in1d(no_native_in1d):
    result = inundation_spike.run_synthetic_hand_spike(stream_acc_threshold=5)
    assert result["hand_max_m"] > result["hand_min_m"]
    assert result["lookup_monotonic"] is True


def test_prepare_hand_stack_runs_without_native_in1d(no_native_in1d, monkeypatch):
    n = 40
    ys = 5_540_000 - 30.0 * np.arange(n)
    xs = 500_000 + 30.0 * np.arange(n)
    z = np.abs(np.arange(n) - n / 2)[None, :] * 0.5 + np.arange(n)[::-1][:, None] * 0.3 + 100
    dem = xr.DataArray(z.astype("float32"), dims=("y", "x"), coords={"y": ys, "x": xs}).rio.write_crs(
        "EPSG:32632"
    )
    monkeypatch.setattr(_dem, "fetch_dem", lambda *a, **k: (dem, "synthetic", "test"))

    out = inundation._prepare_hand_stack(box(8.9, 49.9, 9.2, 50.1), stream_acc_threshold=5)
    assert out["acc"].max() > 5
    assert np.isfinite(out["hand"]).any()
