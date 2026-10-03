"""D4 S2: precipitation provenance reaches the sealed row's evidence body.

capture_result_evidence drops "_"-prefixed keys, so the underscore
``_precipitation`` block cannot carry provenance into a claim's evidence
fingerprint; the non-underscore mirrors must. A real post_run is used.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

SQUARE = {"type": "Polygon", "coordinates": [[
    [-77.5, 39.2], [-77.4, 39.2], [-77.4, 39.3], [-77.5, 39.3], [-77.5, 39.2]]]}


@pytest.fixture
def sealed(tmp_path, monkeypatch):
    from ai_hydro.session import chat_binding, store
    from ai_hydro.session.chat_binding import ChatBindingStore
    from ai_hydro.session.store import HydroSession
    from ai_hydro.mcp.enforcement import post_run
    import aihydro_watershed.signatures.signatures as impl

    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    HydroSession("d4-seal").save()
    days = pd.date_range("2000-01-01", periods=800, freq="D")
    q_cms = pd.Series(np.random.default_rng(3).lognormal(1.0, 0.8, size=800), index=days)

    def run(p_series):
        monkeypatch.setattr(impl, "_fetch_precipitation_data_bygeom", lambda *a, **k: p_series)
        res = impl.extract_hydrological_signatures(
            gauge_id=None, watershed_geojson=SQUARE, area_km2=250.0, q_cms_series=q_cms)
        out = post_run("extract_hydrological_signatures", "d4-seal",
                       {"data": dict(res.data), "meta": {"version": "x"}})
        return (HydroSession.load("d4-seal").get("_run_log") or {})[out["_run_id"]], days
    return run


def test_used_precipitation_provenance_is_in_sealed_evidence(sealed):
    days = pd.date_range("2000-01-01", periods=800, freq="D")
    p = pd.Series(np.random.default_rng(1).gamma(0.5, 8.0, 800), index=days)
    p.attrs["product"] = "GRIDMET_PRECIP"
    row, _ = sealed(p)
    ev = row["evidence"]["data"]
    assert ev["precipitation_status"] == "used"
    assert ev["precipitation_product"] == "GRIDMET_PRECIP"
    assert ev["precipitation_digest"].startswith("sha256:")
    assert "_precipitation" not in ev            # confirms the underscore block is dropped
    assert ev["runoff_ratio"] is not None


def test_rejected_precipitation_is_visible_in_sealed_evidence(sealed):
    days = pd.date_range("2000-01-01", periods=800, freq="D")
    row, _ = sealed(pd.Series(4.3e32, index=days))
    ev = row["evidence"]["data"]
    assert ev["runoff_ratio"] is None
    assert ev["precipitation_status"] == "rejected"
    assert ev["precipitation_digest"].startswith("sha256:")
