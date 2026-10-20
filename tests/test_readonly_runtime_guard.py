"""readOnlyHint is trusted by hosts, so it is enforced at run time, not only by reading the code.

Every tool annotated readOnlyHint=true is called through the in-memory server against a fixture
session while a ``sys.addaudithook`` recorder is active. Any file write, mkdir, rename/remove,
sqlite connection, socket/DNS event or subprocess outside the explicit allowlist fails the test.

Allowlist (nothing else):
  * the run record / run-log append: ``<session>.runlog.sqlite3`` and its journal files;
  * creating an empty directory under the test home or temp dir (a tool-owned cache root);
  * interpreter housekeeping: ``__pycache__`` and the matplotlib/fontconfig caches.

The mutation test relabels every NON-read-only tool as read-only and proves the static guard
(``test_tool_surface._static_violations``) or this runtime guard flags it.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _runtime_guard import Event, recording  # noqa: E402
from ai_hydro.mcp import mcp  # noqa: E402
from ai_hydro.mcp.tool_surface import load_annotation_rows  # noqa: E402

ROWS = load_annotation_rows()
SID = "u5_runtime_fixture"
_RUNLOG_MARKERS = (".runlog.sqlite3",)
_HOUSEKEEPING = ("__pycache__", "matplotlib", "fontconfig", ".cache/", "fonts")


def allowed(ev: Event, roots: tuple[str, ...]) -> bool:
    detail = ev.detail
    if any(m in detail for m in _RUNLOG_MARKERS):
        return True
    if detail.startswith("/dev/null"):
        return True
    if any(h in detail for h in _HOUSEKEEPING) and ev.kind in ("write", "path"):
        return True
    if ev.name == "os.mkdir" and any(detail.startswith(r) for r in roots):
        return True
    return False


def violations(events, roots) -> list[str]:
    return sorted({str(e) for e in events if not allowed(e, roots)})


@pytest.fixture
def fixture_home(tmp_path, monkeypatch):
    from ai_hydro.session import chat_binding
    from ai_hydro.session import store as _store

    home = tmp_path / "home"
    (home / ".aihydro").mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # A tool's first lazy import compiles + caches a .pyc whenever the package has no
    # cached bytecode yet (fresh CI checkout/venv). The interpreter's import machinery
    # does that write via os-level fd (audit "open: <fd> mode='wb'"), not the tool, so
    # it is not a tool side effect; keep it out of the recording. All writes by tool
    # code itself are still recorded.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions)
    monkeypatch.setattr(chat_binding, "get_binding_store",
                        lambda: chat_binding.ChatBindingStore(tmp_path / "bindings.json"))
    s = _store.HydroSession.load(SID)
    s.add_note("fixture note")
    s.save()
    return (str(tmp_path), str(home))


def _args_for(tool) -> dict:
    from ai_hydro.mcp.tools_discovery import _example_value

    schema = tool.parameters or {}
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    args = {}
    for name, spec in props.items():
        if name == "session_id":
            args[name] = SID
        elif name in required:
            args[name] = _example_value(name, spec or {})
    return args


def _call_all(names, tools) -> dict[str, list[str]]:
    """Call each tool once (errors are fine), return {tool: [violating events]}."""
    from fastmcp import Client

    out: dict[str, Event] = {}

    async def go(roots):
        res = {}
        async with Client(mcp) as c:
            for n in names:
                with recording() as rec:
                    try:
                        await asyncio.wait_for(
                            c.call_tool(n, _args_for(tools[n]), raise_on_error=False), timeout=60)
                    except Exception:
                        pass
                res[n] = rec.events
        return res

    return go


@pytest.fixture
def record_network(monkeypatch):
    """Record (then still refuse) connection attempts the offline guard intercepts first."""
    seen: list[Event] = []
    real_connect, real_ex = socket.socket.connect, socket.socket.connect_ex
    real_gai, real_cc = socket.getaddrinfo, socket.create_connection

    def wrap(name, real):
        def inner(*a, **k):
            addr = a[1] if name.startswith("socket.socket") else (a[0] if a else "")
            seen.append(Event("network", name, str(addr)))
            return real(*a, **k)
        return inner

    monkeypatch.setattr(socket.socket, "connect", wrap("socket.socket.connect", real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", wrap("socket.socket.connect_ex", real_ex))
    monkeypatch.setattr(socket, "getaddrinfo", wrap("socket.getaddrinfo", real_gai))
    monkeypatch.setattr(socket, "create_connection", wrap("socket.create_connection", real_cc))
    return seen


def _run_tools(names, roots):
    tools = asyncio.run(mcp.get_tools())
    fn = _call_all(names, tools)
    return asyncio.run(fn(roots))


def test_read_only_tools_have_no_side_effects_at_runtime(fixture_home, record_network):
    names = sorted(n for n, r in ROWS.items() if r["readOnlyHint"] and n in asyncio.run(mcp.get_tools()))
    assert len(names) > 40
    before = {p: p.stat().st_mtime_ns for p in Path(fixture_home[0]).rglob("*") if p.is_file()
              and ".runlog.sqlite3" not in p.name}
    results = _run_tools(names, fixture_home)
    bad = {n: violations(ev, fixture_home) for n, ev in results.items()}
    bad = {n: v for n, v in bad.items() if v}
    assert not bad, f"readOnlyHint=true tools with runtime side effects: {json.dumps(bad, indent=1)}"
    assert not record_network, [str(e) for e in record_network]
    after = {p: p.stat().st_mtime_ns for p in Path(fixture_home[0]).rglob("*") if p.is_file()
             and ".runlog.sqlite3" not in p.name}
    assert after == before, sorted(str(p) for p in set(after) ^ set(before) | {p for p in after if after[p] != before.get(p)})


def test_mutation_every_mislabelled_writer_is_caught(fixture_home, record_network):
    """Relabel each non-read-only tool as read-only: the static or the runtime guard must flag it.

    The runtime pass calls only the tools the static guard misses, with schema example arguments
    against the fixture session. Tools that do nothing observable with those arguments (they refuse
    them) are reported by name in the failure so the guard can be strengthened.
    """
    from test_tool_surface import _static_violations

    tools = asyncio.run(mcp.get_tools())
    mislabels = sorted(n for n, r in ROWS.items() if not r["readOnlyHint"] and n in tools)
    static_caught = {n for n in mislabels if _static_violations(tools[n].fn)}
    rest = [n for n in mislabels if n not in static_caught]
    results = _run_tools(rest, fixture_home)
    runtime_caught = {n for n, ev in results.items() if violations(ev, fixture_home)}
    network_seen = bool(record_network)
    missed = sorted(set(rest) - runtime_caught)
    print(f"mislabels={len(mislabels)} static={len(static_caught)} runtime={len(runtime_caught)} missed={missed}")
    assert not missed, f"mislabelled writers neither guard catches: {missed}"
