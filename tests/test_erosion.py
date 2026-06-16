"""Known-answer tests for RUSLE kernels.

Pure compute — no network, no session. Each factor's expected value is re-derived
independently (plain arithmetic) from the published formula, so the test catches
transcription errors in the kernel.
"""
import math

import numpy as np
import pytest

from ai_hydro.analysis.erosion import (
    rusle, modified_fournier_index, r_factor_renard_freimund,
    k_factor_epic, ls_factor_wischmeier, c_factor_from_landcover,
    soil_loss_severity, _DEFAULT_C_FACTORS,
)


def test_rusle_product_exact():
    """A = R·K·LS·C·P — the backlog's known-answer example."""
    assert rusle(200, 0.3, 1.2, 0.1, 1.0) == pytest.approx(7.2, abs=1e-9)


def test_rusle_broadcasts_over_arrays():
    R = np.array([100.0, 200.0])
    out = rusle(R, 0.3, 1.2, 0.1, 1.0)
    assert out.shape == (2,)
    assert out[1] == pytest.approx(7.2, abs=1e-9)


def test_modified_fournier_index_known():
    """Equal monthly 50 mm → F = Σ(50²)/600 = 12·2500/600 = 50.0 exactly."""
    monthly = [50.0] * 12
    assert modified_fournier_index(monthly) == pytest.approx(50.0, abs=1e-9)


def test_mfi_requires_twelve_months():
    with pytest.raises(ValueError):
        modified_fournier_index([10.0] * 6)


def test_r_factor_renard_freimund_low_branch():
    """F = 50 (≤ 55) → R = 0.07397·50^1.847, re-derived independently."""
    monthly = [50.0] * 12
    expected = 0.07397 * 50.0 ** 1.847
    assert r_factor_renard_freimund(monthly) == pytest.approx(expected, rel=1e-9)


def test_r_factor_renard_freimund_high_branch():
    """Large monthly totals push F > 55 → quadratic branch."""
    monthly = [120.0] * 12          # F = 120 > 55
    F = 120.0
    expected = 95.77 - 6.081 * F + 0.4770 * F ** 2
    assert r_factor_renard_freimund(monthly) == pytest.approx(expected, rel=1e-9)


def test_k_factor_epic_known():
    """EPIC K for SAN=40, SIL=40, CLA=20, OC=2 — re-derived independently, → SI."""
    san, sil, cla, c = 40.0, 40.0, 20.0, 2.0
    sn1 = 1 - san / 100
    f_csand = 0.2 + 0.3 * math.exp(-0.0256 * san * (1 - sil / 100))
    f_clsi = (sil / (cla + sil)) ** 0.3
    f_orgc = 1 - (0.25 * c) / (c + math.exp(3.72 - 2.95 * c))
    f_hisand = 1 - (0.7 * sn1) / (sn1 + math.exp(-5.51 + 22.9 * sn1))
    expected = f_csand * f_clsi * f_orgc * f_hisand * 0.1317
    assert k_factor_epic(san, sil, cla, c) == pytest.approx(expected, rel=1e-9)
    # Physical sanity: K in SI is a small positive number, typically 0.01–0.07.
    assert 0.005 < k_factor_epic(san, sil, cla, c) < 0.1


def test_ls_factor_wischmeier_known():
    """LS for slope 9% (m=0.5), λ=50 m — re-derived independently."""
    slope_pct, lam = 9.0, 50.0
    theta = math.atan(slope_pct / 100)
    sin_t = math.sin(theta)
    expected = (lam / 22.13) ** 0.5 * (65.41 * sin_t ** 2 + 4.56 * sin_t + 0.065)
    assert ls_factor_wischmeier(slope_pct, lam) == pytest.approx(expected, rel=1e-9)


def test_ls_factor_increases_with_slope():
    flat = ls_factor_wischmeier(1.0, 50.0)
    steep = ls_factor_wischmeier(20.0, 50.0)
    assert steep > flat


def test_c_factor_lookup_exact_and_substring():
    assert c_factor_from_landcover("forest") == _DEFAULT_C_FACTORS["forest"]
    assert c_factor_from_landcover("Deciduous Forest") == _DEFAULT_C_FACTORS["forest"]
    assert c_factor_from_landcover("water") == 0.0
    # Unknown class → documented fallback 0.1.
    assert c_factor_from_landcover("unobtanium") == pytest.approx(0.1)


def test_c_factor_override_table():
    custom = {"cropland": 0.5}
    assert c_factor_from_landcover("cropland", table=custom) == 0.5


def test_full_rusle_chain_plausible():
    """End-to-end: derive all factors, multiply → a physically plausible A."""
    monthly = [80.0] * 12
    R = r_factor_renard_freimund(monthly)
    K = k_factor_epic(45, 35, 20, 1.5)
    LS = ls_factor_wischmeier(8.0, 60.0)
    C = c_factor_from_landcover("cropland")
    A = rusle(R, K, LS, C, 1.0)
    assert A > 0
    assert soil_loss_severity(A) in {"low", "moderate", "high", "severe"}


def test_severity_bands():
    assert soil_loss_severity(1.0) == "low"
    assert soil_loss_severity(5.0) == "moderate"
    assert soil_loss_severity(25.0) == "high"
    assert soil_loss_severity(80.0) == "severe"
