"""Known-answer tests for SPI/SPEI drought-index kernels.

Pure compute — no network, no session. The defining property of a standardised
drought index is that, on its own calibration data, it is approximately standard
normal; we assert that, plus dry/wet sign behaviour and PET sensitivity.
"""
import numpy as np
import pandas as pd
import pytest

from ai_hydro.analysis.drought_indices import spi, spei


def _daily_dates(years):
    return pd.date_range("1980-01-01", periods=365 * years, freq="D")


def test_spi_is_approximately_standard_normal():
    """SPI on its own calibration data → mean≈0, std≈1 (probability integral transform)."""
    rng = np.random.default_rng(0)
    dates = _daily_dates(40)
    # Seasonal daily precip, strictly non-negative.
    doy = dates.dayofyear.values
    base = 2.0 + 1.5 * np.sin(2 * np.pi * doy / 365)
    precip = np.clip(rng.gamma(shape=base, scale=1.5), 0, None)
    out = spi(precip, dates, scale_months=3)
    assert abs(out["mean"]) < 0.15
    assert 0.8 < out["std"] < 1.2
    assert out["index_name"] == "SPI"
    assert out["scale_months"] == 3


def test_spi_dry_period_is_negative():
    """A multi-year dry anomaly produces strongly negative SPI in that window."""
    rng = np.random.default_rng(1)
    dates = _daily_dates(40)
    precip = np.clip(rng.gamma(2.0, 1.5, size=len(dates)), 0, None)
    # Suppress precip in calendar years 2000–2001 (a drought).
    yrs = dates.year.values
    drought = (yrs >= 2000) & (yrs <= 2001)
    precip[drought] *= 0.15
    out = spi(precip, dates, scale_months=6)
    idx = pd.Series(out["index"], index=pd.to_datetime(out["index_months"]))
    drought_vals = idx[(idx.index.year >= 2000) & (idx.index.year <= 2001)].dropna()
    assert drought_vals.mean() < -1.0


def test_spi_wet_period_is_positive():
    rng = np.random.default_rng(2)
    dates = _daily_dates(40)
    precip = np.clip(rng.gamma(2.0, 1.5, size=len(dates)), 0, None)
    yrs = dates.year.values
    wet = (yrs >= 2005) & (yrs <= 2006)
    precip[wet] *= 4.0
    out = spi(precip, dates, scale_months=6)
    idx = pd.Series(out["index"], index=pd.to_datetime(out["index_months"]))
    wet_vals = idx[(idx.index.year >= 2005) & (idx.index.year <= 2006)].dropna()
    assert wet_vals.mean() > 1.0


def test_spei_is_approximately_standard_normal():
    rng = np.random.default_rng(3)
    dates = _daily_dates(40)
    precip = np.clip(rng.gamma(2.0, 1.5, size=len(dates)), 0, None)
    pet = np.clip(2.0 + rng.normal(0, 0.5, size=len(dates)), 0, None)
    out = spei(precip, pet, dates, scale_months=3)
    assert abs(out["mean"]) < 0.15
    assert 0.8 < out["std"] < 1.2
    assert out["index_name"] == "SPEI"


def test_spei_responds_to_higher_pet():
    """Holding precip fixed, raising PET makes the balance drier → lower mean SPEI
    in the elevated-PET window."""
    rng = np.random.default_rng(4)
    dates = _daily_dates(40)
    precip = np.clip(rng.gamma(2.0, 1.5, size=len(dates)), 0, None)
    pet = np.full(len(dates), 2.0)
    yrs = dates.year.values
    hot = (yrs >= 2010) & (yrs <= 2011)
    pet[hot] = 6.0   # heatwave: much higher evaporative demand
    out = spei(precip, pet, dates, scale_months=6)
    idx = pd.Series(out["index"], index=pd.to_datetime(out["index_months"]))
    hot_vals = idx[(idx.index.year >= 2010) & (idx.index.year <= 2011)].dropna()
    other = idx[(idx.index.year < 2010) | (idx.index.year > 2011)].dropna()
    assert hot_vals.mean() < other.mean()
    assert hot_vals.mean() < 0


def test_spi_rejects_short_record():
    dates = _daily_dates(1)  # 1 year — too short for a 3-month index w/ 12-mo cushion
    precip = np.ones(len(dates))
    with pytest.raises(ValueError):
        spi(precip, dates, scale_months=3)


def test_spei_length_mismatch_raises():
    dates = _daily_dates(40)
    precip = np.ones(len(dates))
    pet = np.ones(len(dates) - 5)
    with pytest.raises(ValueError):
        spei(precip, pet, dates, scale_months=3)
