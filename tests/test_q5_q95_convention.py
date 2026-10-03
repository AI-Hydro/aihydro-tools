"""Regression for defect P2-D0: q5 / q95 follow CAMELS (Addor et al. 2017, Table 3).

CAMELS ``q5`` is the 5% flow quantile (low flow), ``q95`` the 95% flow quantile
(high flow). Before the fix the platform returned them swapped. The signature
engine lives in aihydro-watershed; this pins what the tools layer exposes
(``ai_hydro.analysis.signatures`` shim) on the frozen proof-1 served flow.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from ai_hydro.analysis import signatures as sig
from ai_hydro.data.streamflow import _to_mm_per_day

FIXTURE = Path(__file__).parent / "fixtures" / "served_streamflow_01013500.csv"
# Byte-identical to docs/vision-2040/evidence/e2e-proof-1/capsule/data/served_streamflow_01013500.csv
FIXTURE_SHA256 = "683ccb1619efe82e3c124f8a540bdc9af93269af12efe5c3681d88d9438d359a"
AREA_KM2 = 2258.5163954900077


def test_known_series_q5_low_q95_high():
    idx = pd.date_range("2000-01-01", periods=1000, freq="D")
    out = sig.compute_flow_stats_camels(pd.Series(np.arange(1, 1001, dtype=float), index=idx))
    assert abs(out["q5"] - 50.95) < 1e-9
    assert abs(out["q95"] - 950.05) < 1e-9


def test_proof1_served_flow_matches_camels_orientation():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
    q_cms = pd.read_csv(FIXTURE, parse_dates=["date"]).set_index("date")["q_cms"]
    out = sig.compute_flow_stats_camels(_to_mm_per_day(q_cms, AREA_KM2))
    # Frozen proof-1 record (pre-fix, swapped): q5 6.3566, q95 0.2405.
    assert abs(out["q5"] - 0.2405) < 5e-4
    assert abs(out["q95"] - 6.3566) < 5e-4
    # CAMELS camels_hydro.txt, gauge 01013500: q5 0.241106, q95 6.373021.
    assert abs(out["q5"] / 0.241106126475711 - 1) < 0.01
    assert abs(out["q95"] / 6.37302139711473 - 1) < 0.01


def test_extract_signatures_marker_through_tools_shim(monkeypatch):
    days = pd.date_range("2000-01-01", periods=800, freq="D")
    q_cms = pd.Series(np.random.default_rng(3).lognormal(1.0, 0.8, size=800), index=days)
    monkeypatch.setattr(sig, "_fetch_precipitation_data_bygeom", lambda *a, **k: pd.Series(2.0, index=days))
    import aihydro_watershed.signatures.signatures as impl
    monkeypatch.setattr(impl, "_fetch_precipitation_data_bygeom", lambda *a, **k: pd.Series(2.0, index=days))
    square = {"type": "Polygon", "coordinates": [[
        [-77.5, 39.2], [-77.4, 39.2], [-77.4, 39.3], [-77.5, 39.3], [-77.5, 39.2],
    ]]}
    d = sig.extract_hydrological_signatures(
        gauge_id=None, watershed_geojson=square, area_km2=250.0, q_cms_series=q_cms,
    ).data
    assert d["flow_quantile_convention"] == "camels_nonexceedance_v1"
    assert d["q5"] < d["q_median"] < d["q95"]


def test_marker_reaches_the_sealed_row(tmp_path, monkeypatch):
    """A real post_run seals the marker: evidence.data keeps non-underscore string keys."""
    from ai_hydro.session import chat_binding, store
    from ai_hydro.session.chat_binding import ChatBindingStore
    from ai_hydro.session.store import HydroSession
    from ai_hydro.mcp.enforcement import post_run
    import aihydro_watershed.signatures.signatures as impl

    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    HydroSession("q5q95-seal").save()

    days = pd.date_range("2000-01-01", periods=800, freq="D")
    q_cms = pd.Series(np.random.default_rng(3).lognormal(1.0, 0.8, size=800), index=days)
    monkeypatch.setattr(impl, "_fetch_precipitation_data_bygeom", lambda *a, **k: pd.Series(2.0, index=days))
    square = {"type": "Polygon", "coordinates": [[
        [-77.5, 39.2], [-77.4, 39.2], [-77.4, 39.3], [-77.5, 39.3], [-77.5, 39.2],
    ]]}
    res = impl.extract_hydrological_signatures(
        gauge_id=None, watershed_geojson=square, area_km2=250.0, q_cms_series=q_cms)
    out = post_run("extract_hydrological_signatures", "q5q95-seal", {"data": dict(res.data), "meta": {"version": "x"}})
    row = (HydroSession.load("q5q95-seal").get("_run_log") or {})[out["_run_id"]]
    assert row["evidence"]["data"]["flow_quantile_convention"] == "camels_nonexceedance_v1"
    assert "_flow_quantile_convention" not in row["evidence"]["data"]
