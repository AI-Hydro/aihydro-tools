"""
Unit tests for ai_hydro/analysis/uncertainty.py

Tests cover:
- bootstrap_ci: basic CI, reproducibility, edge cases
- block_bootstrap_ci: basic CI, block sizing, wrapping
- bootstrap_dict: multi-metric pass, use_block=True
- UncertaintyResult dict shape
"""
from __future__ import annotations

import math
import pytest
import numpy as np

from ai_hydro.analysis.uncertainty import (
    UncertaintyResult,
    bootstrap_ci,
    block_bootstrap_ci,
    bootstrap_dict,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def iid_data():
    rng = np.random.default_rng(42)
    return rng.normal(loc=5.0, scale=1.0, size=200)


@pytest.fixture
def autocorr_data():
    """AR(1) process with phi=0.9 — strong autocorrelation."""
    rng = np.random.default_rng(42)
    n = 365
    x = np.zeros(n)
    x[0] = 0.0
    for i in range(1, n):
        x[i] = 0.9 * x[i - 1] + rng.normal(0, 0.5)
    return x + 5.0  # shift to positive


# ---------------------------------------------------------------------------
# TestBootstrapCI
# ---------------------------------------------------------------------------

class TestBootstrapCI:
    def test_basic_shape(self, iid_data):
        result = bootstrap_ci(np.mean, iid_data, n=200, ci=0.90)
        assert set(result.keys()) == {"value", "ci_low", "ci_high", "method", "n", "ci_level"}

    def test_method_label(self, iid_data):
        result = bootstrap_ci(np.mean, iid_data, n=200, ci=0.90)
        assert result["method"] == "bootstrap_iid"

    def test_ci_contains_true_mean(self, iid_data):
        """90% CI should contain the point estimate (internal consistency)."""
        result = bootstrap_ci(np.mean, iid_data, n=500, ci=0.90)
        assert result["ci_low"] <= result["value"] <= result["ci_high"]

    def test_ci_ordered(self, iid_data):
        result = bootstrap_ci(np.mean, iid_data, n=200, ci=0.90)
        assert result["ci_low"] < result["ci_high"]

    def test_ci_level_stored(self, iid_data):
        result = bootstrap_ci(np.mean, iid_data, n=200, ci=0.95)
        assert result["ci_level"] == pytest.approx(0.95)

    def test_n_stored(self, iid_data):
        result = bootstrap_ci(np.mean, iid_data, n=200, ci=0.90)
        assert result["n"] == len(iid_data)

    def test_reproducible(self, iid_data):
        r1 = bootstrap_ci(np.mean, iid_data, n=200, ci=0.90, random_state=7)
        r2 = bootstrap_ci(np.mean, iid_data, n=200, ci=0.90, random_state=7)
        assert r1["ci_low"] == r2["ci_low"]
        assert r1["ci_high"] == r2["ci_high"]

    def test_different_seed_gives_different_ci(self, iid_data):
        r1 = bootstrap_ci(np.mean, iid_data, n=200, ci=0.90, random_state=1)
        r2 = bootstrap_ci(np.mean, iid_data, n=200, ci=0.90, random_state=99)
        assert r1["ci_low"] != r2["ci_low"]

    def test_nan_stripped(self):
        data = np.array([1.0, 2.0, np.nan, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        result = bootstrap_ci(np.mean, data, n=200, ci=0.90)
        assert result["n"] == 10  # NaN stripped

    def test_too_few_points_raises(self):
        with pytest.raises(ValueError, match="≥5 valid data points"):
            bootstrap_ci(np.mean, np.array([1.0, 2.0, 3.0]), n=100, ci=0.90)

    def test_custom_fn_median(self, iid_data):
        result = bootstrap_ci(np.median, iid_data, n=200, ci=0.90)
        assert math.isfinite(result["ci_low"])
        assert math.isfinite(result["ci_high"])

    def test_value_equals_point_estimate(self, iid_data):
        expected = float(np.mean(iid_data[~np.isnan(iid_data)]))
        result = bootstrap_ci(np.mean, iid_data, n=200, ci=0.90)
        assert result["value"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# TestBlockBootstrapCI
# ---------------------------------------------------------------------------

class TestBlockBootstrapCI:
    def test_basic_shape(self, autocorr_data):
        result = block_bootstrap_ci(np.mean, autocorr_data, n=200, ci=0.90)
        assert set(result.keys()) == {"value", "ci_low", "ci_high", "method", "n", "ci_level"}

    def test_method_label(self, autocorr_data):
        result = block_bootstrap_ci(np.mean, autocorr_data, n=200, ci=0.90)
        assert result["method"] == "bootstrap_block"

    def test_ci_ordered(self, autocorr_data):
        result = block_bootstrap_ci(np.mean, autocorr_data, n=200, ci=0.90)
        assert result["ci_low"] < result["ci_high"]

    def test_ci_contains_value(self, autocorr_data):
        result = block_bootstrap_ci(np.mean, autocorr_data, n=200, ci=0.90)
        assert result["ci_low"] <= result["value"] <= result["ci_high"]

    def test_explicit_block_size(self, autocorr_data):
        result = block_bootstrap_ci(np.mean, autocorr_data, block_size=14, n=200, ci=0.90)
        assert result["method"] == "bootstrap_block"
        assert math.isfinite(result["ci_low"])

    def test_auto_block_size_default(self, autocorr_data):
        # Default block_size = int(n^(1/3)); no errors
        result = block_bootstrap_ci(np.mean, autocorr_data, n=200, ci=0.90)
        assert result["n"] == len(autocorr_data)

    def test_reproducible(self, autocorr_data):
        r1 = block_bootstrap_ci(np.mean, autocorr_data, n=200, ci=0.90, random_state=3)
        r2 = block_bootstrap_ci(np.mean, autocorr_data, n=200, ci=0.90, random_state=3)
        assert r1["ci_low"] == r2["ci_low"]

    def test_block_wider_than_iid(self, autocorr_data):
        """Block CI should generally be wider than IID for autocorrelated data."""
        iid = bootstrap_ci(np.mean, autocorr_data, n=500, ci=0.90)
        blk = block_bootstrap_ci(np.mean, autocorr_data, n=500, ci=0.90)
        iid_width = iid["ci_high"] - iid["ci_low"]
        blk_width = blk["ci_high"] - blk["ci_low"]
        assert blk_width >= iid_width * 0.5  # block should be >= half of IID width

    def test_too_few_points_raises(self):
        with pytest.raises(ValueError, match="≥10 valid data points"):
            block_bootstrap_ci(np.mean, np.array([1.0, 2.0, 3.0]), n=100, ci=0.90)


# ---------------------------------------------------------------------------
# TestBootstrapDict
# ---------------------------------------------------------------------------

class TestBootstrapDict:
    def test_returns_all_keys(self, iid_data):
        fns = {"mean": np.mean, "median": np.median}
        results = bootstrap_dict(fns, iid_data, n=200, ci=0.90)
        assert set(results.keys()) == {"mean", "median"}

    def test_each_result_shape(self, iid_data):
        fns = {"mean": np.mean}
        results = bootstrap_dict(fns, iid_data, n=200, ci=0.90)
        r = results["mean"]
        assert set(r.keys()) == {"value", "ci_low", "ci_high", "method", "n", "ci_level"}

    def test_iid_method_label(self, iid_data):
        fns = {"mean": np.mean}
        results = bootstrap_dict(fns, iid_data, use_block=False, n=200, ci=0.90)
        assert results["mean"]["method"] == "bootstrap_iid"

    def test_block_method_label(self, autocorr_data):
        fns = {"mean": np.mean}
        results = bootstrap_dict(fns, autocorr_data, use_block=True, n=200, ci=0.90)
        assert results["mean"]["method"] == "bootstrap_block"

    def test_values_finite(self, iid_data):
        fns = {"mean": np.mean, "q5": lambda x: float(np.quantile(x, 0.05))}
        results = bootstrap_dict(fns, iid_data, n=200, ci=0.90)
        for k, r in results.items():
            assert math.isfinite(r["value"]), f"{k}.value is not finite"
            assert math.isfinite(r["ci_low"]), f"{k}.ci_low is not finite"
            assert math.isfinite(r["ci_high"]), f"{k}.ci_high is not finite"

    def test_ci_ordered(self, iid_data):
        fns = {"mean": np.mean, "std": np.std}
        results = bootstrap_dict(fns, iid_data, n=200, ci=0.90)
        for k, r in results.items():
            assert r["ci_low"] < r["ci_high"], f"{k}: ci_low >= ci_high"

    def test_shared_n(self, iid_data):
        fns = {"mean": np.mean, "median": np.median}
        results = bootstrap_dict(fns, iid_data, n=200, ci=0.90)
        n_vals = {r["n"] for r in results.values()}
        assert len(n_vals) == 1, "All metrics should use the same n"

    def test_too_few_points_raises(self):
        with pytest.raises(ValueError):
            bootstrap_dict({"mean": np.mean}, np.array([1.0, 2.0]))


# ---------------------------------------------------------------------------
# TestUncertaintyResultShape
# ---------------------------------------------------------------------------

class TestUncertaintyResultShape:
    """UncertaintyResult is a TypedDict — verify the dict structure is correct."""

    def test_all_required_keys(self, iid_data):
        result = bootstrap_ci(np.mean, iid_data, n=100, ci=0.90)
        for key in ("value", "ci_low", "ci_high", "method", "n", "ci_level"):
            assert key in result

    def test_json_serializable(self, iid_data):
        import json
        result = bootstrap_ci(np.mean, iid_data, n=100, ci=0.90)
        json.dumps(dict(result))  # should not raise
