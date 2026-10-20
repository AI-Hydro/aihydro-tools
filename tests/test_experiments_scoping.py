"""Review B1/C1/C2 regressions: basin-selecting params, the active feature, old modelling.

* B1  ``geometry_geojson`` in an experiment's shared params overrode every feature's polygon
      (``_scoped`` was None) so each row held the session basin's numbers under its own gauge's
      label and the experiment said ``complete``.
* C1  ``feature=None`` with an active registered feature filed the SESSION gauge's numbers
      under the active feature's id.
* C2  get_leaderboard on aihydro-modelling 0.6.0 presented the old structure as facts.

Same offline world as test_experiments_real_tools.py (recorded site records, labelled SYNTHETIC
daily values at the USGS transport seam).
"""
from __future__ import annotations

import importlib.util
import json
from unittest.mock import patch

import pytest

from ai_hydro.session.store import HydroSession
from test_experiments_real_tools import (  # noqa: F401  (fixture + helpers)
    AREA, GAUGE_AREA, SID, SQUARE, START, END, _register, _run, world,
)


needs_modelling = pytest.mark.skipif(
    importlib.util.find_spec("aihydro_modelling") is None,
    reason="needs the optional aihydro-modelling package (private; not installable in public CI)",
)


def _sigs(**kw):
    from ai_hydro.mcp.tools_analysis import extract_hydrological_signatures
    return extract_hydrological_signatures(session_id=SID, start_date=START, end_date=END,
                                           precipitation="skip", **kw)


# ── B1 ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reserved", [
    {"geometry_geojson": json.dumps(SQUARE)}, {"feature": "basin_a"}, {"session_id": "other"}])
def test_define_rejects_basin_selecting_params(world, reserved):
    from ai_hydro.mcp.tools_experiments import define_experiment
    r = define_experiment(session_id=SID, name="x", tool="extract_hydrological_signatures",
                          features=["basin_a", "basin_b"], params=reserved)
    assert r["error"] and next(iter(reserved)) in r["message"] and "register_feature" in r["message"]
    assert not world


def test_two_gauges_with_geometry_override_param_never_become_identical_rows(world):
    _register({"basin_a": "01013500", "basin_b": "03245500"})
    from ai_hydro.mcp.tools_experiments import define_experiment
    r = define_experiment(session_id=SID, name="x", tool="extract_hydrological_signatures",
                          features=["basin_a", "basin_b"],
                          params={"start_date": START, "end_date": END, "precipitation": "skip",
                                  "geometry_geojson": json.dumps(SQUARE)})
    assert r["error"], "an experiment with a basin-selecting param must not be definable"


def test_run_refuses_a_stored_older_definition_with_a_reserved_param(world):
    from ai_hydro.mcp.tools_experiments import define_experiment, run_experiment, _EXP_SLOT
    _register({"basin_a": "01013500", "basin_b": "03245500"})
    d = define_experiment(session_id=SID, name="x", tool="extract_hydrological_signatures",
                          features=["basin_a", "basin_b"], params={"start_date": START, "end_date": END})
    s = HydroSession.load(SID)                       # model a definition written before the rule
    exps = s.get(_EXP_SLOT)
    exps["data"][d["experiment_id"]]["defn"]["params"]["geometry_geojson"] = json.dumps(SQUARE)
    s.set(_EXP_SLOT, exps)
    s.save()
    r = run_experiment(session_id=SID, experiment_id=d["experiment_id"])
    assert r["error"] and "geometry_geojson" in r["message"]
    assert not world, "nothing may be acquired for a refused experiment"


# ── C1 ────────────────────────────────────────────────────────────────────────

def test_active_feature_is_scoped_like_an_explicit_one(world):
    from ai_hydro.mcp.tools_session import register_feature
    _register({"basin_a": "01013500"})
    register_feature(session_id=SID, geojson=json.dumps(SQUARE), name="basin_b", feature_id="basin_b",
                     gauge_id="03245500", area_km2=GAUGE_AREA["03245500"], set_active=True)
    assert HydroSession.load(SID).get_active_feature_id() == "basin_b"   # session gauge is 01013500

    implicit = _sigs()                         # feature=None -> the active feature
    assert not implicit.get("error"), implicit
    assert implicit["feature_id"] == "basin_b"
    assert implicit["data"]["basin_gauge_id"] == "03245500"           # not the session's 01013500
    assert {c[0] for c in world} == {"03245500"}

    explicit = _sigs(feature="basin_b")
    assert explicit["data"]["q_mean"] == implicit["data"]["q_mean"]
    assert explicit.get("_cache_hit") is True


