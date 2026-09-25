"""The session→modelling bridge labels data with what was served.

`extract_basin_data` used to label every dataset "CAMELS+GridMET" or
"USGS+GridMET" regardless of the forcing product actually fetched.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("aihydro_modelling")

from ai_hydro.modelling import metrics


def _session():
    return SimpleNamespace(
        watershed={"data": {"area_km2": 100.0, "gauge_lat": 39.2, "gauge_lon": -77.4}},
        camels=None,
    )


@pytest.fixture
def slots(monkeypatch):
    forcing = {"dates": ["2000-01-01"], "prcp_mm": [1.0]}

    def load(session, slot, gauge_id):
        if slot == "streamflow":
            return {"dates": ["2000-01-01"], "q_cms": [1.0]}
        return forcing

    monkeypatch.setattr(metrics, "_load_full_data", load)
    monkeypatch.setattr(metrics, "fetch_camels_streamflow", lambda *a, **k: {})
    return forcing


def test_label_uses_served_forcing_product(slots, tmp_path):
    slots["product"] = "DAYMET_PRECIP | ERA5L_TMAX"

    data, label = metrics.extract_basin_data(_session(), "01013500", tmp_path)

    assert label == "USGS+DAYMET_PRECIP | ERA5L_TMAX"
    assert data.data_source == label
    assert "GridMET" not in label


def test_missing_forcing_product_is_labelled_unknown(slots, tmp_path):
    _, label = metrics.extract_basin_data(_session(), "01013500", tmp_path)
    assert label == "USGS+unknown-forcing"


def test_camels_streamflow_keeps_camels_label(slots, monkeypatch, tmp_path):
    slots["product"] = "GRIDMET_PRECIP"
    monkeypatch.setattr(metrics, "fetch_camels_streamflow", lambda *a, **k: {"2000-01-01": 0.5})

    _, label = metrics.extract_basin_data(_session(), "01013500", tmp_path)

    assert label == "CAMELS+GRIDMET_PRECIP"
