"""
Tests for the argument-repair + self-correcting-error middleware (WS-4).

Covers:
  - repair_arguments: alias rename, fuzzy match, type coercion (pure fn)
  - repair_arguments: no-op on already-valid args + empty props
  - ArgRepairMiddleware.on_call_tool: repairs args before execution
  - ArgRepairMiddleware.on_call_tool: returns structured self-help on failure
  - retry-loop breaker escalates after a repeated failing call
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ai_hydro.mcp.arg_repair import (
    ArgRepairMiddleware,
    repair_arguments,
    _coerce_type,
)


def _run(coro):
    return asyncio.run(coro)


class TestRepairArguments:
    def test_alias_rename(self):
        props = {"index_name", "session_id"}
        types = {"index_name": "string", "session_id": "string"}
        repaired, notes = repair_arguments({"index": "NDWI"}, props, types)
        assert repaired == {"index_name": "NDWI"}
        assert any("renamed" in n for n in notes)

    def test_fuzzy_match_close_key(self):
        props = {"latitude", "longitude"}
        types = {"latitude": "number", "longitude": "number"}
        # 'lattitude' (typo) is close enough to 'latitude' (>0.82)
        repaired, notes = repair_arguments({"lattitude": 28.2}, props, types)
        assert "latitude" in repaired
        assert "lattitude" not in repaired

    def test_no_rename_when_target_present(self):
        props = {"index_name"}
        types = {"index_name": "string"}
        # Both alias and canonical supplied → don't clobber canonical
        repaired, _ = repair_arguments(
            {"index": "NDWI", "index_name": "NDVI"}, props, types
        )
        assert repaired["index_name"] == "NDVI"

    def test_type_coercion_string_to_number(self):
        props = {"latitude"}
        types = {"latitude": "number"}
        repaired, notes = repair_arguments({"latitude": "28.2"}, props, types)
        assert repaired["latitude"] == 28.2
        assert any("coerced" in n for n in notes)

    def test_type_coercion_scalar_to_array(self):
        props = {"bands"}
        types = {"bands": "array"}
        repaired, _ = repair_arguments({"bands": "B4"}, props, types)
        assert repaired["bands"] == ["B4"]

    def test_valid_args_unchanged(self):
        props = {"index_name", "session_id"}
        types = {"index_name": "string", "session_id": "string"}
        repaired, notes = repair_arguments(
            {"index_name": "NDWI", "session_id": "abc"}, props, types
        )
        assert repaired == {"index_name": "NDWI", "session_id": "abc"}
        assert notes == []

    def test_empty_props_noop(self):
        repaired, notes = repair_arguments({"foo": 1}, set(), {})
        assert repaired == {"foo": 1}
        assert notes == []


class TestCoerceType:
    def test_bool_from_string(self):
        assert _coerce_type("true", "boolean") is True
        assert _coerce_type("no", "boolean") is False

    def test_int_from_string(self):
        assert _coerce_type("42", "integer") == 42
        assert _coerce_type("-7", "integer") == -7

    def test_number_from_string(self):
        assert _coerce_type("3.14", "number") == 3.14

    def test_unparseable_left_alone(self):
        assert _coerce_type("not-a-number", "number") == "not-a-number"


class _FakeMCP:
    """Minimal stand-in so the middleware can build its schema cache."""

    def __init__(self, tools):
        self._tools = tools

    async def list_tools(self):
        return self._tools


def _fake_tool(name, schema):
    mcp_tool = SimpleNamespace(name=name, inputSchema=schema)
    return SimpleNamespace(to_mcp_tool=lambda: mcp_tool)


_SCHEMA = {
    "type": "object",
    "properties": {
        "index_name": {"type": "string"},
        "session_id": {"type": "string"},
    },
    "required": ["index_name"],
}


def _make_mw():
    fake = _FakeMCP([_fake_tool("compute_spectral_index", _SCHEMA)])
    return ArgRepairMiddleware(fake)


def _ctx(name, arguments):
    msg = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(message=msg)


class TestMiddleware:
    def test_repairs_args_before_execution(self):
        mw = _make_mw()
        ctx = _ctx("compute_spectral_index", {"index": "NDWI"})

        captured = {}

        async def call_next(c):
            captured["args"] = c.message.arguments
            return "ok"

        result = _run(mw.on_call_tool(ctx, call_next))
        assert result == "ok"
        # alias 'index' was renamed to 'index_name' before the body ran
        assert captured["args"] == {"index_name": "NDWI"}

    def test_self_help_on_failure(self):
        mw = _make_mw()
        ctx = _ctx("compute_spectral_index", {"bogus": 1})

        async def call_next(c):
            raise ValueError("missing required argument index_name")

        result = _run(mw.on_call_tool(ctx, call_next))
        payload = result.structured_content
        assert payload["error"] is True
        assert payload["tool"] == "compute_spectral_index"
        assert "index_name" in payload["required"]
        assert "input_schema" in payload
        assert payload["example_call"]["tool"] == "compute_spectral_index"

    def test_retry_breaker_escalates(self):
        mw = _make_mw()

        async def call_next(c):
            raise ValueError("boom")

        # First failure: normal teaching message
        r1 = _run(mw.on_call_tool(_ctx("compute_spectral_index", {"bogus": 1}), call_next))
        assert "already tried" not in r1.structured_content["message"].lower()
        # Second identical failure: escalated guidance
        r2 = _run(mw.on_call_tool(_ctx("compute_spectral_index", {"bogus": 1}), call_next))
        assert "already tried" in r2.structured_content["message"].lower()

    def test_unknown_tool_passes_through(self):
        mw = _make_mw()
        ctx = _ctx("not_a_registered_tool", {"x": 1})

        async def call_next(c):
            raise ValueError("upstream error")

        # No schema info → middleware must not swallow; re-raises
        with pytest.raises(ValueError):
            _run(mw.on_call_tool(ctx, call_next))
