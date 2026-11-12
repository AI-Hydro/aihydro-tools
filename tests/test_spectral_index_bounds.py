"""compute_spectral_index: bounded, recorded fetching (DEMO-1 rehearsal F10). Offline; aihydro_data.fetch is faked."""
from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from shapely.geometry import box

from ai_hydro.mcp import tools_indices as ti
from ai_hydro.session import HydroSession

EE_SIZE = "Total request size (57800000 Bytes) must be less than or equal to 50331648 bytes."


def _da(res=33.0):
    da = xr.DataArray(np.full((3, 3), 0.5), dims=("y", "x"))
    da.attrs["resolution_m"] = res
    return da


def _install(fetch):
    fake = types.ModuleType("aihydro_data")
    fake.fetch = fetch
    tr = types.ModuleType("aihydro_data.transforms.indices")
    tr.compute_index = lambda *a, **k: None
    tr.INDEX_REGISTRY = {}
    return patch.dict(sys.modules, {"aihydro_data": fake, "aihydro_data.transforms.indices": tr})


def _run(frequency, start, end):
    return ti._fetch_and_compute_index(idx_key="NDVI", sensor="sentinel2", required_bands=[], watershed=box(0, 0, 1, 1),
                                       start=start, end=end, frequency=frequency, apply_cloud_mask=True, reg_entry={})


# ---- (a) size error -> bounded retry, resolution recorded ---------------------------------------------------
def test_size_error_is_retried_once_tiled_and_the_resolution_is_recorded():
    calls = []

    def fetch(**kw):
        calls.append(kw)
        if not kw.get("native_resolution"):
            raise RuntimeError(EE_SIZE)
        return types.SimpleNamespace(data=_da(10.0), product="SENTINEL2_SR", product_substituted=False)

    with _install(fetch):
        out = _run(None, "2021-01-01", "2021-12-31")
    assert "error" not in out
    assert [c.get("native_resolution") for c in calls] == [False, True]
    assert all(c.get("fallback") == [] for c in calls)                    # strict pin: no walk to other hosts
    rec = out["fetch_records"][0]
    assert rec["resolution_m"] == 10.0 and rec["tiled_native_retry"] is True
    assert [a["attempt"] for a in rec["attempts"]] == ["pinned_strict", "pinned_strict_tiled_native"]


def test_size_error_that_persists_does_not_fall_through_to_other_backends():
    calls = []

    def fetch(**kw):
        calls.append(kw)
        raise RuntimeError(EE_SIZE)

    with _install(fetch):
        out = _run(None, "2021-01-01", "2021-12-31")
    assert out["code"] == "FETCH_ERROR" and len(calls) == 2              # strict + tiled, then stop (no policy chain)
    assert [a["attempt"] for a in out["attempts"]] == ["pinned_strict", "pinned_strict_tiled_native"]


# ---- (b) bounded fall-through, structured partial result -----------------------------------------------------
def test_consecutive_failures_stop_the_stack_and_return_a_structured_partial():
    calls = []

    def fetch(**kw):
        calls.append(kw["start"])
        if kw["start"] == "2001-01-01":
            return types.SimpleNamespace(data=_da(), product="SENTINEL2_SR")
        raise RuntimeError("backend unreachable")

    with _install(fetch):
        out = _run("yearly", "2001-01-01", "2010-12-31")
    assert "error" not in out
    assert [r["period"] for r in out["period_stats"]] == ["2001-01-01"]
    assert len(out["period_failures"]) == ti.MAX_CONSECUTIVE_PERIOD_FAILURES
    assert out["stopped_early"]["after_consecutive_failures"] == ti.MAX_CONSECUTIVE_PERIOD_FAILURES
    assert [p["period"] for p in out["periods_not_attempted"]] == [f"{y}-01-01" for y in range(2005, 2011)]
    assert not any(c.startswith(("2005", "2006", "2010")) for c in calls)   # never requested
    assert all(f["attempts"] for f in out["period_failures"])


