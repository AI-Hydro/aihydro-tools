"""Science Wave 3, PR-4b: the packaged CAMELS-US v1.2 reference, the stdlib baseflow port, and the readers of
slot health. Offline."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

import ai_hydro.mcp  # noqa: F401  (registers every tool)
from ai_hydro.science import camels_reference as cr
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession

PKG = cr.PACKAGED_DIR


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)
    cr._packaged_tables.cache_clear()
    yield
    cr._packaged_tables.cache_clear()


# ------------------------------------------------------------------ the packaged tables

def test_packaged_files_match_the_published_md5_and_ship_attribution():
    assert (PKG / "ATTRIBUTION.md").is_file()
    text = (PKG / "ATTRIBUTION.md").read_text()
    for needle in ("CC-BY 4.0", "15529996", "10.5065/D6MW2F4D", "Addor", "10.5194/hess-21-5293-2017"):
        assert needle in text, needle
    for name in cr.PACKAGED_FILES.values():
        prov = json.loads((PKG / (name + ".provenance.json")).read_text())
        assert prov["md5_published"] == prov["md5_observed"]
        assert hashlib.md5((PKG / name).read_bytes()).hexdigest() == prov["md5_published"]
        assert hashlib.sha256((PKG / name).read_bytes()).hexdigest() == prov["sha256"]
        assert (PKG / name).stat().st_size == prov["size_bytes"]
        assert prov["source"].startswith("https://zenodo.org/api/records/15529996/")


def test_packaged_row_has_hydro_area_and_climate_context_for_the_known_gauges():
    row, source, problem = cr.packaged_row("01013500")
    assert problem is None and source["kind"] == "packaged" and source["license"] == "CC-BY-4.0"
    assert float(row["slope_fdc"]) == pytest.approx(1.52821853538976)
    assert float(row["baseflow_index"]) == pytest.approx(0.585225955779508)
    assert float(row["area_gages2"]) == pytest.approx(2252.7)
    assert float(row["frac_snow"]) == pytest.approx(0.313440357191799)
    row2, _, _ = cr.packaged_row("1031500")                      # 7 digits: zero-padded like CAMELS
    assert float(row2["baseflow_index"]) == pytest.approx(0.445090526012048)
    assert float(row2["slope_fdc"]) == pytest.approx(1.49401920331253)


def test_all_671_basins_are_present_and_unknown_gauges_are_not_invented():
    tables, _, problem = cr._packaged_tables()
    assert problem is None and len(tables) == 671
    assert all({"q_mean", "area_gages2", "frac_snow", "aridity", "p_seasonality"} <= set(r) for r in tables.values())
    row, _, why = cr.packaged_row("09999999")
    assert row is None and "not one of the CAMELS-US v1.2 basins" in why
    assert cr.packaged_row("abc")[0] is None and cr.packaged_row(None)[0] is None


def test_a_modified_packaged_file_makes_the_reference_unavailable_never_silently_used(monkeypatch, tmp_path):
    copy = tmp_path / "camels"
    shutil.copytree(PKG, copy)
    hydro = copy / "camels_hydro.txt"
    hydro.write_text(hydro.read_text().replace("01013500;1.69915450753356", "01013500;9.99999"))
    monkeypatch.setattr(cr, "PACKAGED_DIR", copy)
    cr._packaged_tables.cache_clear()
    row, _, problem = cr.packaged_row("01013500")
    assert row is None and "md5" in problem and "camels_hydro.txt" in problem
    blk, rc = cr.build_camels_reference({"q_mean": 1.7}, "01013500", 2253.0, "1989-10-01", "2009-09-30", None)
    assert blk["available"] is False and rc is None and blk["fix"] and "fetch_camels_us" in blk["fix"]


def test_packaged_hydro_table_matches_the_evidence_copy_when_present():
    evidence = Path("/Users/mgalib/Documents/AI-Hydro/docs/vision-2040/evidence/reference/camels_hydro.txt")
    if not evidence.is_file():
        pytest.skip("workspace evidence copy not present")
    assert evidence.read_bytes() == (PKG / "camels_hydro.txt").read_bytes()


def _pyproject_text():
    return (Path(cr.__file__).resolve().parents[2] / "pyproject.toml").read_text()


def test_package_data_ships_the_reference_directory():
    assert '"ai_hydro.knowledge" = ["*.yaml", "*.json", "library_refs/*.json", "reference/camels_us_v1_2/*"]' in _pyproject_text()


# ------------------------------------------------------------------ pins

def test_sibling_pins_are_the_t1p_minimums():
    text = _pyproject_text()
    for pin in ('"aihydro-core[contracts]>=0.2.11,<0.3"', '"aihydro-data>=0.2.6,<0.3"',
                '"aihydro-watershed>=0.3,<0.4"'):
        assert pin in text, pin


# ------------------------------------------------------------------ capsule stdlib port == watershed kernel

def _wk():
    import warnings
    from aihydro_watershed.signatures.baseflow import lyne_hollick_ladson2013
    def run(x):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return lyne_hollick_ladson2013(np.asarray(x, dtype=float))
    return run


def _bfi_ws(x):
    x = np.asarray(x, dtype=float)
    bf = np.clip(_wk()(x), 0, x)
    ok = np.isfinite(bf) & np.isfinite(x)
    return float(bf[ok].sum() / x[ok].sum())


@pytest.mark.parametrize("seed,n", [(0, 400), (1, 2000), (2, 7305)])
def test_capsule_port_equals_the_watershed_kernel(seed, n):
    from ai_hydro.capsule.data_artifacts import lyne_hollick_bfi
    rng = np.random.default_rng(seed)
    q = np.abs(rng.lognormal(0.0, 1.0, n)) + 0.05
    assert lyne_hollick_bfi(list(q)) == pytest.approx(_bfi_ws(q), abs=1e-12)


def test_capsule_port_equals_the_watershed_kernel_across_gaps_and_short_runs():
    from ai_hydro.capsule.data_artifacts import lyne_hollick_bfi
    rng = np.random.default_rng(4)
    q = np.abs(rng.lognormal(0.0, 1.0, 1200)) + 0.05
    q[300] = np.nan                                   # splits into runs of 300 and 899
    q[320:340] = np.nan                               # a run of 20 days (<= 30): takes no part in either sum
    q[900:905] = np.nan
    assert lyne_hollick_bfi([None if np.isnan(v) else float(v) for v in q]) == pytest.approx(_bfi_ws(q), abs=1e-12)


def test_capsule_port_reads_dates_so_dropped_days_are_gaps_not_splices():
    from ai_hydro.capsule.data_artifacts import lyne_hollick_bfi
    import pandas as pd
    rng = np.random.default_rng(6)
    idx = pd.date_range("2000-01-01", periods=900, freq="D")
    q = np.abs(rng.lognormal(0.0, 1.0, 900)) + 0.05
    keep = np.ones(900, bool)
    keep[400] = False                                  # a day absent from the rows, as the data layer drops nulls
    dates = [f"{d:%Y-%m-%d}" for d in idx[keep]]
    full = q.copy()
    full[400] = np.nan
    assert lyne_hollick_bfi(list(q[keep]), dates=dates) == pytest.approx(_bfi_ws(full), abs=1e-12)
    spliced = lyne_hollick_bfi(list(q[keep]))          # without dates the rows are read as consecutive days
    assert spliced != pytest.approx(_bfi_ws(full), abs=1e-6)


def test_capsule_port_edge_cases():
    from ai_hydro.capsule.data_artifacts import lyne_hollick_bfi
    import math
    assert math.isnan(lyne_hollick_bfi([1.0] * 30))                 # <= n_reflect days: no baseflow, no raise
    assert math.isnan(lyne_hollick_bfi([None] * 100))
    assert math.isnan(lyne_hollick_bfi([0.0] * 100))                # sum(q) == 0
    assert lyne_hollick_bfi([2.0] * 500) == pytest.approx(_bfi_ws([2.0] * 500), abs=1e-12)


# ------------------------------------------------------------------ readers of computed() use slot health

def _degraded_session():
    s = HydroSession("w3-readers")
    s.set("watershed", {"data": {"area_km2": 5.0}, "meta": {"computed_at": "2026-10-01T00:00:00+00:00"}})
    s.set("signatures", {"data": {"q_mean": 1.0, "baseflow_index": 0.5},          # no flow_quantile_convention
                         "meta": {"computed_at": "2026-10-01T00:00:00+00:00"}})
    s.site_id = "01013500"
    s.save()
    return s


def test_get_session_raw_state_omits_degraded_slots_and_lists_them():
    from ai_hydro.mcp.tools_session import get_session_raw_state
    _degraded_session()
    out = get_session_raw_state(session_id="w3-readers")
    assert "signatures" not in out["slots"] and "signatures" not in out["computed"]
    assert out["computed"] == ["watershed"]
    assert [d["slot"] for d in out["degraded"]] == ["signatures"] and out["degraded"][0]["health"] == "legacy_convention"


def test_research_md_is_a_stub_even_for_a_degraded_session(tmp_path):
    """T-R1: the digest moved to the context resource (tests/test_study_context.py covers the degraded slot)."""
    s = _degraded_session()
    s.workspace_dir = str(tmp_path)
    s.write_research_context()
    from ai_hydro.registry.paths import rules_dir
    text = (rules_dir(str(tmp_path)) / "research.md").read_text()
    assert "no longer updated" in text and "signatures" not in text


def test_sync_reminder_counts_only_healthy_analyses():
    from ai_hydro.mcp.helpers import _sync_reminder
    s = _degraded_session()                    # watershed ok, signatures degraded
    assert _sync_reminder("w3-readers") is None                       # one healthy analysis: no reminder yet
    s.set("streamflow", {"data": {"n_days": 5}, "meta": {"computed_at": "2026-10-01T00:00:00+00:00"}})
    s.save()
    assert "[2 analyses complete" in _sync_reminder("w3-readers")     # the degraded one is not counted


# ------------------------------------------------------------------ review fixes: pinned hashes, attribution, slot notes

PINNED_SHA256 = {
    "camels_hydro.txt": "7dcae3ea8c28e6485fc9e39b3c5e8bb509d6354ebe9b0b98873ec47fa36e3c41",
    "camels_clim.txt": "6a3a80256afbaea13f59f279c174d3c010156b826ffb53a4cfb5508814404f5f",
    "camels_topo.txt": "b64ca9923bcaccf21dde33137903797919e8d6732edd7849f8534e0ddcbec8e8",
}


def test_packaged_files_are_pinned_by_sha256_in_the_test_not_only_in_the_adjacent_json():
    for name, want in PINNED_SHA256.items():
        assert hashlib.sha256((PKG / name).read_bytes()).hexdigest() == want, name


def test_gitattributes_keeps_the_reference_bytes_untouched():
    text = (Path(cr.__file__).resolve().parents[2] / ".gitattributes").read_text()
    assert "ai_hydro/knowledge/reference/** -text" in text.splitlines()


def test_attribution_names_newman_and_addor():
    text = (PKG / "ATTRIBUTION.md").read_text()
    assert "Newman" in text and "19, 209" in text and "Addor" in text


def test_multi_gauge_or_empty_slot_is_reported_as_ignored_not_silently_skipped():
    s = HydroSession("w3-slot")
    s.set("camels", {"data": {"mode": "multi", "gauges": {}}, "meta": {}})
    blk, _ = cr.build_camels_reference({"q_mean": 1.7, "streamflow_start": "1989-10-01", "streamflow_end": "2009-09-30"},
                                       "01013500", 2253.0, "1989-10-01", "2009-09-30", s)
    assert blk["available"] and blk["source"]["kind"] == "packaged"
    assert "multi-gauge" in blk["session_slot_ignored"]
