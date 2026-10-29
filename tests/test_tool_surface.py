"""U5a: MCP tool annotations, deterministic tools/list, hot-only surface, description cap,
and the audit's refusal to guess a study.

The table under test is ``ai_hydro/mcp/tool_annotations.toml``; the mechanism is
``ai_hydro/mcp/tool_surface.py``. Everything runs in memory (``fastmcp.Client(mcp)``): no
network, no subprocess server, except the shuffled-construction test which imports the server
twice in child interpreters.
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ai_hydro.mcp import mcp
from ai_hydro.mcp.tool_surface import (
    ANNOTATIONS_PATH, SURFACE_ENV, _parse_minimal_toml, load_annotation_rows,
)

ROWS = load_annotation_rows()
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
PLUGIN_MODULES = {"aihydro-data": "aihydro_data", "aihydro-lsh": "aihydro_lsh"}


def _run(coro):
    return asyncio.run(coro)


def _registered() -> dict:
    return _run(mcp.get_tools())


async def _client_list(m=mcp):
    from fastmcp import Client

    async with Client(m) as c:
        return await c.list_tools()


# ── table <-> registration ──────────────────────────────────────────────────

def _expected_names() -> set[str]:
    """Rows that must exist as tools: builtin rows, plus rows of each importable plugin."""
    out = set()
    for name, row in ROWS.items():
        src = row["source"]
        if src == "builtin" or importlib.util.find_spec(PLUGIN_MODULES[src]) is not None:
            out.add(name)
    return out


def test_every_registered_tool_has_a_row_and_every_row_has_a_tool():
    registered = set(_registered())
    assert registered - set(ROWS) == set(), "registered tool without a tool_annotations.toml row"
    assert _expected_names() - registered == set(), "row without a registered tool"


def test_rows_are_complete_and_coherent():
    for name, row in ROWS.items():
        assert all(isinstance(row[h], bool) for h in HINTS), name
        assert row["evidence"].strip(), name
        assert row["source"] in ("builtin", *PLUGIN_MODULES), name
        if row["readOnlyHint"]:
            # a tool that writes nothing and calls no network can neither destroy nor reach out
            assert not row["destructiveHint"] and not row["openWorldHint"], name
            assert row["idempotentHint"], name


def test_minimal_toml_parser_matches_tomllib():
    text = ANNOTATIONS_PATH.read_text(encoding="utf-8")
    try:
        import tomllib
    except ImportError:  # pragma: no cover - 3.10
        pytest.skip("tomllib not available")
    assert _parse_minimal_toml(text) == tomllib.loads(text)


def test_minimal_toml_parser_rejects_duplicate_tables():
    text = '["a"]\nreadOnlyHint = true\n\n["a"]\nreadOnlyHint = false\n'
    with pytest.raises(ValueError, match="duplicate table"):
        _parse_minimal_toml(text)


def test_annotations_are_served_in_tools_list():
    listed = _run(_client_list())
    by_name = {t.name: t for t in listed}
    assert set(by_name) == set(_registered())
    for name, tool in by_name.items():
        row = ROWS[name]
        assert tool.annotations is not None, name
        for hint in HINTS:
            assert getattr(tool.annotations, hint) is row[hint], (name, hint)


# ── no writer is marked read-only ───────────────────────────────────────────

_WRITE_ATTRS = {
    "save", "write_text", "write_bytes", "unlink", "rmtree", "touch", "to_netcdf", "to_parquet",
    "to_csv", "savefig", "set_slot", "bind", "Popen", "write_map_command", "write_preview_command",
    "write_ledger_event", "persist_streamflow_slot", "write_workspace_file",
    "write_session_data_file", "record_change", "register", "set_active", "cancel_job",
    "archive", "export_bibtex", "start_job", "submit_job", "submit", "cancel", "mkdir", "makedirs",
    "rmdir", "to_file", "to_json_file", "dump", "check_call", "check_output", "system", "popen",
    "run_cell", "exec_module",
}
# Helpers whose side effects are infrastructure by the rules in the annotations header: session
# resolution bookkeeping (itself disabled for read-only calls at run time) and cache-root creation.
_INFRA_HELPERS = {
    "_resolve_session", "_resolve_session_with_rule", "_resolve_session_impl", "_maybe_set_workspace",
    "cache_dir", "_cache_dir", "_artifact_dir",
}
_FLAGGED_QUALIFIED = {
    "os.remove", "os.rename", "os.replace", "os.rmdir", "os.unlink", "os.makedirs", "os.mkdir",
    "os.system", "os.popen", "os.truncate", "os.chmod", "jobs.start_job", "jobs.submit_job",
    "jobs.cancel_job", "jobs.create_job", "jobs.launch", "jobs.run_job",
}
_WRITE_NAMES = {
    "write_map_command", "write_preview_command", "write_ledger_event", "persist_streamflow_slot",
    "_save_experiments", "_save_progress", "_write_all", "rmtree", "_save", "save_skill",
    "_write_csv", "_workspace_write", "_write_tools_md", "start_job", "submit_job", "Popen",
    "exec", "eval", "execfile", "system", "_run_feature", "_launch_job", "_start_job", "MeritDataManager",
}
# Provenance recording of the call itself (see the header of tool_annotations.toml).
_EXCLUDED = {"_write_run_log", "post_run", "_post_run"}
_NET_ATTRS = {"urlopen", "Initialize", "getInfo", "urlretrieve", "data_fetch", "fetch_dem", "fetch"}
_NET_PREFIXES = ("fetch_", "download", "nldi_", "stac_", "ensure_")
_BASES_FLAGGED = {"subprocess", "ee", "requests", "httpx", "urllib", "jobs", "shutil", "os"}


def _resolve_local_imports(tree, fn) -> dict[str, object]:
    """Objects bound by imports inside the function body (function-local imports)."""
    import importlib

    bound: dict[str, object] = {}
    for node in ast.walk(tree):
        try:
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mod = importlib.import_module(node.module)
                for a in node.names:
                    bound[a.asname or a.name] = getattr(mod, a.name, None)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    bound[(a.asname or a.name).split(".")[0]] = importlib.import_module(a.name.split(".")[0])
        except Exception:
            continue
    return bound


def _own_calls(fn, depth: int = 3, _seen=None) -> set[str]:
    """Names of calls in the tool body and in the helpers it calls (AST, no strings).

    Helpers are followed through module globals, function-local imports and ``module.func`` calls.
    """
    _seen = _seen if _seen is not None else set()
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return set()
    names: set[str] = set()
    targets: list[object] = []
    local = _resolve_local_imports(tree, fn)
    mod = inspect.getmodule(fn)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            names.add(f.attr)
            if isinstance(f.value, ast.Name):
                base = f.value.id
                if base in _BASES_FLAGGED:
                    names.add(f"{base}.{f.attr}")
                obj = local.get(base) or getattr(mod, base, None)
                target = getattr(obj, f.attr, None) if inspect.ismodule(obj) else None
                if inspect.isfunction(target):
                    targets.append(target)
        elif isinstance(f, ast.Name):
            names.add(f.id)
            target = local.get(f.id) or getattr(mod, f.id, None)
            if inspect.isfunction(target):
                targets.append(target)
        if isinstance(f, ast.Name) and f.id == "open" and len(node.args) > 1:
            mode = node.args[1]
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and set(mode.value) & set("wax"):
                names.add("open(write)")
    if depth > 0:
        for target in targets:
            if not getattr(target, "__module__", "").startswith(("ai_hydro", "aihydro_")):
                continue
            key = (target.__module__, target.__qualname__)
            if target.__name__ in _INFRA_HELPERS:
                continue
            if key not in _seen:
                _seen.add(key)
                names |= _own_calls(target, depth - 1, _seen)
    return names


def _static_violations(fn) -> set[str]:
    calls = _own_calls(fn) - _EXCLUDED
    bad = (calls & _WRITE_ATTRS) | (calls & _WRITE_NAMES) | (calls & _NET_ATTRS)
    bad |= {c for c in calls if c.startswith(_NET_PREFIXES) or c == "open(write)"}
    bad |= {c for c in calls if c.split(".")[0] in ("subprocess", "ee", "requests", "httpx", "urllib")
            and "." in c}
    bad |= {c for c in calls if c.startswith("shutil.") and c != "shutil.which"}
    bad |= calls & _FLAGGED_QUALIFIED
    return bad


def test_no_read_only_tool_calls_a_known_writer_or_the_network():
    tools = _registered()
    offenders = {}
    for name, row in ROWS.items():
        if not row["readOnlyHint"] or name not in tools:
            continue
        bad = _static_violations(tools[name].fn)
        if bad:
            offenders[name] = sorted(bad)
    assert not offenders, f"readOnlyHint=true but the code writes or reaches the network: {offenders}"


def test_known_writers_are_not_read_only():
    for name in ("add_claim", "add_note", "compute_twi", "delineate_watershed", "export_session",
                 "clear_session", "write_research_interpretation", "define_experiment",
                 "map_show", "save_skill", "train_hydro_model", "data_fetch"):
        assert not ROWS[name]["readOnlyHint"], name
    for name in ("clear_session", "cancel_job", "run_python", "data_invalidate_cache",
                 "write_research_interpretation", "add_claim"):
        assert ROWS[name]["destructiveHint"], name
    for name in ("data_fetch", "lookup_citation", "run_python", "lsh_attributes"):
        assert ROWS[name]["openWorldHint"], name


# ── deterministic order ─────────────────────────────────────────────────────

def test_tools_list_is_sorted_by_name():
    names = [t.name for t in _run(_client_list())]
    assert names == sorted(names)


def _fresh_server(order: list[str]):
    """A FastMCP with the real tool functions registered in ``order`` and the surface installed."""
    from fastmcp import FastMCP

    from ai_hydro.mcp.tool_surface import install_tool_surface

    tools = _registered()
    m = FastMCP(name="shuffle-test")
    for name in order:
        m.tool(name=name)(tools[name].fn)
    install_tool_surface(m)
    return m


def _dump(listed) -> bytes:
    return json.dumps([t.model_dump(mode="json", exclude_none=True) for t in listed], sort_keys=False).encode()


def test_shuffled_registration_gives_byte_identical_tools_list():
    names = sorted(n for n in _registered() if not n.startswith("gee."))[:40]
    forward, backward = list(names), list(reversed(names))
    interleaved = names[0::2] + names[1::2]
    dumps = [_dump(_run(_client_list(_fresh_server(order)))) for order in (forward, backward, interleaved)]
    assert dumps[0] == dumps[1] == dumps[2]
    assert len(json.loads(dumps[0])) == len(names)


_CHILD = r"""
import hashlib, json, sys, asyncio, importlib.metadata as md
order = sys.argv[1]
real = md.entry_points
def shuffled(*a, **k):
    eps = real(*a, **k)
    if hasattr(eps, "select"):
        eps = list(eps)
    items = sorted(eps, key=lambda e: e.name, reverse=(order == "reverse"))
    return items
