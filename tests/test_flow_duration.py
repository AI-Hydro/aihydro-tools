"""Known-answer tests for the flow-duration / low-flow kernels.

Pure compute — no network, no session. Validates the FDC percentile convention,
the slope, and 7Q10 against hand-computable expectations.
"""
import numpy as np
import pandas as pd
import pytest

from ai_hydro.analysis.flow_duration import flow_duration_curve, seven_q10


def test_fdc_percentiles_on_ramp():
    """A 1..100 ramp has known quantiles under the exceedance convention."""
    q = list(range(1, 101))
    out = flow_duration_curve(q)
    pf = out["percentile_flows"]
    # Q50 (median) ≈ 50.5; Q95 (exceeded 95% of time → small) ≈ 5.95;
    # Q5 (exceeded 5% → large) ≈ 95.05.
    assert pf["Q50"] == pytest.approx(50.5, abs=1.0)
    assert pf["Q95"] == pytest.approx(5.95, abs=1.0)
    assert pf["Q5"] == pytest.approx(95.05, abs=1.0)
    # Convention sanity: high-flow Q5 must exceed low-flow Q95.
    assert pf["Q5"] > pf["Q50"] > pf["Q95"]
    assert out["n_days"] == 100


def test_fdc_exceedance_array_shape_and_order():
    q = np.random.default_rng(0).gamma(2.0, 5.0, size=500)
    out = flow_duration_curve(q)
    assert len(out["exceedance_prob"]) == 500
    assert len(out["sorted_flows"]) == 500
    # Flows sorted descending; exceedance ascending.
    sf = out["sorted_flows"]
    assert sf[0] >= sf[-1]
    ep = out["exceedance_prob"]
    assert ep[0] < ep[-1]
    assert 0 < ep[0] < ep[-1] < 1


def test_fdc_slope_zero_for_constant_series():
    """A constant series has a flat FDC → slope 0."""
    q = [42.0] * 200
    out = flow_duration_curve(q)
    assert out["slope_fdc"] == pytest.approx(0.0, abs=1e-9)


def test_fdc_slope_positive_for_variable_series():
    """A variable (log-spread) series has a non-trivial positive slope."""
    q = np.exp(np.linspace(0, 3, 300))  # spans ~1 to ~20, monotone spread
    out = flow_duration_curve(q)
    assert out["slope_fdc"] > 0


def test_fdc_rejects_too_few_points():
    with pytest.raises(ValueError):
        flow_duration_curve([1.0])


def test_seven_q10_constant_series():
    """For a constant series the annual 7-day min is the constant → 7Q10 = const."""
    dates = pd.date_range("2000-01-01", periods=365 * 12, freq="D")
    q = [10.0] * len(dates)
    out = seven_q10(q, dates=dates)
    assert out["n_years"] >= 10
    assert out["seven_q10"] == pytest.approx(10.0, abs=0.5)


def test_seven_q10_needs_enough_years():
    """Fewer than 10 years → 7Q10 None with a note, not a crash."""
    dates = pd.date_range("2000-01-01", periods=365 * 3, freq="D")
    q = list(np.random.default_rng(1).gamma(2, 3, size=len(dates)))
    out = seven_q10(q, dates=dates)
    assert out["seven_q10"] is None
    assert "year" in (out["note"] or "").lower()


def test_seven_q10_below_mean_for_noisy_series():
    """7Q10 (a low-flow design stat) should sit below the series mean."""
    rng = np.random.default_rng(2)
    dates = pd.date_range("1990-01-01", periods=365 * 20, freq="D")
    # Seasonal baseflow + noise, strictly positive.
    base = 20 + 10 * np.sin(np.arange(len(dates)) * 2 * np.pi / 365)
    q = np.clip(base + rng.normal(0, 3, len(dates)), 0.1, None)
    out = seven_q10(q, dates=dates)
    assert out["seven_q10"] is not None
    assert out["seven_q10"] < float(np.mean(q))
    assert out["seven_q10"] > 0
