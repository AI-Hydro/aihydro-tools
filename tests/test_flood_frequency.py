"""Known-answer tests for flood-frequency kernels.

Pure compute — no network, no session. The Gumbel method-of-moments path is
textbook-deterministic, so we assert against the closed-form reduced-variate
formula. We also verify annual-maxima extraction and return-level monotonicity.
"""
import numpy as np
import pandas as pd
import pytest

from ai_hydro.analysis.flood_frequency import (
    annual_maxima, flood_frequency, _EULER_GAMMA,
)


def test_annual_maxima_extraction():
    """3 years of daily data with known injected peaks → 3 maxima."""
    dates = pd.date_range("2000-01-01", periods=365 * 3, freq="D")
    q = np.full(len(dates), 5.0)
    # Inject a distinct peak in each calendar year.
    q[100] = 100.0   # year 2000
    q[465] = 200.0   # year 2001
    q[830] = 150.0   # year 2002
    out = annual_maxima(q, dates=dates)
    assert out["n_years"] == 3
    assert sorted(out["annual_max"]) == [100.0, 150.0, 200.0]
    assert out["years"] == [2000, 2001, 2002]


def test_gumbel_mom_matches_closed_form():
    """Gumbel method-of-moments Q100 must equal the textbook reduced-variate formula.

    MoM: scale β = s·√6/π ; location μ = x̄ − γβ.
    Return level: x_T = μ + β·(−ln(−ln(1−1/T))).
    """
    rng = np.random.default_rng(42)
    am = rng.gumbel(loc=100.0, scale=25.0, size=60)

    mean, std = am.mean(), am.std(ddof=1)
    beta = std * np.sqrt(6.0) / np.pi
    mu = mean - _EULER_GAMMA * beta

    out = flood_frequency(am, dist="gumbel", method="mom", n_bootstrap=0)
    # Params recovered via MoM.
    assert out["params"]["scale"] == pytest.approx(beta, rel=1e-6)
    assert out["params"]["loc"] == pytest.approx(mu, rel=1e-6)

    # Q100 closed form.
    T = 100
    y_T = -np.log(-np.log(1 - 1.0 / T))
    expected_q100 = mu + beta * y_T
    q100 = next(r["value"] for r in out["return_levels"] if r["return_period"] == 100)
    assert q100 == pytest.approx(expected_q100, rel=1e-4)


def test_return_levels_monotonic_increasing():
    """Longer return periods → larger floods, for every distribution."""
    rng = np.random.default_rng(7)
    am = rng.gumbel(loc=50, scale=15, size=50)
    for dist in ("gumbel", "gev", "lp3"):
        out = flood_frequency(am, dist=dist, n_bootstrap=0)
        vals = [r["value"] for r in out["return_levels"]]
        assert all(b >= a for a, b in zip(vals, vals[1:])), f"{dist} not monotonic: {vals}"


def test_q2_near_median():
    """The 2-year flood (annual exceedance 0.5) ≈ the sample median, roughly."""
    rng = np.random.default_rng(3)
    am = rng.gumbel(loc=80, scale=20, size=80)
    out = flood_frequency(am, dist="gumbel", method="mom", n_bootstrap=0)
    q2 = next(r["value"] for r in out["return_levels"] if r["return_period"] == 2)
    # Gumbel median = loc - scale*ln(ln2); for these params ~87. Sample median ~ similar.
    assert q2 == pytest.approx(float(np.median(am)), rel=0.15)


def test_bootstrap_ci_brackets_estimate():
    """Bootstrap CI must bracket the point estimate (low ≤ value ≤ high)."""
    rng = np.random.default_rng(11)
    am = rng.gumbel(loc=100, scale=30, size=40)
    out = flood_frequency(am, dist="gumbel", n_bootstrap=300, ci=0.90)
    for r in out["return_levels"]:
        if r["ci_low"] is not None and r["ci_high"] is not None:
            assert r["ci_low"] <= r["value"] <= r["ci_high"]


def test_exceedance_prob_is_reciprocal_return_period():
    am = np.random.default_rng(1).gumbel(60, 12, size=30)
    out = flood_frequency(am, n_bootstrap=0)
    for r in out["return_levels"]:
        assert r["exceedance_prob"] == pytest.approx(1.0 / r["return_period"], abs=1e-9)


def test_too_few_years_raises():
    with pytest.raises(ValueError):
        flood_frequency([10.0, 20.0, 30.0], n_bootstrap=0)


def test_unknown_distribution_raises():
    am = np.random.default_rng(0).gumbel(50, 10, size=30)
    with pytest.raises(ValueError):
        flood_frequency(am, dist="weibull3", n_bootstrap=0)