md.entry_points = shuffled
import ai_hydro.mcp as m
from fastmcp import Client
async def main():
    async with Client(m.mcp) as c:
        ts = await c.list_tools()
    blob = json.dumps([t.model_dump(mode="json", exclude_none=True) for t in ts]).encode()
    print(len(ts), hashlib.sha256(blob).hexdigest())
asyncio.run(main())
"""


def test_entry_point_order_does_not_change_the_real_server_tools_list():
    outs = []
    for order, seed in (("forward", "1"), ("reverse", "2")):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        res = subprocess.run([sys.executable, "-c", _CHILD, order], capture_output=True, text=True,
                             env=env, timeout=600)
        assert res.returncode == 0, res.stderr[-2000:]
        outs.append(res.stdout.strip().splitlines()[-1])
    assert outs[0] == outs[1], outs


# ── AIHYDRO_TOOL_SURFACE ────────────────────────────────────────────────────

def test_default_surface_is_full(monkeypatch):
    monkeypatch.delenv(SURFACE_ENV, raising=False)
    assert {t.name for t in _run(_client_list())} == set(_registered())


def test_hot_surface_lists_hot_tools_and_discovery_only(monkeypatch):
    from ai_hydro.mcp.app import HOT_TOOL_ALLOWLIST, is_hot_tool

    full = _run(_client_list())
    monkeypatch.setenv(SURFACE_ENV, "hot")
    hot = _run(_client_list())
    names = [t.name for t in hot]
    assert names == sorted(names)
    assert set(names) == {t.name for t in full if is_hot_tool(t.name)}
    assert {"describe_tool", "describe_tools", "list_available_tools",
            "aihydro_describe_capability"} <= set(names)
    assert HOT_TOOL_ALLOWLIST <= set(names)
    assert len(hot) < len(full) / 2
    assert len(_dump(hot)) < len(_dump(full)) / 2


def test_unknown_surface_value_means_full(monkeypatch):
    monkeypatch.setenv(SURFACE_ENV, "everything")
    assert len(_run(_client_list())) == len(_registered())


def test_a_tool_left_out_of_the_hot_listing_is_still_callable(monkeypatch):
    from fastmcp import Client

    monkeypatch.setenv(SURFACE_ENV, "hot")

    async def go():
        async with Client(mcp) as c:
            listed = {t.name for t in await c.list_tools()}
            assert "list_known_metrics" not in listed
            hidden = await c.call_tool("list_known_metrics", {}, raise_on_error=False)
            described = await c.call_tool("describe_tool", {"name": "list_known_metrics"},
                                          raise_on_error=False)
            return hidden, described

    hidden, described = _run(go())
    assert not hidden.is_error
    assert not described.is_error and "list_known_metrics" in json.dumps(described.data, default=str)


# ── explicit hints on every tool, rowed or not ───────────────────────────────

def test_every_served_tool_has_explicit_read_only_and_destructive_hints():
    """Fails when a tool reaches tools/list with readOnlyHint or destructiveHint unset."""
    for tool in _run(_client_list()):
        ann = tool.annotations
        assert ann is not None, tool.name
        assert isinstance(ann.readOnlyHint, bool), (tool.name, "readOnlyHint")
        assert isinstance(ann.destructiveHint, bool), (tool.name, "destructiveHint")


def test_a_tool_without_a_row_gets_conservative_hints_not_none():
    from fastmcp import FastMCP

    from ai_hydro.mcp.tool_surface import UNROWED_DEFAULTS, apply_tool_annotations

    m = FastMCP("t")

    @m.tool
    def plugin_tool() -> str:
        return "x"

    @m.tool(annotations={"readOnlyHint": True})
    def declares_own() -> str:
        return "x"

    assert apply_tool_annotations(m, rows={}) == 2
    got = {t.name: t.annotations for t in _run(_client_list(m))}
    for hint, val in UNROWED_DEFAULTS.items():
        assert getattr(got["plugin_tool"], hint) is val, hint
    assert got["plugin_tool"].readOnlyHint is False and got["plugin_tool"].destructiveHint is True
    assert got["declares_own"].readOnlyHint is True  # a hint the tool declares itself is kept
    assert got["declares_own"].destructiveHint is True  # the rest are filled conservatively


def test_representative_tools_of_each_class():
    expect = {  # tool -> (readOnly, destructive, openWorld)
        "list_claims": (True, False, False),            # pure read of session state
        "get_session_summary": (True, False, False),
        "data_list_products": (True, False, False),     # aihydro-data, catalogue read
        "clear_session": (False, True, False),          # deletes session state
        "update_claim_status": (False, True, False),    # overwrites researcher-authored state
        "run_python": (False, True, True),              # arbitrary code, unknown side effects
        "fetch_streamflow_data": (False, False, True),  # network + derived slot write
        "map_set_basemap": (False, False, False),       # local map command file
    }
    reg = _registered()
    for name, (ro, de, ow) in expect.items():
        if name not in reg:
            continue
        a = reg[name].annotations
        assert (a.readOnlyHint, a.destructiveHint, a.openWorldHint) == (ro, de, ow), name


# ── T-A1: annotation vocabulary (effect / requiresConsent / audience), data only ─────────────

_META_PREFIX = "aihydro/"
_VOCAB_KEYS = ("effect", "requiresConsent", "audience")
_VOCAB_VALUES = {"effect": {"ui"}, "audience": {"host"}}
_SESSION_WRITERS = {"save", "_ensure_session", "_resolve_session", "_workspace_write", "HydroSession",
                    "add_note", "record_change", "set_slot", "write_ledger_event"}
_COMMAND_WRITERS = {"write_map_command", "write_preview_command", "mkdir", "write_text"}


def _rows_with(key: str, value=True) -> set[str]:
    return {n for n, r in ROWS.items() if r.get(key) == value and (
        r["source"] == "builtin" or importlib.util.find_spec(PLUGIN_MODULES[r["source"]]) is not None)}


def test_every_tool_has_a_row_and_the_vocabulary_is_closed():
    """Each class is optional per row, but a row may use only the three keys and known values."""
    known = set(HINTS) | {"source", "evidence"} | set(_VOCAB_KEYS)
    for name, row in ROWS.items():
        assert set(row) <= known, (name, sorted(set(row) - known))
        for key, allowed in _VOCAB_VALUES.items():
            if key in row:
                assert row[key] in allowed, (name, key, row[key])
        if "requiresConsent" in row:
            assert row["requiresConsent"] is True, name        # absence means false; never write false
    assert set(_registered()) <= set(ROWS)


def test_vocabulary_is_served_in_tools_list_meta_and_only_there():
    listed = {t.name: t for t in _run(_client_list())}
    for name, tool in listed.items():
        meta = tool.meta or {}
        served = {k: v for k, v in meta.items() if k.startswith(_META_PREFIX)}
        expected = {f"{_META_PREFIX}{k}": ROWS[name][k] for k in _VOCAB_KEYS if k in ROWS[name]}
        assert served == expected, name
    # the tool-specific classes the program names must exist
    assert {"map_fly_to", "map_fit_extent", "map_set_time_range"} <= _rows_with("effect", "ui")
    assert {"log_researcher_observation", "update_researcher_profile"} <= _rows_with("requiresConsent")


def test_vocabulary_changes_no_hint():
    """Annotating a class never flips a hint: the hint columns of those rows are the audited ones."""
    snapshot = {n: tuple(r[h] for h in HINTS) for n, r in ROWS.items()}
    for n in _rows_with("effect", "ui") | _rows_with("requiresConsent"):
        assert snapshot[n][0] is False, n                       # none of these claims read-only
    listed = {t.name: t for t in _run(_client_list())}
    for n, tool in listed.items():
        assert tuple(getattr(tool.annotations, h) for h in HINTS) == snapshot[n], n


def test_ui_class_is_only_ui_by_static_scan():
    """A `ui` tool reaches only the map/preview command writers: no session, claim, workspace or network."""
    tools = _registered()
    ui = _rows_with("effect", "ui")
    assert len(ui) >= 10
    for name in sorted(ui):
        assert name.startswith(("map_", "preview_")), name
        calls = _own_calls(tools[name].fn)
        assert not (calls & _SESSION_WRITERS), (name, sorted(calls & _SESSION_WRITERS))
        other_writers = (calls & (_WRITE_ATTRS | _WRITE_NAMES)) - _COMMAND_WRITERS
        assert not other_writers, (name, sorted(other_writers))
        assert not (_static_violations(tools[name].fn) - _COMMAND_WRITERS), name
        assert not (ROWS[name]["openWorldHint"] or ROWS[name]["destructiveHint"]), name


def test_ui_class_writes_only_command_files_at_runtime(tmp_path, monkeypatch):
    """Run each `ui` tool whose arguments are simple, recording file/process/network events."""
    sys.path.insert(0, str(Path(__file__).parent))
    from _runtime_guard import recording
    import ai_hydro.mcp.map_commands as mc
    import ai_hydro.mcp.preview_commands as pc
    import ai_hydro.mcp.tools_map as tm

    map_dir, prev_dir = tmp_path / "map_commands", tmp_path / "preview_commands"
    monkeypatch.setattr(mc, "_MAP_COMMANDS_DIR", map_dir)
    monkeypatch.setattr(pc, "_PREVIEW_COMMANDS_DIR", prev_dir)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(tm, "find_catalog_layer", lambda lid: {"id": lid, "type": "vector"})
    # a seeded scratch session with a run log (map_add_layer_from_run reads it, writes nothing)
    from ai_hydro.session import store as _store
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "basin.geojson").write_text(json.dumps({"type": "Point", "coordinates": [0.0, 1.0]}))
    (sessions / "s1.json").write_text(json.dumps({"session_id": "s1", "workspace_dir": str(ws), "artifact_manifest": {
        "file:basin.geojson": {"artifact_id": "file:basin.geojson", "kind": "workspace_artifact",
                               "type": "workspace_file", "path": "basin.geojson", "run_id": "run1", "sha256": None}}}))
    _store._run_log_record("s1", "run1", {"tool_name": "demo"})   # the run row lives in the run store
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {"v": float(i)}}
        for i in range(10)]}
    monkeypatch.setattr(tm, "load_geojson_for_layer", lambda entry, root: fc)
    calls = {
        "map_show": {}, "map_fit_extent": {}, "map_fit_layer": {"layer_id": "L1"},
        "map_fly_to": {"longitude": -70.0, "latitude": 45.0},
        "map_set_basemap": {"basemap_id": "esri-imagery"},
        "map_set_time_range": {"start_iso": "2020-01-01", "end_iso": "2020-12-31"},
        "map_remove_layer": {"layer_id": "L1"},
        "map_update_layer": {"layer_id": "L1", "fill_color": "#336699", "visible": True},
        "map_add_layer_from_run": {"session_id": "s1", "run_id": "run1"},
        "map_apply_symbology": {"layer_id": "L1", "attribute": "v"},
        "preview_focus_cell": {"module_id": "m1", "cell_id": "c1"},
        "preview_revise_section": {"module_id": "m1", "section_id": "s1", "new_html": "<p>x</p>"},
        "preview_address_comment": {"module_id": "m1", "comment_id": "k1"},
    }
    assert set(calls) <= _rows_with("effect", "ui")
    session_before = {p.name: p.read_bytes() for p in sessions.iterdir()}

    async def go():
        from fastmcp import Client
        out = {}
        async with Client(mcp) as c:
            for n, args in calls.items():
                with recording() as rec:
                    r = await c.call_tool(n, args, raise_on_error=False)
                out[n] = (r, list(rec.events))
        return out

    for n, (res, events) in _run(go()).items():
        assert not res.is_error, n
        assert res.structured_content.get("ok", res.structured_content.get("success")) is True, n
        bad = [str(e) for e in events if e.kind in ("process", "network") or not (
            str(map_dir) in e.detail or str(prev_dir) in e.detail or ".runlog.sqlite3" in e.detail
            or "__pycache__" in e.detail)]
        assert not bad, (n, bad)
    # map_update_layer(visible=...) queues two commands (style, then visibility)
    assert len(list(map_dir.glob("*.json"))) == 11 and len(list(prev_dir.glob("*.json"))) == 3
    assert {p.name: p.read_bytes() for p in sessions.iterdir()} == session_before   # no session write
    assert set(calls) == _rows_with("effect", "ui"), "every ui tool is exercised"


def test_map_add_layer_from_run_cannot_traverse_out_of_the_sessions_dir(tmp_path, monkeypatch):
    import ai_hydro.mcp.map_commands as mc
    import ai_hydro.mcp.tools_map as tm
    from ai_hydro.session import store as _store

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions)
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path))     # so "<home>/sessions/../evil.json" would be this file
    monkeypatch.setattr(mc, "_MAP_COMMANDS_DIR", tmp_path / "map_commands")
    # a well-formed run log OUTSIDE the sessions dir: reachable only through "../"
    (tmp_path / "evil.json").write_text(json.dumps({"_run_log": {"run1": {
        "tool_name": "demo", "key_outputs": {"basin": {"type": "Point", "coordinates": [0.0, 1.0]}}}}}))
    for sid in ("../evil", "..\\evil", "../../evil", "a/../../evil"):
        out = tm.map_add_layer_from_run(session_id=sid, run_id="run1")
        assert out["ok"] is False, sid
    assert not (tmp_path / "map_commands").exists()


def test_host_audience_is_a_mechanism_without_tools_yet():
    """`audience = "host"` is defined (closed vocabulary, `_meta` key); no tool claims it yet, and
    the mechanism serves it as it would any other key (no code branches on a tool name)."""
    from ai_hydro.mcp.tool_surface import META_KEYS, apply_tool_annotations

    assert META_KEYS["audience"] == "aihydro/audience"
    assert _rows_with("audience", "host") == set()

    class _Comp:
        name, meta, annotations = "t", None, None
    comp = _Comp()
    holder = type("M", (), {"_local_provider": type("P", (), {"_components": {"k": comp}})()})()
    row = {h: False for h in HINTS} | {"source": "builtin", "evidence": "x", "audience": "host"}
    apply_tool_annotations(holder, {"t": row})
    assert comp.meta == {"aihydro/audience": "host"}


def test_class_membership_is_stable_for_the_audited_lists():
    """The audited lists: a change here is a deliberate, reviewed edit of the table."""
    assert _rows_with("effect", "ui") == {
        "map_add_layer_from_run", "map_apply_symbology", "map_fit_extent", "map_fit_layer", "map_fly_to",
        "map_remove_layer", "map_set_basemap", "map_set_time_range", "map_show", "map_update_layer",
        "preview_address_comment", "preview_focus_cell", "preview_revise_section",
    }
    assert _rows_with("requiresConsent") == {"log_researcher_observation", "update_researcher_profile"}
