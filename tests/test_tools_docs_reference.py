"""``generate_tool_reference`` writes only to the path it is given."""
from __future__ import annotations

import inspect

import pytest

import ai_hydro.mcp  # noqa: F401  (registers every tool)
from ai_hydro.mcp.tools_docs import generate_tool_reference


def test_target_is_required():
    assert inspect.signature(generate_tool_reference).parameters["target"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        generate_tool_reference()  # type: ignore[call-arg]


def test_writes_exactly_the_given_path(tmp_path):
    out = tmp_path / "sub" / "reference.md"
    assert generate_tool_reference(out) == out
    assert out.read_text().startswith("---") and "Complete Tool Reference" in out.read_text()
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == ["reference.md"]
