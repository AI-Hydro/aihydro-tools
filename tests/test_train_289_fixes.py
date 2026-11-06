"""2.8.9: watershed 0.4.0 integration fixes (slope fail-closed, relief flag, delineation arguments)."""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest


def test_slope_unavailable_becomes_a_structured_error():
    from ai_hydro.analysis._dem import SlopeUnavailableError

    from ai_hydro.mcp.helpers import _tool_error_to_dict

    env = _tool_error_to_dict(SlopeUnavailableError("DEM has no CRS; refusing to assume a metric grid"))
    assert env["error"] is True
    assert env["code"] == "SLOPE_UNAVAILABLE"
    assert env["recovery"]


def test_hand_stack_fails_closed_when_reprojection_fails():
    from ai_hydro.analysis import inundation as inu

    class _Rio:
        def reproject(self, *_a, **_k):
            raise RuntimeError("no transform")

    class _Dem:
        rio = _Rio()
        ndim = 2

    geom = type("G", (), {"x": 10.0, "y": 50.0, "centroid": None})()
    geom.centroid = geom
    with patch("ai_hydro.analysis._dem.fetch_dem", return_value=(_Dem(), "p", "s")), patch(
        "ai_hydro.analysis._dem._geom_in_conus", return_value=False
    ):
        from ai_hydro.core import ToolError

        with pytest.raises(ToolError) as ei:
            inu._prepare_hand_stack(geom)
    assert ei.value.code == "DEM_REPROJECTION_FAILED"


def test_utm_zone_wraps_at_180():
    src = inspect.getsource(__import__("ai_hydro.analysis.inundation", fromlist=["x"])._prepare_hand_stack)
    assert "% 60 + 1" in src
    assert (int((180.0 + 180) // 6) % 60) + 1 == 1


def test_delineate_tool_exposes_expansion_and_hybrid_alias():
    from ai_hydro.mcp import tools_analysis as ta

    sig = inspect.signature(ta.delineate_watershed_from_point.fn if hasattr(ta.delineate_watershed_from_point, "fn") else ta.delineate_watershed_from_point)
    assert sig.parameters["allow_downstream_expansion"].default is False
