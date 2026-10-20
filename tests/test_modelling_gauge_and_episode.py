"""Modelling jobs never use the session id as a gauge; get_leaderboard serves facts, not a grade.

* search_runner read ``getattr(session, "usgs_gauge_id", None) or session_id`` (the attribute
  does not exist) and modelling/runner used ``session.site_id or session_id``: a session
  called "kings-study" was looked up as a gauge. Both now resolve ``site_id`` and an empty one
  is ``GAUGE_NOT_SET``; the three job kick-offs refuse before anything is spawned.
* aihydro-modelling 0.6.1 returns ``facts`` and no score/grade; get_leaderboard exposes them.
"""
from __future__ import annotations

import importlib.util
import json

import pytest

from ai_hydro.session import chat_binding, store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

SID = "kings-study"


needs_modelling = pytest.mark.skipif(
    importlib.util.find_spec("aihydro_modelling") is None,
    reason="needs the optional aihydro-modelling package (private; not installable in public CI)",
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "home" / ".aihydro"))
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    return tmp_path


def _session(site_id: str) -> HydroSession:
    s = HydroSession(SID)
    s.site_id = site_id
    s.set("watershed", {"data": {"area_km2": 10.0}, "meta": {"tool": "t"}})
    s.set("forcing", {"data": {}, "meta": {"tool": "t"}})
    s.set("streamflow", {"data": {}, "meta": {"tool": "t"}})
    s.save()
    return s


# ── gauge resolution ─────────────────────────────────────────────────────────

def test_gauge_error_helper(home):
    from ai_hydro.modelling.gauge import gauge_error, require_gauge
    from aihydro_core.primitives.errors import ToolError
    assert gauge_error(_session(""), SID)["code"] == "GAUGE_NOT_SET"
    assert gauge_error(_session("   "), SID)["code"] == "GAUGE_NOT_SET"
    assert gauge_error(_session("01013500"), SID) is None
    assert require_gauge(_session(" 01013500 "), SID) == "01013500"
    with pytest.raises(ToolError) as e:
        require_gauge(_session(""), SID)
    assert e.value.code == "GAUGE_NOT_SET" and SID in str(e.value)


@needs_modelling
def test_search_runner_resolves_site_id_not_the_session_id(home, tmp_path, monkeypatch):
    from ai_hydro.mcp import search_runner

    class Stop(Exception):
        pass

    seen = {}

    def fake_extract(session, gauge, artifact_dir):
        seen["gauge"] = gauge
        raise Stop

    monkeypatch.setattr("ai_hydro.modelling.metrics.extract_basin_data", fake_extract)
    _session("01013500")
    with pytest.raises(Stop):
        search_runner._run_inner({"session_id": SID}, tmp_path / "art", "job1")
    assert seen["gauge"] == "01013500"


@needs_modelling
def test_search_runner_with_no_site_id_fails_closed_before_any_lookup(home, tmp_path, monkeypatch):
    from aihydro_core.primitives.errors import ToolError
    from ai_hydro.mcp import search_runner

    called = []
    monkeypatch.setattr("ai_hydro.modelling.metrics.extract_basin_data",
                        lambda *a, **k: called.append(a))
    _session("")
    with pytest.raises(ToolError) as e:
        search_runner._run_inner({"session_id": SID}, tmp_path / "art", "job1")
    assert e.value.code == "GAUGE_NOT_SET" and not called


def test_training_runner_with_no_site_id_writes_a_failed_status(home, tmp_path, monkeypatch):
    from ai_hydro.modelling import runner

    _session("")
    art = tmp_path / "art"
    art.mkdir()
    (art / "job_config.json").write_text(json.dumps({"job_id": "j", "session_id": SID}))
    statuses = []
    monkeypatch.setattr(runner, "_write_status",
                        lambda d, j, status, **kw: statuses.append((status, kw)))
    try:
        runner.run(art)
    except SystemExit:
        pass
    assert statuses[-1][0] == "failed"
    assert "GAUGE_NOT_SET" in json.dumps(statuses[-1][1], default=str)


