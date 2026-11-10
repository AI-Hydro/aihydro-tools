"""Background jobs report a modelling refusal's own code, not TRAINING_ERROR (duck-typed stubs)."""
from __future__ import annotations

import pytest

from ai_hydro.modelling.runner import _refusal_code


def _stub(name, base=Exception):
    return type(name, (base,), {"code": f"{name.upper()}_CODE"})


@pytest.mark.parametrize("name", [
    "QuarantinedError", "BackendQuarantinedError", "UncertaintyHeadQuarantinedError",
    "LatitudeRequiredError", "WaterBalanceError", "InsufficientDataError",
])
def test_modelling_refusals_map_to_their_code(name):
    assert _refusal_code(_stub(name)("x")) == f"{name.upper()}_CODE"


def test_subclass_of_a_refusal_maps():
    Base = _stub("QuarantinedError")
    assert _refusal_code(type("Child", (Base,), {})("x")) == "QUARANTINEDERROR_CODE"


def test_unrelated_exception_with_a_code_is_not_a_refusal():
    assert _refusal_code(_stub("SomeOtherError")("x")) is None
    assert _refusal_code(ValueError("x")) is None


def test_refusal_without_a_code_is_not_mapped():
    assert _refusal_code(type("LatitudeRequiredError", (Exception,), {})("x")) is None


@pytest.mark.parametrize("cls_name", [
    "BackendQuarantinedError", "UncertaintyHeadQuarantinedError", "QuarantinedError",
])
def test_real_modelling_quarantine_classes_map(cls_name):
    q = pytest.importorskip("aihydro_modelling.quarantine")
    cls = getattr(q, cls_name, None)
    if cls is None:
        pytest.skip(f"{cls_name} not defined in this modelling release")
    try:
        exc = cls("x")
    except TypeError:
        pytest.skip(f"{cls_name} needs constructor arguments this test cannot guess")
    assert _refusal_code(exc) == getattr(exc, "code")
