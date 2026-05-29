"""
Tests for the capability/tool discovery surface (WS-2 context-injection engine).

Covers:
  - describe_tool: full inputSchema + worked example for a known tool
  - describe_tool: graceful error + suggestions on unknown name
  - describe_tools: batch, with unknown-name reporting
  - hot-flag tagging: every Tier-1 tool and the curated allowlist are hot;
    the `_meta.hot` flag is stamped onto each tool's MCP wire payload
"""
from __future__ import annotations

import asyncio

import pytest

from ai_hydro.mcp import mcp
from ai_hydro.mcp.app import HOT_TOOL_ALLOWLIST, TOOL_TIERS, is_hot_tool
from ai_hydro.mcp.tools_discovery import describe_tool, describe_tools


def _run(coro):
    return asyncio.run(coro)


class TestDescribeTool:
    def test_known_tool_returns_schema_and_example(self):
        result = _run(describe_tool("compute_spectral_index"))
        assert result.get("name") == "compute_spectral_index"
        assert "input_schema" in result
        assert isinstance(result["parameters"], list)
        assert "index_name" in result["required"]
        # Worked example must include the required param
        assert result["example_call"]["tool"] == "compute_spectral_index"
        assert "index_name" in result["example_call"]["arguments"]

    def test_case_insensitive_fallback(self):
        result = _run(describe_tool("COMPUTE_TWI"))
        assert result.get("name") == "compute_twi"

    def test_unknown_tool_returns_suggestions(self):
        result = _run(describe_tool("compute_twiii"))
        assert result.get("error") is True
        assert "did_you_mean" in result
        assert "compute_twi" in result["did_you_mean"]

    def test_parameters_have_required_flags(self):
        result = _run(describe_tool("compute_spectral_index"))
        names = {p["name"]: p for p in result["parameters"]}
        assert names["index_name"]["required"] is True
        # session_id is optional
        assert names["session_id"]["required"] is False


class TestDescribeTools:
    def test_batch_mixes_known_and_unknown(self):
        result = _run(describe_tools(["compute_twi", "not_a_tool"]))
        got = {t["name"] for t in result["tools"]}
        assert "compute_twi" in got
        assert any(u["name"] == "not_a_tool" for u in result["unknown"])

    def test_empty_input(self):
        result = _run(describe_tools([]))
        assert result["count"] == 0
        assert result["tools"] == []


class TestHotFlag:
    def test_all_tier1_are_hot(self):
        for name, tier in TOOL_TIERS.items():
            if tier == 1:
                assert is_hot_tool(name), f"Tier-1 tool {name} must be hot"

    def test_allowlist_are_hot(self):
        for name in HOT_TOOL_ALLOWLIST:
            assert is_hot_tool(name)

    def test_meta_hot_stamped_on_wire_tools(self):
        tools = _run(mcp.list_tools())
        sample = next(t for t in tools if t.name == "compute_spectral_index")
        meta = sample.to_mcp_tool().meta or {}
        assert meta.get("hot") is True
        assert meta.get("tier") == 2
        assert "domain" in meta

    def test_non_hot_tool_marked_summary(self):
        tools = _run(mcp.list_tools())
        # A clearly cold infra tool
        cold = next(t for t in tools if t.name == "get_session_health")
        meta = cold.to_mcp_tool().meta or {}
        assert meta.get("hot") is False