def test_success_resets_the_consecutive_counter():
    def fetch(**kw):
        if kw["start"][:4] in ("2002", "2003", "2005", "2006"):
            raise RuntimeError("no scenes")
        return types.SimpleNamespace(data=_da(), product="SENTINEL2_SR")

    with _install(fetch):
        out = _run("yearly", "2001-01-01", "2008-12-31")
    assert out["periods_not_attempted"] == [] and out["stopped_early"] is None
    assert len(out["period_failures"]) == 4 and len(out["period_stats"]) == 4


def test_all_periods_failing_is_a_structured_no_data_error():
    def fetch(**kw):
        raise RuntimeError("down")

    with _install(fetch):
        out = _run("monthly", "2021-01-01", "2021-12-31")
    assert out["code"] == "NO_DATA"
    assert out["stopped_early"] and len(out["periods_failed"]) == ti.MAX_CONSECUTIVE_PERIOD_FAILURES
    assert len(out["periods_not_attempted"]) == 12 - ti.MAX_CONSECUTIVE_PERIOD_FAILURES


# ---- (c) cache keyed by parameters -------------------------------------------------------------------------------
@pytest.fixture
def tool_env(tmp_path):
    poly = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
         patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
         patch("ai_hydro.mcp.helpers._resolve_session", return_value="spx"), \
         patch("ai_hydro.mcp.tools_analysis._resolve_session_geometry", return_value=poly):
        HydroSession("spx").save()
        yield


def _call(**kw):
    return asyncio.run(ti.compute_spectral_index(index_name="NDVI", session_id="spx", create_map=False, **kw))


def test_second_call_with_a_different_window_computes_and_each_request_is_cached(tool_env):
    seen = []

    def fake(**kw):
        seen.append((kw["start"], kw["end"]))
        v = 0.2 if kw["start"].startswith("2020") else 0.7
        return {"index_da": xr.DataArray(np.full((3, 3), v, dtype="float32"), dims=("y", "x")),
                "time_axis": None, "period_stats": [], "period_failures": [], "fetch_records": []}

    with patch("ai_hydro.mcp.tools_indices._fetch_and_compute_index", side_effect=fake):
        a = _call(start="2020-01-01", end="2020-12-31")
        b = _call(start="2021-01-01", end="2021-12-31")      # old code: CACHED_INDEX_PARAMS_MISMATCH
        a2 = _call(start="2020-01-01", end="2020-12-31")
    assert not a.get("error") and not b.get("error"), (a, b)
    assert a["data"]["mean"] == pytest.approx(0.2) and b["data"]["mean"] == pytest.approx(0.7)
    assert a2.get("_cached") is True and a2["data"]["mean"] == pytest.approx(0.2)
    assert seen == [("2020-01-01", "2020-12-31"), ("2021-01-01", "2021-12-31")]


def test_partial_stack_is_flagged_and_not_cached(tool_env):
    stats = [{"period": "2020-01-01", "period_end": "2020-12-31", "mean": 0.4, "n_pixels": 9}]
    fake = {"index_da": xr.DataArray(np.full((3, 3), 0.4, dtype="float32"), dims=("y", "x")),
            "time_axis": ["2020-01-01"], "period_stats": stats, "n_periods_requested": 4,
            "period_failures": [{"period": "2021-01-01", "period_end": "2021-12-31", "reason": "x", "attempts": []}],
            "periods_not_attempted": [{"period": "2022-01-01", "period_end": "2022-12-31"}],
            "stopped_early": {"after_consecutive_failures": 3, "limit": 3},
            "fetch_records": [{"period": "2020-01-01", "resolution_m": 33.0}]}
    with patch("ai_hydro.mcp.tools_indices._fetch_and_compute_index", return_value=fake) as m:
        a = _call(start="2020-01-01", end="2023-12-31", frequency="yearly")
        _call(start="2020-01-01", end="2023-12-31", frequency="yearly")
    assert a["partial"] is True and a["stopped_early"]["limit"] == 3
    assert a["periods_not_attempted"][0]["period"] == "2022-01-01" and a["resolution_m"] == 33.0
    assert m.call_count == 2                                            # the gap is retried, not replayed
