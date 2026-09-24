"""
Data-product fixes found while delineating culvert-scale catchments (INDOT
SPR-4926): TR-55 pasture CN, NLCD 2021 default, SSURGO soil attributes and
recorded Kw, and the small-catchment delineation method on the MCP surface.

The heavy lifting lives in aihydro-watershed (its own known-answer tests);
these tests pin the aihydro-tools side: shims, MCP signatures, session wiring.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from shapely.geometry import box, mapping

INDIANA_BOX = box(-86.93, 40.42, -86.92, 40.43)


@pytest.fixture
def tmp_sessions(tmp_path, monkeypatch):
    from ai_hydro.session import store

    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setattr(store, "SESSIONS_DIR", d)
    monkeypatch.setattr(store, "_SESSIONS_DIR", d)
    return d


def _new_session(session_id: str):
    from ai_hydro.session import HydroSession

    s = HydroSession(session_id=session_id)
    s.save()
    return s


# ── Fix 1: pasture CN through the tools shim ──────────────────────────────────

def test_shim_pasture_cn_is_tr55_pasture_good():
    from ai_hydro.analysis.curve_number import _create_cn_lookup_table

    t = _create_cn_lookup_table()
    assert tuple(t[(81, g)] for g in (1, 2, 3, 4)) == (39, 61, 74, 80)
    assert tuple(t[(82, g)] for g in (1, 2, 3, 4)) == (67, 78, 85, 89)


# ── Fix 4: NLCD default year ──────────────────────────────────────────────────

def test_landcover_defaults_to_nlcd_2021():
    from ai_hydro.data import landcover

    assert landcover.NLCD_LATEST_YEAR == 2021
    for fn in (landcover.fetch_lulc_data, landcover._fetch_nlcd_direct):
        assert inspect.signature(fn).parameters["year"].default == 2021


def test_create_cn_grid_signature():
    from ai_hydro.mcp.tools_analysis import create_cn_grid

    params = inspect.signature(create_cn_grid).parameters
    assert params["year"].default == 2021
    assert params["dual_hsg"].default == "drained"


# ── Fixes 2/3: SSURGO soil attributes tool + RUSLE K ─────────────────────────

FAKE_SUMMARY = {
    "kw_mean": 0.30, "kw_si_mean": 0.30 * 0.1317,
    "sand_pct_mean": 20.0, "silt_pct_mean": 60.0, "clay_pct_mean": 20.0,
    "hsg_drained_pct": {"B": 100.0}, "hsg_undrained_pct": {"B": 70.0, "D": 30.0},
    "pct_dual_hsg": 30.0, "n_map_units": 3, "n_cells": 100,
    "soil_coverage": 1.0, "hsg_coverage": 1.0, "resolution_m": 10.0,
    "product": "SSURGO_GNATSGO", "source": "test", "kw_units": "US customary",
}


def test_ssurgo_tool_registered_and_discoverable():
    from ai_hydro.mcp.app import TOOL_TIERS
    from ai_hydro.mcp.tools_discovery import _DOMAIN_PREFIXES

    assert TOOL_TIERS["fetch_soil_attributes_ssurgo"] == 2
    assert any("fetch_soil_attributes_ssurgo".startswith(p)
               for p in _DOMAIN_PREFIXES["watershed"])


def test_ssurgo_tool_writes_session_soil_slot(tmp_sessions, monkeypatch):
    from aihydro_watershed.terrain import ssurgo
    from ai_hydro.mcp.tools_analysis import fetch_soil_attributes_ssurgo
    from ai_hydro.session import HydroSession

    monkeypatch.setattr(ssurgo, "ssurgo_catchment_attributes",
                        lambda geom: {"summary": dict(FAKE_SUMMARY), "dataset": None})
    _new_session("ssurgo-test")
    out = asyncio.run(fetch_soil_attributes_ssurgo(
        session_id="ssurgo-test", geometry_geojson=json.dumps(mapping(INDIANA_BOX)),
    ))
    assert "error" not in out, out
    assert out["data"]["kw_mean"] == pytest.approx(0.30)
    assert out["meta"]["product"] == "SSURGO_GNATSGO"
    soil = HydroSession.load("ssurgo-test").get("soil")
    assert soil["data"]["kw_si"] == pytest.approx(0.30 * 0.1317)
    assert soil["data"]["clay"] == pytest.approx(20.0)


def test_ssurgo_tool_refuses_outside_conus(tmp_sessions):
    from ai_hydro.mcp.tools_analysis import fetch_soil_attributes_ssurgo

    _new_session("ssurgo-de")
    out = asyncio.run(fetch_soil_attributes_ssurgo(
        session_id="ssurgo-de", geometry_geojson=json.dumps(mapping(box(8.0, 50.0, 8.1, 50.1))),
    ))
    assert out["code"] == "OUTSIDE_SSURGO_COVERAGE"
    assert out["recovery"] and out["next_tools"]


def test_rusle_uses_recorded_ssurgo_kw(tmp_sessions):
    from ai_hydro.mcp.tools_analysis import compute_soil_loss_rusle

    s = _new_session("rusle-ssurgo")
    s.set("soil", {"data": {"kw_si": 0.04, "sand": 20.0, "silt": 60.0, "clay": 20.0}})
    s.save()
    out = compute_soil_loss_rusle(
        session_id="rusle-ssurgo", r_factor=1000.0, ls_factor=1.0, c_factor=0.2,
    )
    assert out["factors"]["K"] == pytest.approx(0.04)
    assert out["soil_loss_t_ha_yr"] == pytest.approx(1000.0 * 0.04 * 1.0 * 0.2, rel=1e-3)
    assert any("SSURGO" in n for n in out["derivation_notes"])


def test_rusle_explicit_texture_still_beats_recorded_kw(tmp_sessions):
    from ai_hydro.analysis.erosion import k_factor_epic
    from ai_hydro.mcp.tools_analysis import compute_soil_loss_rusle

    s = _new_session("rusle-texture")
    s.set("soil", {"data": {"kw_si": 0.04}})
    s.save()
    out = compute_soil_loss_rusle(
        session_id="rusle-texture", r_factor=1000.0, ls_factor=1.0, c_factor=0.2,
        sand_pct=40.0, silt_pct=40.0, clay_pct=20.0,
    )
    assert out["factors"]["K"] == pytest.approx(k_factor_epic(40.0, 40.0, 20.0, 1.0), abs=1e-5)


def test_rusle_session_texture_path_now_reachable(tmp_sessions):
    """The session soil slot used to be read as `session.soil`, which does not
    exist, so the texture branch never fired. It now reads session.get('soil')."""
    from ai_hydro.analysis.erosion import k_factor_epic
    from ai_hydro.mcp.tools_analysis import compute_soil_loss_rusle

    s = _new_session("rusle-sess-texture")
    s.set("soil", {"data": {"sand": 40.0, "silt": 40.0, "clay": 20.0}})
    s.save()
    out = compute_soil_loss_rusle(
        session_id="rusle-sess-texture", r_factor=1000.0, ls_factor=1.0, c_factor=0.2,
    )
    assert out["factors"]["K"] == pytest.approx(k_factor_epic(40.0, 40.0, 20.0, 1.0), abs=1e-5)


# ── Fix 5: small_catchment on the delineation MCP tool ────────────────────────

@pytest.mark.parametrize("given", ["small_catchment", "3dep", "SMALL_CATCHMENT"])
def test_delineation_tool_accepts_small_catchment(given, tmp_sessions, monkeypatch):
    import ai_hydro.analysis.delineation as deln
    from ai_hydro.mcp.tools_analysis import delineate_watershed_from_point

    seen = {}

    def fake(lat, lon, **kw):
        seen.update(kw)
        raise RuntimeError("stop after method check")

    monkeypatch.setattr(deln, "delineate_from_point", fake)
    out = delineate_watershed_from_point(40.3530, -86.8250, method=given, name="cv-test")
    assert seen.get("method") == "small_catchment"
    assert "stop after method check" in json.dumps(out)


def test_delineation_tool_error_lists_small_catchment(tmp_sessions):
    from ai_hydro.mcp.tools_analysis import delineate_watershed_from_point

    out = delineate_watershed_from_point(40.0, -86.0, method="bogus", name="bad-method")
    assert "small_catchment" in json.dumps(out)


def test_round_area_keeps_small_catchments_visible():
    from ai_hydro.mcp.tools_analysis import _round_area

    assert _round_area(0.0333) == 0.033
    assert _round_area(1234.56) == 1234.6
