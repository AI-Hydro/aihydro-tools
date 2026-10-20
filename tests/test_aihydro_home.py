"""One home (WP-0): every AI-Hydro state path honours ``AIHYDRO_HOME`` at call time.

Hermetic: ``HOME`` is pointed at a scratch dir and ``AIHYDRO_HOME`` at another, so a
call site that still hard-codes ``~/.aihydro`` would create ``<scratch HOME>/.aihydro``
and fail here. Nothing touches the real ``~/.aihydro``.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import pickle
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RESOLVER = ROOT / "ai_hydro" / "registry" / "paths.py"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    ah = tmp_path / "ah"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("AIHYDRO_HOME", str(ah))
    return fake_home, ah


def _assert_under(path, root):
    assert Path(path).resolve().is_relative_to(Path(root).resolve()), f"{path} not under {root}"


# ── guard ───────────────────────────────────────────────────────────────────

def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                out.add(id(body[0].value))
    return out


_DOT_AIHYDRO = re.compile(r"(?<![\w-])\.aihydro(?![\w-])")


def _fold(node):
    """Constant string value of a ``"a" + "b" + ...`` chain, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _hardcoded_home_literals(path):
    """(line, text) of every non-docstring string that spells the ``.aihydro`` directory.

    Best effort: plain and prefix constants (``".aihydro/sessions"``), constant parts of
    f-strings, and ``"."+"aihydro"`` style concatenation. ``.aihydrorules`` is a different
    directory and is allowed, as are ``vnd.aihydro.module`` and ``.aihydro-cell``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstring_nodes(tree)
    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if _DOT_AIHYDRO.search(node.value):
                hits.add((node.lineno, node.value))
        elif isinstance(node, ast.BinOp):
            folded = _fold(node)
            if folded is not None and _DOT_AIHYDRO.search(folded):
                hits.add((node.lineno, folded))
    return sorted(hits)


def test_no_hardcoded_aihydro_path_outside_the_resolver():
    """Only ``ai_hydro/registry/paths.py`` may spell the default ``.aihydro`` directory."""
    files = [*sorted((ROOT / "ai_hydro").rglob("*.py")), ROOT / "mcp_server.py", ROOT / "setup_mcp.py",
             *sorted((ROOT / "bench").rglob("*.py"))]
    offenders = {}
    for f in files:
        if f == RESOLVER:
            continue
        hits = _hardcoded_home_literals(f)
        if hits:
            offenders[str(f.relative_to(ROOT))] = hits
    assert not offenders, f"hard-coded .aihydro path outside the resolver: {offenders}"


def test_guard_detects_a_hardcoded_path(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text('"""doc ~/.aihydro"""\nfrom pathlib import Path\nX = Path.home() / ".aihydro" / "x"\n'
                   'Y = "~/.aihydro/cache"\nZ = ".aihydro/sessions"\nW = f"{X}/.aihydro/{Y}"\n'
                   'V = "." + "aihydro"\nU = ".aih" + "ydro"\nT = Path.home() / ("." + "aihydro" + "/x")\n'
                   'OK = ".aihydrorules"\nOK2 = "x" + ".aihydrorules"\n')
    found = {line for line, _ in _hardcoded_home_literals(bad)}
    assert found == {3, 4, 5, 6, 7, 8, 9}, found          # every evasion, and neither .aihydrorules line


def test_resolver_is_call_time_and_defaults(monkeypatch, tmp_path):
    from ai_hydro.registry.paths import aihydro_home, home_path

    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "a"))
    lazy = home_path("sessions")
    assert Path(lazy) == tmp_path / "a" / "sessions"
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "b"))
    assert Path(lazy) == tmp_path / "b" / "sessions"          # same object, re-resolved
    assert Path(lazy / "x.json") == tmp_path / "b" / "sessions" / "x.json"
    monkeypatch.delenv("AIHYDRO_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert aihydro_home() == Path.home() / ".aihydro"


# ── behaviour: state lands under AIHYDRO_HOME ───────────────────────────────

def test_session_create_load_lands_under_home(homes):
    from ai_hydro.session.store import HydroSession

    fake_home, ah = homes
    s = HydroSession("home-sid")
    s.save()
    assert (ah / "sessions" / "home-sid.json").is_file()
    assert HydroSession.load("home-sid").session_id == "home-sid"
    assert not (fake_home / ".aihydro").exists()


def test_run_log_lands_under_home(homes):
    from ai_hydro.session import store

    fake_home, ah = homes
    entry = {"tool_name": "t", "timestamp": "1", "key_outputs": {"a": 1}}
    store._run_log_record("home-sid", "r1", entry)
    db = store._run_log_db_path("home-sid")
    _assert_under(db, ah)
    assert db.is_file()
    assert store._run_log_read_one("home-sid", "r1")["key_outputs"] == {"a": 1}
    assert not (fake_home / ".aihydro").exists()


def test_ledger_event_lands_under_home(homes):
    from ai_hydro.mcp.ledger_commands import push_claim_event

    fake_home, ah = homes
    assert push_claim_event(change_type="added", session_id="home-sid", claim_id="c1")
    files = list((ah / "ledger_events").glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["claim_id"] == "c1"
    assert not (fake_home / ".aihydro").exists()


def test_approval_store_read_resolves_under_home(homes):
    from ai_hydro.approval import records

    fake_home, ah = homes
    _assert_under(records.approvals_file(), ah)
    assert list(records.iter_approvals()) == []
    assert not (fake_home / ".aihydro").exists()


def test_other_state_dirs_resolve_under_home(homes):
    """Map, comments, citations, data_jobs, chat studies, skills, knowledge, persona, projects."""
    from ai_hydro.knowledge import embeddings
    from ai_hydro.mcp import (
        map_commands,
        map_events,
        map_layer_catalog,
        tools_citations,
        tools_map,
        tools_preview,
    )
    from ai_hydro.mcp.tools_data_async import _artifact_dir
    from ai_hydro.session import chat_binding, persona, project
    from ai_hydro.skills import registry as skills

    fake_home, ah = homes
    for p in (map_commands._MAP_COMMANDS_DIR, map_events._MAP_EVENTS_DIR, map_layer_catalog.MAP_LAYER_CATALOG_FILE,
              tools_map._MAP_SESSION_FILE, tools_map._MAP_EVENTS_OUTBOUND, tools_citations._CACHE_DIR,
              tools_preview._COMMENTS_DIR, tools_preview._PREVIEW_EVENTS_DIR, embeddings.PASSAGE_INDEX_PATH,
              skills._USER_SKILLS_DIR, persona._PROFILE_PATH, project._PROJECTS_DIR, chat_binding._binding_file(),
              _artifact_dir("job-1")):
        _assert_under(p, ah)
    assert not (fake_home / ".aihydro").exists()


# ── absolute home, pickle/copy ──────────────────────────────────────────────

def test_relative_aihydro_home_is_absolute_and_survives_chdir(tmp_path, monkeypatch):
    from ai_hydro.registry.paths import aihydro_home, home_path, pin_aihydro_home

    start = tmp_path / "start"
    elsewhere = tmp_path / "elsewhere"
    start.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(start)
    monkeypatch.setenv("AIHYDRO_HOME", "rel-home")
    assert aihydro_home().is_absolute()
    pin_aihydro_home()                        # what the server entry points do before chdir
    assert os.environ["AIHYDRO_HOME"] == str(start / "rel-home")
    before, lazy_before = aihydro_home(), Path(home_path("cache"))
    assert before.is_absolute() and before == start / "rel-home"
    os.chdir(elsewhere)                       # mcp_server chdirs into the cache dir after startup
    assert aihydro_home() == before
    assert Path(home_path("cache")) == lazy_before


def test_home_path_copies_and_pickles(tmp_path, monkeypatch):
    from ai_hydro.registry.paths import HomePath, home_path

    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "a"))
    lazy = home_path("sessions") / "x"
    for clone in (copy.copy(lazy), copy.deepcopy(lazy), pickle.loads(pickle.dumps(lazy))):
        assert isinstance(clone, HomePath)
        assert Path(clone) == tmp_path / "a" / "sessions" / "x"
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "b"))
    assert Path(pickle.loads(pickle.dumps(lazy))) == tmp_path / "b" / "sessions" / "x"   # still lazy
    with pytest.raises(AttributeError):
        lazy._nope
