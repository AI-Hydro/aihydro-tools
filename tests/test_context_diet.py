"""T-C1 context diet: the discovery answer is capped, ``start_session`` stays small, and
``write_research_interpretation`` leaves the agent's instruction file (research.md) alone.

Everything runs in memory against the real app; the session store and AIHYDRO_HOME are scratch.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess

import pytest
from fastmcp import Client

import ai_hydro.mcp  # noqa: F401  (registers every tool)
from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.tools_session import LIST_TOOLS_MAX_CHARS
from ai_hydro.session import store

CAP = 4000


def _call(tool: str, args: dict | None = None):
    async def run():
        async with Client(mcp) as c:
            return await c.call_tool(tool, args or {}, raise_on_error=False)
    return asyncio.run(run())


def _text(res) -> str:
    return "".join(getattr(b, "text", "") for b in res.content)


def _size(res) -> int:
    return len(json.dumps(res.structured_content, default=str))


def test_cap_constant_matches_the_program_budget():
    assert LIST_TOOLS_MAX_CHARS == CAP


def test_no_argument_list_is_capped_sorted_and_paged():
    res = _call("list_available_tools")
    assert not res.is_error
    out = res.structured_content
    assert _size(res) <= CAP
    names = [t["name"] for t in out["tools"]]
    assert names == sorted(names)
    assert out["n_tools"] > 100 and len(names) < out["n_tools"]
    assert out["next_offset"] == len(names) and out["offset"] == 0
    assert all(set(t) == {"name", "summary"} for t in out["tools"])
    assert _size(_call("list_available_tools")) == _size(res)                  # deterministic


def test_paging_visits_every_tool_exactly_once_and_every_page_is_capped():
    seen, offset, pages = [], 0, 0
    while offset is not None:
        res = _call("list_available_tools", {"offset": offset})
        assert _size(res) <= CAP, (offset, _size(res))
        seen += [t["name"] for t in res.structured_content["tools"]]
        offset = res.structured_content["next_offset"]
        pages += 1
        assert pages < 100
    from ai_hydro.mcp.tool_surface import load_annotation_rows

    host_only = {n for n, r in load_annotation_rows().items() if r.get("audience") == "host"}
    registered = sorted(set(asyncio.run(mcp.get_tools())) - host_only)     # host-audience tools are not listed (T-P1)
    assert seen == registered
    assert len(set(seen)) == len(seen)


def test_limit_and_domain_filters_are_capped_and_deterministic():
    res = _call("list_available_tools", {"limit": 3})
    assert len(res.structured_content["tools"]) == 3 and res.structured_content["next_offset"] == 3
    dom = _call("list_available_tools", {"domain": "maps"})
    assert _size(dom) <= CAP and dom.structured_content["domain"] == "maps"
    names = {t["name"] for t in dom.structured_content["tools"]}
    assert {"map_fly_to", "map_fit_extent"} <= names
    assert all(n.startswith(("map_", "show_on_map", "gee.")) for n in names)
    bad = _call("list_available_tools", {"domain": "nope"})
    assert bad.is_error and "maps" in _text(bad)               # the error contract carries the payload


def test_name_returns_one_tool_in_full_and_unknown_names_get_suggestions():
    one = _call("list_available_tools", {"name": "add_claim"}).structured_content
    assert one["tool"]["name"] == "add_claim" and "session_id" in one["tool"]["parameters"]
    assert one["tool"]["description"]
    miss = _call("list_available_tools", {"name": "add_cla"})
    assert miss.is_error and "add_claim" in _text(miss)


def test_the_full_dump_is_gone_but_the_registry_is_not():
    """The old answer was ~136k characters. One page is not, and the registry is still complete."""
    assert _size(_call("list_available_tools")) < 0.05 * 135_000


@pytest.fixture
def scratch_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    return tmp_path


def test_start_session_does_not_dump_the_pip_package_map(scratch_sessions, monkeypatch):
    """Pins the existing 2.8.0 behaviour (off by default): no `pip list`, no package map; plus a size bound."""
    calls = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or real(*a, **k))
    res = _call("start_session", {"session_id": "diet01"})
    assert not res.is_error
    out = res.structured_content
    assert "available_packages" not in out
    assert not any("pip" in " ".join(map(str, a[0])) for a in calls if a and isinstance(a[0], (list, tuple)))
    assert _size(res) <= CAP, _size(res)


def test_start_session_stays_under_the_cap_for_a_populated_session(scratch_sessions):
    """A long study: many notes, many slots. Notes are the unbounded part and are trimmed to the last 5.

    `computed` / `slot_health` grow with the number of slots (bounded by the slot registry), so the
    4k budget is a property of a session with a normal slot count, not an absolute guarantee.
    """
    s = store.HydroSession("diet_pop")
    for i in range(60):
        s.add_note(f"note {i} " + "x" * 300)
    for i in range(25):
        s.set(f"slot{i}", {"v": i})
    s.save()
    res = _call("start_session", {"session_id": "diet_pop"})
    out = res.structured_content
    assert _size(res) <= CAP, _size(res)
    assert out["notes_total"] == 60 and len(out["notes"]) == 5
    assert out["notes"][-1].startswith("note 59") or "note 59" in str(out["notes"][-1])
    full = _call("get_session_summary", {"session_id": "diet_pop"}).structured_content
    assert len(full["notes"]) == 60                              # nothing is lost, only not echoed here


def test_list_limit_coercion_is_documented_behaviour():
    """limit of 0, None or negative means "no limit beyond the size cap"; a negative offset is 0."""
    base = _call("list_available_tools").structured_content
    for lim in (0, -3):
        assert [t["name"] for t in _call("list_available_tools", {"limit": lim}).structured_content["tools"]] \
            == [t["name"] for t in base["tools"]]
    assert _call("list_available_tools", {"offset": -5}).structured_content["offset"] == 0
    assert len(_call("list_available_tools", {"limit": 1}).structured_content["tools"]) == 1


def test_start_session_opt_in_still_returns_packages(scratch_sessions):
    """Pins the existing 2.8.0 behaviour (include_packages is opt-in); not new in this change."""
    out = _call("start_session", {"session_id": "diet02", "include_packages": True}).structured_content
    assert isinstance(out["available_packages"], dict)


def test_write_research_interpretation_does_not_touch_research_md(scratch_sessions):
    ws = scratch_sessions / "ws"
    ws.mkdir()
    sid = "diet03"
    out = _call("start_session", {"session_id": sid, "workspace_dir": str(ws)}).structured_content
    rm = ws / ".aihydrorules" / "research.md"
    assert rm.is_file(), "start_session leaves the retirement stub"
    before = (rm.stat().st_mtime_ns, rm.read_bytes())

    res = _call("write_research_interpretation", {
        "session_id": sid, "site_name": "diet basin",
        "interpretation": "The basin is hydrologically complex with multiple sub-regimes."})
    assert not res.is_error and res.structured_content.get("error") is None
    assert (rm.stat().st_mtime_ns, rm.read_bytes()) == before          # the instruction surface is unchanged
    saved = store.HydroSession.load(sid)
    assert saved.interpretation.startswith("The basin is hydrologically complex")   # still persisted
    assert "research.md" in res.structured_content["_note"] and "not rewritten" in res.structured_content["_note"]
    # and a later start_session does not rewrite it either: it is the stub, never a digest again (T-R1)
    _call("start_session", {"session_id": sid})
    assert (rm.stat().st_mtime_ns, rm.read_bytes()) == before
    assert b"hydrologically complex" not in rm.read_bytes()


def test_tools_md_is_not_rewritten_when_the_registry_is_unchanged(tmp_path, monkeypatch):
    from ai_hydro.mcp.tools_docs import _write_tools_md

    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path))
    p = _write_tools_md()
    first = p.stat().st_mtime_ns
    os.utime(p, ns=(first - 10**9, first - 10**9))
    stamped = p.stat().st_mtime_ns
    assert _write_tools_md().stat().st_mtime_ns == stamped
