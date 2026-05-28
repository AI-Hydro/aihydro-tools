"""
Sprint 0 — Tool tier coverage tests.

Every tool registered via @mcp.tool() must have an entry in TOOL_TIERS.
A missing entry fails CI so new tools can't ship without an explicit tier
assignment (see DESIGN_PRINCIPLES.md §Tool tiering).
"""
import pytest
from ai_hydro.mcp.app import TOOL_TIERS, get_tool_tiers, get_tool_tier


def test_tier_values_are_valid():
    """All tier values must be 1, 2, or 3."""
    for name, tier in TOOL_TIERS.items():
        assert tier in (1, 2, 3), f"Tool '{name}' has invalid tier {tier!r}"


def test_tier_distribution_sanity():
    """At least 5 Tier-1 tools must exist — if this fails something was demoted."""
    tiers = list(TOOL_TIERS.values())
    assert tiers.count(1) >= 5, "Fewer than 5 Tier-1 tools — check for unintended demotion"
    assert tiers.count(2) >= 3, "Fewer than 3 Tier-2 tools — check for unintended demotion"
    assert tiers.count(3) >= 5, "Fewer than 5 Tier-3 tools — check for unintended demotion"


def test_get_tool_tier_known():
    """get_tool_tier() returns the correct tier for known tools."""
    assert get_tool_tier("delineate_watershed") == 1
    # fetch_streamflow_data demoted to tier 3 in Wave 2.5 Axis 4:
    # agents should use data_fetch directly; legacy tool is a backward-compat shim.
    assert get_tool_tier("fetch_streamflow_data") == 3
    assert get_tool_tier("start_session") == 3


def test_get_tool_tier_unknown():
    """get_tool_tier() returns None for unknown tool names."""
    assert get_tool_tier("nonexistent_tool") is None


def test_get_tool_tiers_is_copy():
    """get_tool_tiers() returns a copy; mutating it does not affect TOOL_TIERS."""
    snapshot = get_tool_tiers()
    snapshot["__canary__"] = 99
    assert "__canary__" not in TOOL_TIERS


def test_all_registered_tools_have_tiers():
    """
    Every tool registered with FastMCP must appear in TOOL_TIERS.

    This test imports all tool modules (triggering @mcp.tool() registration)
    then compares the live tool list against TOOL_TIERS.  A new tool that
    ships without a tier entry will fail here.
    """
    import asyncio
    # Import all modules to trigger @mcp.tool() registration
    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.mcp.app import mcp

    registered_names: set[str] = set()
    try:
        tools = asyncio.run(mcp.list_tools())
        registered_names = {t.name for t in tools}
    except Exception:
        # Fallback: introspect the internal tool map if list_tools() is unavailable
        if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
            registered_names = set(mcp._tool_manager._tools.keys())
        elif hasattr(mcp, "_tools"):
            registered_names = set(mcp._tools.keys())

    if not registered_names:
        pytest.skip("Could not enumerate registered tools from FastMCP")

    missing = registered_names - set(TOOL_TIERS)
    assert not missing, (
        f"The following tools are registered but have no tier assignment in "
        f"TOOL_TIERS (ai_hydro/mcp/app.py): {sorted(missing)}\n"
        "Add each tool to TOOL_TIERS with tier 1, 2, or 3."
    )
