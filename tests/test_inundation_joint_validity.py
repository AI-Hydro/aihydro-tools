"""Joint support and undefined metric regression cases."""
import json

import numpy as np
import pytest
from rasterio.transform import from_bounds

from ai_hydro.analysis.inundation_validation import (
    contingency_metrics,
    validate_inundation_against_geojson,
)


def test_missing_and_masked_cells_are_excluded():
    model = np.ma.array([1., 0., np.nan, 1.], mask=[0, 0, 0, 1])
    result = contingency_metrics(model, np.array([1., 1., 1., 0.]))
    assert result["valid_cells"] == 2
    assert result["excluded_cells"] == 2
    assert result["hits"] == result["misses"] == 1
    assert result["csi"] == .5
    assert result["false_alarms"] == 0


def test_joint_validity_drops_nodata_codes():
    result = contingency_metrics(np.array([1, 255, 0, 1]), np.array([1, 0, 1, 255]),
                                 model_valid_mask=np.array([True, False, False, True]),
                                 reference_valid_mask=np.array([True, True, True, False]))
    assert result["valid_cells"] == 1 and result["hits"] == 1
    assert result["excluded_cells"] == 3


@pytest.mark.parametrize("values", [[255], [-1], [0.5], ["False"]])
def test_nonbinary_values_not_silently_coerced(values):
    with pytest.raises(ValueError):
        contingency_metrics(np.array(values), np.array([1]))


@pytest.mark.parametrize("validity", [np.array([1]), np.array([True, False])])
def test_invalid_support_mask(validity):
    with pytest.raises(ValueError, match="boolean array"):
        contingency_metrics(np.array([1]), np.array([1]), reference_valid_mask=validity)


def test_empty_domain_and_all_dry_do_not_score_perfect():
    for model, ref, status in [(np.array([np.nan]), np.array([1]), "not_assessed"),
                               (np.zeros(3), np.zeros(3), "computed")]:
        result = contingency_metrics(model, ref)
        assert result["status"] == status
        assert all(result[key] is None for key in ("csi", "pod", "far", "bias"))
        json.dumps(result, allow_nan=False)


def test_each_undefined_ratio_is_independent():
    result = contingency_metrics(np.array([0]), np.array([1]))
    assert result["csi"] == result["pod"] == result["bias"] == 0
    assert result["far"] is None


def test_extent_requires_support_and_crs():
    result = validate_inundation_against_geojson(np.ones((2, 2)), transform=from_bounds(0, 0, 1, 1, 2, 2),
                                                reference_geojson={"type": "FeatureCollection", "features": []})
    assert result["status"] == "not_assessed" and "csi" not in result


def test_geojson_dry_pixels_only_with_declared_support():
    result = validate_inundation_against_geojson(np.array([[1, 1], [0, 0]]),
        transform=from_bounds(0, 0, 1, 1, 2, 2), reference_geojson={"type": "FeatureCollection", "features": []},
        model_crs="EPSG:4326", reference_valid_mask=np.array([[True, False], [False, False]]))
    assert result["valid_cells"] == 1 and result["false_alarms"] == 1
    assert result["pod"] is None and result["rasterization"] == "pixel_center"


def test_surrogate_no_event_target_has_explicit_error():
    from ai_hydro.analysis.inundation_surrogate import _tune_morphology_iterations
    with pytest.raises(ValueError, match="target flood cells"):
        _tune_morphology_iterations(np.zeros((2, 2)), np.zeros((2, 2)))


def test_masked_validity_is_not_silently_true():
    with pytest.raises(ValueError, match="cannot itself contain masked"):
        contingency_metrics(np.array([1]), np.array([1]),
                            reference_valid_mask=np.ma.array([True], mask=[True]))


def test_empty_extent_still_requires_valid_grid():
    from affine import Affine
    with pytest.raises(ValueError, match="finite and invertible"):
        validate_inundation_against_geojson(np.ones((2, 2)), transform=Affine(0, 0, 0, 0, 0, 0),
            reference_geojson={"type": "FeatureCollection", "features": []},
            model_crs="EPSG:4326", reference_valid_mask=np.ones((2, 2), dtype=bool))
