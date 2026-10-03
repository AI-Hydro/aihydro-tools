"""The new series tools do not change the per-arm tool differences.

The eval condition layer must still remove exactly the governance set between
arms: C3 - C2 is the registry tools and C2 - C1 is the C1-only governance set
(every ``check_*`` tool plus the names in ``C1_EXTRA``). The series / geometry
tools and ``data_fetch`` are present in every arm.
"""
from __future__ import annotations

import pytest

# Helpers and fixtures of the eval-condition tests (tool list per arm, arm marker).
from test_eval_condition import C1_EXTRA, REGISTRY, arm, home, names  # noqa: F401

from ai_hydro.mcp import eval_condition as ec

NEW_TOOLS = {"summarize_series", "detect_threshold_runs", "compare_series", "bootstrap_statistic",
             "measure_feature"}


def test_new_tools_are_registered_and_visible_in_every_arm(home, monkeypatch):
    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp.app import mcp

    assert NEW_TOOLS <= names(mcp) and "data_fetch" in names(mcp)
    for condition in ("C3", "C2", "C1"):
        arm(home, monkeypatch, condition)
        visible = names(mcp)
        assert NEW_TOOLS <= visible and "data_fetch" in visible, condition


def test_per_arm_tool_differences_are_exactly_the_governance_set(home, monkeypatch):
    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp.app import mcp

    full = names(mcp)
    arm(home, monkeypatch, "C3")
    c3 = names(mcp)
    arm(home, monkeypatch, "C2")
    c2 = names(mcp)
    arm(home, monkeypatch, "C1")
    c1 = names(mcp)

    assert full - c3 == ec.HIDDEN_ALL_ARMS
    assert c3 - c2 == REGISTRY
    c2_minus_c1 = c2 - c1
    assert C1_EXTRA <= c2_minus_c1
    assert all(n.startswith("check_") for n in c2_minus_c1 - C1_EXTRA)
    # None of the added tools is in a difference set: they are in all three arms.
    assert not (NEW_TOOLS | {"data_fetch"}) & ((c3 - c2) | c2_minus_c1)
    # And the difference sets are computed on a surface that includes them.
    assert NEW_TOOLS <= c1 < c2 < c3
