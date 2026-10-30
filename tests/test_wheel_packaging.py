"""Built-wheel packaging guard (R-06, mechanism M6).

A test that runs from the source tree cannot see missing package data, because
the YAML/JSON files exist there. This test builds the wheel, installs it into an
isolated directory, and checks the *installed* copy: every tracked data file
shipped, the knowledge and workflow registries are non-empty, and one tool of
each kind answers.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("wheel")
    # Build from a clean copy of the working tree: an in-tree build leaves
    # build/ and *.egg-info behind and a later run would reuse stale files.
    src = tmp_path_factory.mktemp("src")
    try:
        files = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout; cannot stage a clean build tree")
    for rel in files:
        f = REPO / rel
        if f.is_file():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, src / rel)
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(out), str(src)],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        pytest.fail(f"wheel build failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    wheels = list(out.glob("aihydro_tools-*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def _tracked_data_files() -> list[str]:
    """Git-tracked non-Python files under ai_hydro/ (the data the code reads)."""
    try:
        res = subprocess.run(
            ["git", "ls-files", "ai_hydro"], cwd=REPO, capture_output=True,
            text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout; cannot enumerate tracked data files")
    return [f for f in res.stdout.splitlines() if f and not f.endswith(".py")]


def test_wheel_ships_every_tracked_data_file(built_wheel):
    with zipfile.ZipFile(built_wheel) as zf:
        names = set(zf.namelist())
    missing = [f for f in _tracked_data_files() if f not in names]
    assert not missing, f"tracked data files absent from the wheel: {missing}"


_PROBE = r"""
import json, sys
import ai_hydro
from ai_hydro.knowledge.loader import get_registry
from ai_hydro.workflows.loader import list_workflows
from ai_hydro.mcp import tools_knowledge as tk, tools_workflows as tw

reg = get_registry()
out = {
    "module_file": ai_hydro.__file__,
    "sizes": {
        "variables": len(reg.variables),
        "metrics": len(reg.metrics),
        "datasets": len(reg.datasets),
    },
    "workflows": [w["id"] for w in list_workflows()],
}
fn = lambda t: getattr(t, "fn", t)
metric = fn(tk.get_metric_definition)("metric.kge")
out["kge_error"] = metric.get("error") or metric.get("code")
out["kge_id"] = metric.get("id")
out["list_metrics"] = len(fn(tk.list_known_metrics)())
print("PROBE:" + json.dumps(out))
"""


def test_installed_wheel_registries_are_non_empty(built_wheel, tmp_path):
    target = tmp_path / "site"
    inst = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--target",
         str(target), str(built_wheel)],
        capture_output=True, text=True, timeout=600,
    )
    assert inst.returncode == 0, inst.stderr[-2000:]

    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(target),
        "HOME": str(tmp_path / "home"),
        "AIHYDRO_HOME": str(tmp_path / "home" / ".aihydro"),
    }
    # cwd away from the repo so the source tree cannot satisfy the import.
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    line = next(l for l in proc.stdout.splitlines() if l.startswith("PROBE:"))
    info = json.loads(line[len("PROBE:"):])

    assert str(target) in info["module_file"], "probe imported the source tree, not the wheel"
    # Equations are intentionally not asserted: no equations.yaml exists in the
    # project yet (see CHANGELOG / R-06 report); an empty equations registry is
    # a content gap, not a packaging loss.
    for name, n in info["sizes"].items():
        assert n > 0, f"installed {name} registry is empty: {info}"
    assert info["workflows"], f"installed workflow registry is empty: {info}"
    assert info["kge_id"], f"get_metric_definition(metric.kge) failed in the installed wheel: {info}"
    assert info["list_metrics"] > 0


def test_metric_lookup_accepts_an_id_without_the_prefix_and_equations_say_none_are_bundled():
    from ai_hydro.mcp import tools_knowledge as tk
    fn = lambda t: getattr(t, "fn", t)
    assert fn(tk.get_metric_definition)("kge")["id"] == "metric.kge"
    out = fn(tk.get_equation_definition)("equation.penman_monteith")
    assert "No equations are bundled" in out["message"]
