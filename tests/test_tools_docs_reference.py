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


def test_md_text_escapes_tags_outside_code_spans():
    from ai_hydro.mcp.tools_docs import _md_text

    assert _md_text("wraps <html>/<body> & <id>") == "wraps &lt;html&gt;/&lt;body&gt; &amp; &lt;id&gt;"
    assert _md_text("call `run(<id>)` then <x>") == "call `run(<id>)` then &lt;x&gt;"


def test_reference_has_no_raw_tags_outside_code(tmp_path):
    import re

    text = generate_tool_reference(tmp_path / "reference.md").read_text()
    prose = re.sub(r"```.*?```", "", text, flags=re.S)
    prose = re.sub(r"`[^`\n]*`", "", prose)
    assert not re.search(r"<(html|body|head|script|[a-z_]+)\b[^>]*>", prose), re.findall(r"<[a-z_]+[^>]*>", prose)[:5]
