"""Bench and test runs never write the user's registry (Slice 1b, acceptance 9)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _tree(root: Path) -> dict:
    return {str(p): p.stat().st_mtime_ns for p in sorted(root.rglob("*"))} if root.exists() else {}


def test_suite_uses_an_isolated_aihydro_home(_isolated_aihydro_home):
    from ai_hydro.registry import store
    home = Path(os.environ["AIHYDRO_HOME"])
    assert home == _isolated_aihydro_home
    assert store.claims_file() == home / "registry" / "claims.jsonl"
    assert Path.home() / ".aihydro" not in (home, *home.parents)


def test_in_process_bench_promotion_writes_only_the_isolated_home(tmp_path, monkeypatch, _isolated_aihydro_home):
    """Run the real B-045 promotion task and watch both candidate locations."""
    import yaml
    from ai_hydro.registry import store as registry
    import ai_hydro.session.store as session_store

    real = Path.home() / ".aihydro" / "registry"
    real_before = _tree(real)

    monkeypatch.setattr(session_store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_store, "_SESSIONS_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()

    task = next(t for t in yaml.safe_load((REPO / "bench" / "tasks.yaml").read_text())["tasks"]
                if t["id"] == "B-045")
    from approval_helpers import approve
    from ai_hydro.session.store import HydroSession
    setup = task["setup"]
    sess = HydroSession(setup["session_id"])
    for name, data in setup["slots"].items():
        sess.set(name, data)
    for cid, data in setup["claims"].items():
        sess.claims[cid] = data
    sess.save()
    for cid in setup["approve_claims"]:
        approve(setup["session_id"], cid)

    from ai_hydro.mcp.tools_ledger import promote_claim_to_registry
    res = promote_claim_to_registry(**task["call"]["kwargs"])
    assert res["status"] == "promoted", res

    assert (_isolated_aihydro_home / "registry" / "claims.jsonl").exists()
    assert registry.claims_file().parent == _isolated_aihydro_home / "registry"
    assert _tree(real) == real_before, "bench promotion touched the user's real registry path"


def test_real_bench_run_with_a_fake_home_leaves_no_registry_behind(tmp_path):
    """Clean interpreter, HOME pointing at a fake user dir, no AIHYDRO_HOME set by the caller.

    Runs the actual bench promotion tasks. ``tests/conftest.py`` must route the
    registry and approvals away from ``$HOME/.aihydro``.
    """
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "AIHYDRO_HOME"}
    env.update(HOME=str(fake_home),
               PYTHONPATH=os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH", "")])))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_bench.py",
         "-m", "bench", "-k", "B-016 or B-045"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1500:]
    assert "2 passed" in proc.stdout, proc.stdout[-1500:]
    assert not (fake_home / ".aihydro" / "registry").exists()
    assert not (fake_home / ".aihydro" / "approvals").exists()
