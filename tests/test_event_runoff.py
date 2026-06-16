"""Known-answer tests for event rainfall-runoff kernels.

Pure compute — no network, no session. Each expected value is re-derived
independently from the published formula; the unit-hydrograph volume-conservation
property is asserted by numerically integrating the discrete hydrograph.
"""
import math

import numpy as np
import pytest

from ai_hydro.analysis.event_runoff import (
    storage_s_mm, scs_cn_runoff, time_of_concentration_kirpich,
    scs_triangular_hydrograph, design_hydrograph,
)


def test_storage_s_known():
    """CN=80 → S = 25400/80 − 254 = 63.5 mm."""
    assert storage_s_mm(80) == pytest.approx(63.5, abs=1e-9)


def test_scs_cn_runoff_known():
    """P=100, CN=80 → S=63.5, Ia=12.7, Q=(100−12.7)²/(100+50.8)=50.54 mm."""
    s = 25400 / 80 - 254
    ia = 0.2 * s
    expected = (100 - ia) ** 2 / (100 + 0.8 * s)
    assert scs_cn_runoff(100, 80) == pytest.approx(expected, rel=1e-9)
    assert scs_cn_runoff(100, 80) == pytest.approx(50.54, abs=0.05)


def test_scs_cn_runoff_below_initial_abstraction():
    """P below 0.2S → no runoff."""
    # CN=80 → S=63.5 → Ia=12.7; P=10 < Ia → Q=0.
    assert scs_cn_runoff(10, 80) == 0.0


def test_scs_cn_runoff_increases_with_cn():
    assert scs_cn_runoff(50, 90) > scs_cn_runoff(50, 60)


def test_invalid_cn_raises():
    with pytest.raises(ValueError):
        storage_s_mm(0)
    with pytest.raises(ValueError):
        storage_s_mm(120)


def test_kirpich_tc_known():
    """L=10 km, S=0.01 → Tc = 0.0663·10^0.77·0.01^−0.385, re-derived."""
    expected = 0.0663 * 10 ** 0.77 * 0.01 ** -0.385
    assert time_of_concentration_kirpich(10, 0.01) == pytest.approx(expected, rel=1e-9)


def test_kirpich_increases_with_length_decreases_with_slope():
    base = time_of_concentration_kirpich(10, 0.01)
    assert time_of_concentration_kirpich(20, 0.01) > base   # longer path, slower
    assert time_of_concentration_kirpich(10, 0.05) < base   # steeper, faster


def test_uh_peak_discharge_known():
    """Qp = 0.208·A·Q/Tp. For A=50, Q=30, Tc such that Tp known → check Qp."""
    out = scs_triangular_hydrograph(area_km2=50.0, tc_hr=5.0, runoff_mm=30.0)
    duration = 0.133 * 5.0
    tp = 0.6 * 5.0 + duration / 2.0
    expected_qp = 0.208 * 50.0 * 30.0 / tp
    assert out["peak_discharge_cms"] == pytest.approx(expected_qp, rel=1e-3)
    assert out["base_time_hr"] == pytest.approx(2.67 * tp, rel=1e-3)


def test_uh_volume_conservation():
    """Integrating the hydrograph must recover the runoff volume (±2%)."""
    area, tc, runoff = 80.0, 6.0, 40.0
    out = scs_triangular_hydrograph(area, tc, runoff, dt_hr=0.05)
    t = np.array(out["time_hr"])
    q = np.array(out["discharge_cms"])
    integrated_m3 = np.trapezoid(q, t * 3600.0)        # ∫ Q dt, seconds
    expected_m3 = runoff / 1000.0 * area * 1e6
    assert integrated_m3 == pytest.approx(expected_m3, rel=0.02)
    assert out["runoff_volume_m3"] == pytest.approx(expected_m3, rel=1e-6)


def test_design_hydrograph_end_to_end():
    """Full chain: rainfall → CN runoff → triangular hydrograph."""
    out = design_hydrograph(area_km2=120.0, tc_hr=4.0, cn=75, rainfall_mm=90.0)
    assert out["runoff_mm"] == pytest.approx(scs_cn_runoff(90, 75), abs=1e-2)  # kernel rounds to 3 dp
    assert 0 < out["runoff_coefficient"] < 1
    assert out["peak_discharge_cms"] > 0
    assert out["curve_number"] == 75.0


def test_design_hydrograph_no_runoff_for_tiny_storm():
    """A storm below initial abstraction yields a zero hydrograph, no crash."""
    out = design_hydrograph(area_km2=50.0, tc_hr=3.0, cn=70, rainfall_mm=5.0)
    assert out["runoff_mm"] == 0.0
    assert out["peak_discharge_cms"] == 0.0
