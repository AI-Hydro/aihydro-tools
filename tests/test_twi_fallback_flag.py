"""compute_twi: the stats-only fallback is flagged, package fields pass through, DEM source follows dem_product.

Offline: the watershed package's computations are stubbed.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

_POLY = json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]})

_PKG_FIELDS = {
    "twi_method": "full",
    "twi_method_reason": None,
    "twi_mask_basis": "watershed polygon rasterised on the DEM grid",
    "n_valid_cells": 1234,
}


def _stats(dem_product: str) -> dict:
    return {"twi_mean": 8.5, "resolution_m": 30.0, "dem_product": dem_product, "dem_source": "stub", **_PKG_FIELDS}


def _hydro_result(dem_product: str):
    from aihydro_core import HydroMeta, HydroResult

    return HydroResult(data=_stats(dem_product), meta=HydroMeta(tool="stub", version="0", gauge_id=None, sources=[], params={}))


async def _run(tmp_path, *, full_raises: bool, dem_product: str, create_map: bool = True, exc=None):
    from ai_hydro.session import HydroSession

    def _full(*a, **k):
        if full_raises:
            raise (exc if exc is not None else RuntimeError("DEM tile read failed"))
        return {**_stats(dem_product), "files_saved": []}

    with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
         patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
         patch("ai_hydro.mcp.tools_analysis._resolve_session", return_value="twi-flag"), \
         patch("ai_hydro.analysis.twi.compute_twi", _full), \
         patch("ai_hydro.analysis.twi.compute_twi_result", lambda **k: _hydro_result(dem_product)):
        HydroSession("twi-flag").save()
        from ai_hydro.mcp.tools_analysis import compute_twi

        out = await compute_twi(session_id="twi-flag", geometry_geojson=_POLY, create_map=create_map)
        cites = set(HydroSession.load("twi-flag")._citations)
    return out, cites


@pytest.mark.asyncio
async def test_fallback_is_flagged_with_reason_and_package_fields_pass_through(tmp_path):
    out, _ = await _run(tmp_path, full_raises=True, dem_product="DEM3DEP_10M")
    d = out["data"]
    assert d["twi_method"] == "fallback"
    assert "DEM tile read failed" in d["twi_method_reason"]
    assert d["package_twi_method"] == "full"
    assert d["twi_mask_basis"] == _PKG_FIELDS["twi_mask_basis"]
    assert d["n_valid_cells"] == 1234
    assert d["dem_product"] == "DEM3DEP_10M"
    assert out["meta"]["dem_product"] == "DEM3DEP_10M"


@pytest.mark.asyncio
async def test_no_workspace_stats_only_is_not_called_a_fallback(tmp_path):
    out, _ = await _run(tmp_path, full_raises=False, dem_product="GLO30", create_map=False)
    d = out["data"]
    assert d["twi_method"] == "full"
    assert "package_twi_method" not in d
    assert d["n_valid_cells"] == 1234


@pytest.mark.asyncio
@pytest.mark.parametrize("product,expected,absent", [
    ("DEM3DEP_10M", "usgs_3dep", "copernicus_glo30"),
    ("GLO30", "copernicus_glo30", "usgs_3dep"),
])
async def test_citations_follow_dem_product(tmp_path, product, expected, absent):
    _, cites = await _run(tmp_path, full_raises=True, dem_product=product)
    assert expected in cites
    assert absent not in cites


def test_map_source_caption_derives_from_dem_product():
    from ai_hydro.mcp.tools_analysis import _dem_map_source

    assert "GLO30" in _dem_map_source("GLO30")
    assert "py3dep" not in _dem_map_source("GLO30")
    assert "not reported" in _dem_map_source(None)


@pytest.mark.asyncio
async def test_exception_with_empty_message_is_still_flagged(tmp_path):
    out, _ = await _run(tmp_path, full_raises=True, dem_product="GLO30", exc=AssertionError())
    d = out["data"]
    assert d["twi_method"] == "fallback" and "AssertionError" in d["twi_method_reason"]
    assert "_visualization_warning" in out