@pytest.mark.parametrize("tool,args", [
    ("train_hydro_model", {}),
    pytest.param("run_autoresearch", {"hypothesis": "h", "max_experiments": 2}, marks=needs_modelling),
])
def test_kickoffs_refuse_without_a_gauge_and_start_no_job(home, monkeypatch, tool, args):
    from ai_hydro.mcp import tools_modelling as tm
    monkeypatch.setattr(tm.jobs, "start_job",
                        lambda **k: pytest.fail("a job was started without a gauge"))
    _session("")
    r = getattr(tm, tool)(session_id=SID, **args)
    assert r["error"] and r["code"] == "GAUGE_NOT_SET"


# ── get_leaderboard serves the episode facts ──────────────────────────────────

def _loop_entries():
    """Entries shaped as ``search.loop.run_loop`` writes them (comparison nested; seed in spec).
    Hand-built stand-in: the point is that get_leaderboard passes the package's facts through."""
    def e(i, wins, lo, p, inc):
        return {"run_index": i, "status": "complete", "is_incumbent": inc,
                "metrics": {"nse": 0.5 + 0.01 * i}, "spec": {"seed": 3}, "prereg_id": "pre-001",
                "comparison": {"challenger_wins": wins, "ci_lower": lo, "ci_upper": lo + 0.1,
                               "p_value": p, "confidence": 0.95, "status": "complete"} if i else None}
    return [e(0, None, 0, None, True), e(1, True, 0.02, 0.001, True), e(2, False, -0.03, 0.4, False)]


@needs_modelling
def test_get_leaderboard_returns_facts_and_no_grade(home, tmp_path, monkeypatch):
    from ai_hydro.mcp import tools_modelling as tm
    from aihydro_modelling.leaderboard import autoresearch_summary
    if "n_trials" not in autoresearch_summary([]):
        pytest.skip("needs aihydro-modelling >= 0.6.1 (facts dict)")
    art = tmp_path / "art"
    art.mkdir()
    (art / "leaderboard.json").write_text(json.dumps(_loop_entries()))
    monkeypatch.setattr(tm.jobs, "get_job_status",
                        lambda job_id: {"status": "complete", "artifact_dir": str(art)})
    r = tm.get_leaderboard("job1")
    assert not r.get("error"), r
    ep = r["episode"]
    assert ep["n_trials"] == 3 and ep["n_promotions"] == 1
    p = ep["promotions"][0]
    assert p["ci_excludes_zero"] is True and "p_adjusted_holm" in p
    assert "significant_after_multiplicity" in p
    assert ep["seeds_recorded"] is True and ep["has_prereg"] is True and ep["prereg_id"] == "pre-001"
    for k in ("episode_score", "episode_grade", "score_breakdown", "deprecation"):
        assert k not in ep
    assert "grade" not in json.dumps(r).lower().replace("upgrade", "")


def test_missing_modelling_package_is_a_structured_setup_error(home, monkeypatch):
    """Without aihydro-modelling the kickoff tools say so, not UNEXPECTED_ERROR."""
    import builtins

    from ai_hydro.mcp import tools_modelling as tm

    real_import = builtins.__import__

    def no_modelling(name, *a, **k):
        if name.split(".")[0] == "aihydro_modelling":
            raise ModuleNotFoundError("No module named 'aihydro_modelling'", name="aihydro_modelling")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_modelling)
    monkeypatch.setattr(tm.jobs, "start_job", lambda **k: pytest.fail("a job was started"))
    _session("01013500")
    for r in (tm.run_autoresearch(session_id=SID, hypothesis="h", max_experiments=2),
              tm.propose_and_train(session_id=SID, spec_json="{}")):
        assert r["error"] and r["code"] == "MODELLING_NOT_INSTALLED"
        assert "aihydro-tools[modelling]" in r["recovery"]
