"""Tests for inundation discharge drivers."""
from __future__ import annotations

import pytest

from ai_hydro.analysis.inundation_drivers import resolve_inundation_discharge
from ai_hydro.session import HydroSession


def test_resolve_explicit_discharge():
    session = HydroSession("drv-test")
    q, src, err = resolve_inundation_discharge(session, discharge_m3s=120.0)
    assert err is None
    assert q == 120.0
    assert src == "explicit"


def test_resolve_return_period_from_session():
    session = HydroSession("drv-rp")
    session.set(
        "flood_frequency",
        {
            "data": {
                "return_levels": [
                    {"return_period": 10, "value": 400.0},
                    {"return_period": 100, "value": 900.0},
                ]
            }
        },
    )
    q, src, err = resolve_inundation_discharge(session, return_period=100)
    assert err is None
    assert q == 900.0
    assert src == "return_period_100"


def test_resolve_design_peak():
    session = HydroSession("drv-dh")
    session.set("design_hydrograph", {"data": {"peak_discharge_cms": 555.0}})
    q, src, err = resolve_inundation_discharge(session, use_design_peak=True)
    assert err is None
    assert q == 555.0
    assert src == "design_hydrograph_peak"


def test_resolve_session_peak_from_q_max():
    session = HydroSession("drv-sf")
    session.set("streamflow", {"data": {"q_max_cms": 777.0}})
    q, src, err = resolve_inundation_discharge(session, use_session_peak=True)
    assert err is None
    assert q == 777.0


def test_resolve_session_peak_from_series():
    session = HydroSession("drv-series")
    session.set("streamflow", {"data": {"q_cms": [10.0, 20.0, 150.0, 5.0]}})
    q, src, err = resolve_inundation_discharge(session, use_session_peak=True)
    assert err is None
    assert q == 150.0


def test_missing_discharge_returns_error():
    session = HydroSession("drv-empty")
    q, src, err = resolve_inundation_discharge(session)
    assert q is None
    assert err is not None
    assert err["code"] == "MISSING_DISCHARGE"
