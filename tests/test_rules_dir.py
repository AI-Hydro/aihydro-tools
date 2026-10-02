"""``.aihydrorules`` generated files never land beside the code checkout.

``research.md`` (session/project digest) and ``tools.md`` (live tool registry) used to
be written relative to the package checkout when a session had no ``workspace_dir``.
They now resolve through ``ai_hydro.registry.paths.rules_dir``: the session workspace
when set, else ``AIHYDRO_HOME``. Synthetic fixtures only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import ai_hydro
from ai_hydro.registry.paths import RULES_DIR_NAME, aihydro_home, rules_dir
from ai_hydro.session import project as project_mod
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession

CHECKOUT_PARENT = Path(ai_hydro.__file__).resolve().parent.parent.parent   # the old _REPO_ROOT


def _snapshot(root: Path):
    """(relative path, size, mtime_ns) of every file under ``root/.aihydrorules``."""
    base = root / RULES_DIR_NAME
    if not base.exists():
        return None
    return sorted((str(p.relative_to(base)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in base.rglob("*") if p.is_file())


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Fresh HOME + AIHYDRO_HOME + session/project dirs, cwd inside the scratch tree."""
    home = tmp_path / "home"
    ah = tmp_path / "aihydro_home"
    home.mkdir()
    ah.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AIHYDRO_HOME", str(ah))
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(project_mod, "_PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_rules_dir_resolution(scratch, monkeypatch):
    ws = scratch / "ws"
    assert rules_dir(ws) == ws / RULES_DIR_NAME
    assert rules_dir(str(ws)) == ws / RULES_DIR_NAME
    assert rules_dir() == aihydro_home() / RULES_DIR_NAME == scratch / "aihydro_home" / RULES_DIR_NAME
    assert rules_dir(None) == rules_dir("")
    other = scratch / "other_home"
    monkeypatch.setenv("AIHYDRO_HOME", str(other))                  # resolved at call time
    assert rules_dir() == other / RULES_DIR_NAME
    assert CHECKOUT_PARENT not in rules_dir().parents


def test_server_start_and_session_save_write_nothing_beside_the_checkout(scratch, monkeypatch):
    import ai_hydro.mcp as mcp_pkg
    from ai_hydro.mcp.app import mcp as server

    before = _snapshot(CHECKOUT_PARENT)
    monkeypatch.setattr(server, "run", lambda *a, **k: None)         # start up, do not serve
    monkeypatch.setattr("sys.argv", ["aihydro-mcp"])
    mcp_pkg.main()

    s = HydroSession("noworkspace")                                   # no workspace_dir
    s.interpretation = "synthetic"
    s.save()

    ah_rules = scratch / "aihydro_home" / RULES_DIR_NAME
    assert (ah_rules / "tools.md").is_file()
    assert "noworkspace" in (ah_rules / "research.md").read_text()
    assert _snapshot(CHECKOUT_PARENT) == before


def test_workspace_session_writes_research_md_in_its_workspace_only(scratch):
    ws = scratch / "ws"
    ws.mkdir()
    before = _snapshot(CHECKOUT_PARENT)
    s = HydroSession("withws")
    s.workspace_dir = str(ws)
    s.save()
    assert "withws" in (ws / RULES_DIR_NAME / "research.md").read_text()
    assert not (scratch / "aihydro_home" / RULES_DIR_NAME / "research.md").exists()
    assert _snapshot(CHECKOUT_PARENT) == before


def test_project_digest_uses_the_same_resolver(scratch):
    ws = scratch / "ws"
    ws.mkdir()
    before = _snapshot(CHECKOUT_PARENT)
    p = project_mod.ProjectSession("proj1")
    p.save()                                                          # no workspace: AIHYDRO_HOME
    assert "proj1" in (scratch / "aihydro_home" / RULES_DIR_NAME / "research.md").read_text()
    p.write_research_context(workspace_dir=str(ws))
    assert "proj1" in (ws / RULES_DIR_NAME / "research.md").read_text()
    assert _snapshot(CHECKOUT_PARENT) == before