def test_active_feature_without_a_gauge_is_refused_not_given_the_session_gauge(world):
    from ai_hydro.mcp.tools_session import register_feature
    register_feature(session_id=SID, geojson=json.dumps(SQUARE), name="annot", feature_id="annot",
                     set_active=True)
    r = _sigs()
    assert r["error"] and r["code"] == "FEATURE_GAUGE_UNKNOWN" and "annot" in r["message"]
    assert not world


def test_no_registered_feature_keeps_the_session_basin_semantics(world):
    r = _sigs(geometry_geojson=None)
    assert not r.get("error"), r
    assert r["feature_id"] == "__legacy__"
    assert {c[0] for c in world} == {"01013500"}       # the session's own gauge


def test_active_feature_geomorphic_uses_its_own_outlet(world, monkeypatch):
    from aihydro_core import HydroMeta, HydroResult
    from ai_hydro.mcp.tools_analysis import extract_geomorphic_parameters
    from ai_hydro.mcp.tools_session import register_feature
    seen = []

    def stand_in(watershed_geojson, outlet_lat, outlet_lon, dem_resolution=30):
        seen.append((outlet_lat, outlet_lon))          # STAND-IN for the DEM step
        return HydroResult(data={"DA_km2": 1.0}, meta=HydroMeta(tool="stand_in", version="0", params={}))

    monkeypatch.setattr("ai_hydro.analysis.geomorphic.extract_geomorphic_parameters_result", stand_in)
    s = HydroSession.load(SID)
    s.set("watershed", {"data": {"area_km2": 9.0, "geometry_geojson": SQUARE, "gauge_lat": 1.0,
                                 "gauge_lon": 2.0}, "meta": {"tool": "t"}})
    s.save()
    register_feature(session_id=SID, geojson=json.dumps(SQUARE), name="b", feature_id="b",
                     outlet_lat=39.5, outlet_lon=-77.0, set_active=True)
    r = extract_geomorphic_parameters(session_id=SID)
    assert r["feature_id"] == "b" and seen == [(39.5, -77.0)]     # not the session's (1.0, 2.0)


# ── C2 ────────────────────────────────────────────────────────────────────────

@needs_modelling
def test_get_leaderboard_on_old_modelling_gives_a_version_note_not_facts(tmp_path, monkeypatch):
    import aihydro_modelling.leaderboard as lb
    from ai_hydro.mcp import tools_modelling as tm
    art = tmp_path / "art"
    art.mkdir()
    (art / "leaderboard.json").write_text("[]")
    monkeypatch.setattr(tm.jobs, "get_job_status",
                        lambda job_id: {"status": "complete", "artifact_dir": str(art)})
    # the 0.6.0 shape: no promotions / n_trials, a grade
    monkeypatch.setattr(lb, "autoresearch_summary", lambda entries: {
        "n_experiments": 3, "n_ci_significant": 0, "has_prereg": False, "episode_grade": "F"})
    r = tm.get_leaderboard("j")
    assert "episode" not in r and "0.6.1" in r["version_note"]
    assert "episode_grade" not in json.dumps(r)


# ── non-blocking: units, no_run_id ────────────────────────────────────────────

def test_table_has_a_units_map(world):
    _register({"basin_a": "01013500"})
    _, _, t = _run(["basin_a"], metrics=["q_mean", "baseflow_index", "high_q_freq"])
    assert t["units"] == {"q_mean": "mm/day", "baseflow_index": "-", "high_q_freq": "days/year"}


def test_geomorphic_units_cover_every_metric():
    from ai_hydro.mcp.tools_experiments import _TOOL_OUTPUT_KEYS, _TOOL_UNITS
    for tool, keys in _TOOL_OUTPUT_KEYS.items():
        assert set(_TOOL_UNITS[tool]) == set(keys), tool


def test_no_run_id_rows_are_not_counted_as_successes(world):
    from ai_hydro.mcp.tools_experiments import run_experiment, define_experiment
    _register({"basin_a": "01013500"})
    d = define_experiment(session_id=SID, name="x", tool="extract_hydrological_signatures",
                          features=["basin_a"], metrics=["q_mean"])
    with patch("ai_hydro.mcp.tools_experiments._get_runners") as g:
        g.return_value = {"extract_hydrological_signatures":
                          lambda session_id, feature, **kw: {"data": {"q_mean": 1.0}}}   # no _run_id
        r = run_experiment(session_id=SID, experiment_id=d["experiment_id"])
    assert r["status"] == "partial" and r["n_success"] == 0 and r["n_no_run_id"] == 1
