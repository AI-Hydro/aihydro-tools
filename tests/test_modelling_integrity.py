"""Modelling tool integrity (tool-audit F2, F5, F6, F7, F9, F12).

Each test reproduces the audit probe it is named for. Needs the optional aihydro-modelling
package (>= the version carrying ModelSpec extra="forbid" and per-backend defaults); skipped
where it is absent.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from ai_hydro.session import chat_binding, store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

from _modelling_marks import modelling_mark

needs_modelling = modelling_mark()


def _spec_has_forbid() -> bool:
    if importlib.util.find_spec("aihydro_modelling") is None:
        return False
    from aihydro_modelling import ModelSpec
    return ModelSpec.model_config.get("extra") == "forbid"


needs_new_modelling = modelling_mark(
    _spec_has_forbid,
    "needs aihydro-modelling >= 0.6.2 (the integrity fixes: extra=forbid, backend defaults)",
)

SID = "audit-job"


@pytest.fixture
def home(tmp_path, monkeypatch):
    import aihydro_core.jobs as J

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "home" / ".aihydro"))
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    monkeypatch.setattr(J, "_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(J, "_REGISTRY", tmp_path / "jobs" / "registry.json")
    return tmp_path


def _mkjob(home, jid, status, kind="training", session=SID, pid=999999, **extra):
    import aihydro_core.jobs as J

    d = home / "ws" / "runs" / jid
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps({"job_id": jid, "status": status, **extra}))
    if session is not None:
        (d / "job_config.json").write_text(json.dumps({"session_id": session}))
    J._registry_put(jid, {"pid": pid, "kind": kind, "runner_module": "x",
                          "artifact_dir": str(d), "status": "pending", "started_at": J._now()})


def _session_with_old_model():
    s = HydroSession(SID)
    s.model = {"nse": 0.91, "kge": 0.88, "framework": "hbv-light", "model_dir": "/old/run",
               "hbv_kernel": "hbv-conserving/1"}
    s.save()
    HydroSession("audit-other").save()


# ── F2 / F7: get_model_results(job_id=...) is that job's own result ──────────

def _gmr(**kw):
    from ai_hydro.mcp.tools_modelling import get_model_results
    return get_model_results(**kw)


def test_f2_unknown_job_never_reports_the_session_model(home):
    _session_with_old_model()
    r = _gmr(session_id=SID, job_id="doesnotexist")
    assert r["error"] and r["code"] == "JOB_NOT_FOUND" and r["model_trained"] is False
    assert "nse" not in r


def test_f2_failed_job_never_reports_the_session_model(home):
    _session_with_old_model()
    _mkjob(home, "job_failed", "failed", error={"code": "TRAINING_ERROR", "message": "boom"})
    r = _gmr(session_id=SID, job_id="job_failed")
    assert r["code"] == "JOB_FAILED" and r["model_trained"] is False and "nse" not in r
    assert r["job_error"]["message"] == "boom"


def test_f2_cancelled_job(home):
    _session_with_old_model()
    _mkjob(home, "job_canc", "cancelled")
    r = _gmr(session_id=SID, job_id="job_canc")
    assert r["code"] == "JOB_CANCELLED" and r["model_trained"] is False


def test_f2_autoresearch_job_is_not_a_training_job(home):
    _session_with_old_model()
    _mkjob(home, "job_auto", "complete", kind="autoresearch", incumbent_metrics={"nse": 0.4})
    r = _gmr(session_id=SID, job_id="job_auto")
    assert r["code"] == "NOT_A_TRAINING_JOB" and r["model_trained"] is False
    assert "get_leaderboard" in r["next_tools"] and "nse" not in r


def test_f2_job_from_another_session_is_refused(home):
    _session_with_old_model()
    _mkjob(home, "job_ok", "complete", partial_results={"nse": 0.66, "kge": 0.6})
    r = _gmr(session_id="audit-other", job_id="job_ok")
    assert r["code"] == "JOB_SESSION_MISMATCH" and r["model_trained"] is False
    assert r["job_session_id"] == SID and "nse" not in r


def test_f2_job_alone_needs_no_session_resolution(home):
    """Audit probe E2: job_id with no context failed SESSION_RESOLUTION_FAILED."""
    _session_with_old_model()
    _mkjob(home, "job_ok", "complete", partial_results={"nse": 0.66, "kge": 0.6})
    r = _gmr(job_id="job_ok")
    assert r["model_trained"] is True and r["nse"] == 0.66 and r["session_id"] == SID
    assert r["source"] == "job_artifact" and r["performance_rating"] == "satisfactory"


def test_f2_running_job_reports_not_trained(home):
    _session_with_old_model()
    _mkjob(home, "job_run", "running", progress={"restarts_done": 1})
    r = _gmr(session_id=SID, job_id="job_run")
    assert r["model_trained"] is False and r["job_status"] == "running" and "nse" not in r


def test_f7_complete_job_without_skill_is_not_trained(home):
    _session_with_old_model()
    _mkjob(home, "job_noskill", "complete", partial_results={"nse": None, "train_nse": 0.0})
    r = _gmr(session_id=SID, job_id="job_noskill")
    assert r["code"] == "JOB_NO_SKILL" and r["model_trained"] is False
    _mkjob(home, "job_empty", "complete")
    assert _gmr(session_id=SID, job_id="job_empty")["model_trained"] is False


def test_f7_session_model_without_nse_is_not_trained(home):
    s = HydroSession(SID)
    s.model = {"nse": None, "train_nse": 0.0, "framework": "hbv-light"}
    s.save()
    r = _gmr(session_id=SID)
    assert r["model_trained"] is False and r["code"] == "MODEL_NOT_SCORED"


def test_session_model_still_reported_without_job_id(home):
    _session_with_old_model()
    r = _gmr(session_id=SID)
    assert r["model_trained"] is True and r["nse"] == 0.91 and r["source"] == "session_cache"


# ── F5 / F6 / F12: one gate for every training request ───────────────────────

def _train_session(site="01031500"):
    s = HydroSession(SID)
    s.site_id = site
    s.set("watershed", {"data": {"area_km2": 769.0}, "meta": {"tool": "t"}})
    s.set("forcing", {"data": {}, "meta": {"tool": "t"}})
    s.set("streamflow", {"data": {}, "meta": {"tool": "t"}})
    s.save()


@pytest.fixture
def captured(home, monkeypatch):
    from ai_hydro.mcp import tools_modelling as tm

    cap: dict = {}

    def fake(**k):
        cap.update(k)
        return {"job_id": k["config"]["job_id"], "artifact_dir": str(k["artifact_dir"]),
                "log_path": "x", "started_at": "t"}

    monkeypatch.setattr(tm.jobs, "start_job", fake)
    return cap


@needs_new_modelling
def test_f5_typo_in_spec_json_is_refused_with_a_suggestion(captured):
    from ai_hydro.mcp.tools_modelling import propose_and_train
    _train_session()
    r = propose_and_train(spec_json='{"backend":"hbv","train_sart":"1990-01-01","n_epochs":3}',
                          session_id=SID)
    assert r["error"] and r["code"] == "INVALID_SPEC" and not captured
    assert set(r["unknown_keys"]) == {"train_sart", "n_epochs"}
    assert "train_start" in r["did_you_mean"]["train_sart"]


@needs_new_modelling
def test_f6_train_hydro_model_refuses_train_test_overlap(captured):
    """Audit probe p08 B: train 2000-2010 / test 2005-2010 was accepted."""
    from ai_hydro.mcp.tools_modelling import train_hydro_model
    _train_session()
    r = train_hydro_model(session_id=SID, train_start="2000-10-01", train_end="2010-09-30",
                          test_start="2005-10-01", test_end="2010-09-30")
    assert r["error"] and r["code"] == "VALIDATION_ERROR" and not captured
    assert any("overlap" in e for e in r["errors"])


@needs_new_modelling
def test_f6_junk_flat_arguments_are_refused(captured):
    from ai_hydro.mcp.tools_modelling import train_hydro_model
    _train_session()
    r = train_hydro_model(session_id=SID, framework="lstm", model="banana", hidden_size=-5, epochs=0)
    assert r["error"] and r["code"] == "INVALID_SPEC" and not captured
    r = train_hydro_model(session_id=SID, framework="lstm", hidden_size=-5, epochs=0)
    assert r["error"] and r["code"] == "INVALID_SPEC" and not captured
    r = train_hydro_model(session_id=SID, framework="ealstm-ish")
    assert r["error"] and r["code"] == "INVALID_SPEC" and not captured


@pytest.fixture
def all_backends(monkeypatch):
    """Report every backend available: these tests are about the spec, not the host's installs."""
    import types

    import aihydro_modelling as m

    ok = types.SimpleNamespace(available=True, reason="")
    monkeypatch.setattr(m, "availability", lambda: {b: ok for b in m.ModelSpec.model_fields["backend"].annotation.__args__})


@needs_new_modelling
def test_f12_flat_neural_defaults_are_the_backends_not_hbv(captured, all_backends):
    """Audit probe p08 C: framework=lstm ran at lr 0.05, epochs 500."""
    from ai_hydro.mcp.tools_modelling import train_hydro_model

    _train_session()
    r = train_hydro_model(session_id=SID, framework="lstm")
    assert not r.get("error"), r
    c = captured["config"]
    assert c["learning_rate"] == 0.001 and c["epochs"] == 30


@needs_new_modelling
def test_f12_flat_hbv_defaults_unchanged(captured):
    from ai_hydro.mcp.tools_modelling import train_hydro_model
    _train_session()
    r = train_hydro_model(session_id=SID)
    assert not r.get("error"), r
    c = captured["config"]
    assert c["learning_rate"] == 0.05 and c["epochs"] == 500 and c["n_restarts"] == 3
    assert c["test_start"] > c["val_end"] > c["val_start"] > c["train_end"]


@needs_new_modelling
def test_f12_propose_and_train_neural_default(captured, all_backends):
    from ai_hydro.mcp.tools_modelling import propose_and_train
    _train_session()
    r = propose_and_train(spec_json='{"backend":"nh_lstm"}', session_id=SID)
    assert not r.get("error"), r
    spec = captured["config"]["spec"]
    assert spec["learning_rate"] == 0.001 and spec["epochs"] == 30 and spec["seq_length"] == 365


# ── F7: the runner refuses a record that does not cover the windows ──────────

@needs_new_modelling
def test_f7_runner_fails_when_the_record_misses_the_windows(home, monkeypatch):
    from datetime import date, timedelta

    from aihydro_modelling import TrainingData
    from ai_hydro.modelling import runner

    _train_session()
    art = home / "art"
    art.mkdir()
    from aihydro_modelling import ModelSpec
    (art / "job_config.json").write_text(json.dumps(
        {"job_id": "j", "session_id": SID, "spec": ModelSpec(backend="hbv").model_dump()}))
    dates = [(date(2015, 1, 1) + timedelta(i)).isoformat() for i in range(2000)]
    z = [0.0] * 2000
    data = TrainingData(gauge_id="01031500", output_dir=art,
                        forcing={"dates": dates, "prcp_mm": z, "tmax_C": z, "tmin_C": z, "pet_mm": z},
                        q_by_date={d: 1.0 for d in dates})
    monkeypatch.setattr("ai_hydro.modelling.metrics.extract_basin_data", lambda *a, **k: (data, "x"))
    statuses = []
    monkeypatch.setattr(runner, "_write_status",
                        lambda d, j, status, **kw: statuses.append((status, kw)))
    with pytest.raises(SystemExit):
        runner.run(art)
    status, kw = statuses[-1]
    assert status == "failed" and kw["error"]["code"] == "INSUFFICIENT_DATA"
    assert "train window" in kw["error"]["message"]


@needs_new_modelling
def test_f7_runner_never_completes_an_unscored_model(home, monkeypatch):
    from aihydro_modelling import ModelSpec
    from ai_hydro.modelling import runner

    class R:                      # what a backend returns when nothing was scored
        data = {"nse": None, "train_nse": 0.0, "scored_period": None, "n_scored": 0}

    _train_session()
    art = home / "art"
    art.mkdir()
    (art / "job_config.json").write_text(json.dumps(
        {"job_id": "j", "session_id": SID, "spec": ModelSpec(backend="hbv").model_dump()}))
    monkeypatch.setattr("ai_hydro.modelling.metrics.extract_basin_data", lambda *a, **k: (object(), "x"))
    monkeypatch.setattr("ai_hydro.modelling.spec_gate.require_trainable", lambda *a, **k: None)
    import sys
    import aihydro_modelling.train  # noqa: F401  (ensure the submodule is imported)
    monkeypatch.setattr(sys.modules["aihydro_modelling.train"], "train", lambda spec, data: R())
    statuses = []
    monkeypatch.setattr(runner, "_write_status",
                        lambda d, j, status, **kw: statuses.append((status, kw)))
    with pytest.raises(SystemExit):
        runner.run(art)
    assert statuses[-1][0] == "failed" and statuses[-1][1]["error"]["code"] == "INSUFFICIENT_DATA"
    assert HydroSession.load(SID).model is None


# ── F9: slots from different gauges / units ──────────────────────────────────

def _mixed_session():
    s = HydroSession("audit-mix")
    s.site_id = "01031500"
    s.watershed = {"data": {"area_km2": 10000.0, "gauge_id": "99999999"}, "meta": {"gauge_id": "99999999"}}
    s.streamflow = {"data": {"dates": ["2000-01-01"], "q_cms": [100.0], "gauge_id": "01031500",
                             "units": "m3/s"}, "meta": {}}
    s.forcing = {"data": {"dates": ["2000-01-01"], "gauge_id": "12345678"}, "meta": {}}
    s.save()
    return HydroSession.load("audit-mix")


@needs_modelling
def test_f9_mixed_basin_slots_are_refused(home, monkeypatch, tmp_path):
    """Audit probe p08 A: watershed 99999999, forcing 12345678, site 01031500 -> no refusal."""
    import ai_hydro.modelling.metrics as MM
    from aihydro_core.primitives.errors import ToolError

    monkeypatch.setattr(MM, "fetch_camels_streamflow", lambda g, a: {})
    s = _mixed_session()
    with pytest.raises(ToolError) as e:
        MM.extract_basin_data(s, "01031500", tmp_path)
    assert e.value.code == "BASIN_MISMATCH"
    assert "99999999" in str(e.value) and "12345678" in str(e.value)


@needs_modelling
def test_f9_declared_non_cms_units_are_refused(home, monkeypatch, tmp_path):
    import ai_hydro.modelling.metrics as MM
    from aihydro_core.primitives.errors import ToolError

    monkeypatch.setattr(MM, "fetch_camels_streamflow", lambda g, a: {})
    s = _mixed_session()
    s.watershed["data"]["gauge_id"] = "01031500"
    s.forcing["data"]["gauge_id"] = "01031500"
    s.streamflow["data"]["units"] = "cfs"
    with pytest.raises(ToolError) as e:
        MM.extract_basin_data(s, "01031500", tmp_path)
    assert e.value.code == "UNIT_MISMATCH"


def test_f9_consistent_and_undeclared_slots_pass(home):
    from ai_hydro.modelling.gauge import basin_consistency_error

    s = _mixed_session()
    s.watershed["data"]["gauge_id"] = "USGS:01031500"
    s.forcing["data"].pop("gauge_id")          # older sessions do not declare a gauge
    assert basin_consistency_error(s, "01031500") is None


@needs_new_modelling
def test_f9_kickoff_refuses_a_mixed_basin_session(home, captured):
    from ai_hydro.mcp.tools_modelling import train_hydro_model

    _mixed_session()
    r = train_hydro_model(session_id="audit-mix")
    assert r["error"] and r["code"] == "BASIN_MISMATCH" and not captured


# ── Review items: stale kernel, too-old package, job edge cases, runner codes, gauge ids ──

def test_t3_unstamped_hbv_model_is_stale_not_clean(home):
    s = HydroSession(SID)
    s.model = {"nse": 0.91, "framework": "hbv-light", "model_type": "hbv"}   # pre-0.6.2: no stamp
    s.save()
    r = _gmr(session_id=SID)
    assert r["model_trained"] is False and r["stale"] is True and r["code"] == "MODEL_STALE_KERNEL"
    assert "nse" not in r and "performance_rating" not in r


def test_t3_unstamped_job_result_is_stale(home):
    _mkjob(home, "job_old", "complete", partial_results={"nse": 0.7, "framework": "hbv-light"})
    r = _gmr(job_id="job_old")
    assert r["model_trained"] is False and r["stale"] is True and "nse" not in r


@needs_new_modelling
def test_t3_current_kernel_stamp_is_clean(home):
    from aihydro_modelling.backends.hbv import HBV_KERNEL
    _mkjob(home, "job_new", "complete",
           partial_results={"nse": 0.7, "framework": "hbv-light", "hbv_kernel": HBV_KERNEL})
    assert _gmr(job_id="job_new")["model_trained"] is True


def test_t4_job_without_artifact_dir_is_not_found_and_never_reads_cwd(home, monkeypatch):
    from ai_hydro.mcp import tools_modelling as tm

    monkeypatch.setattr(tm.jobs, "get_job_status", lambda j: {"job_id": j, "status": "complete",
                        "partial_results": {"nse": 0.9}})
    monkeypatch.setattr(tm.jobs, "list_jobs", lambda *a, **k: {"jobs": []})
    (home / "job_config.json").write_text(json.dumps({"session_id": "elsewhere"}))
    monkeypatch.chdir(home)           # a job_config.json sits in the cwd; it must not be read
    r = _gmr(job_id="ghost")
    assert r["code"] == "JOB_NOT_FOUND" and r["model_trained"] is False


def test_t5_dead_process_job_is_not_still_running(home):
    _mkjob(home, "job_dead", "running", progress={"restarts_done": 1})
    r = _gmr(session_id=SID, job_id="job_dead")
    assert r["code"] == "JOB_PROCESS_GONE" and r["model_trained"] is False
    assert "still" not in r["message"]


@needs_modelling
def test_t1_old_modelling_is_refused_not_run(monkeypatch):
    import aihydro_modelling as m
    from ai_hydro.modelling.spec_gate import gate_spec

    monkeypatch.setattr(m, "__version__", "0.6.1")
    spec, bad = gate_spec({"backend": "hbv"})
    assert spec is None and bad["code"] == "MODELLING_TOO_OLD" and bad["installed"] == "0.6.1"


@needs_modelling
def test_t6_runner_keeps_coded_refusals_and_the_traceback_for_the_rest(home, monkeypatch):
    from ai_hydro.modelling import runner

    class Coded(Exception):          # has .code but is not a deliberate refusal
        code = "SOMETHING_ELSE"

    assert runner._refusal_code(Coded("x")) is None
    from aihydro_core.primitives.errors import ToolError
    assert runner._refusal_code(ToolError("INSUFFICIENT_DATA", "m")) == "INSUFFICIENT_DATA"

    _train_session()
    art = home / "art"
    art.mkdir()
    (art / "job_config.json").write_text(json.dumps({"job_id": "j", "session_id": SID, "framework": "hbv"}))
    monkeypatch.setattr("ai_hydro.modelling.metrics.extract_basin_data",
                        lambda *a, **k: (_ for _ in ()).throw(Coded("boom")))
    statuses = []
    monkeypatch.setattr(runner, "_write_status", lambda d, j, status, **kw: statuses.append((status, kw)))
    with pytest.raises(SystemExit):
        runner.run(art)
    err = statuses[-1][1]["error"]
    assert err["code"] == "TRAINING_ERROR" and "Coded" in err["message"]


def test_t7_gauge_id_formats_do_not_raise_false_mismatch(home):
    from ai_hydro.modelling.gauge import _norm_gauge, basin_consistency_error

    assert _norm_gauge("USGS-01031500") == _norm_gauge("usgs:1031500") == _norm_gauge("01031500")
    s = _mixed_session()
    s.watershed["data"]["gauge_id"] = "USGS-01031500"
    s.forcing["data"]["gauge_id"] = "1031500"
    assert basin_consistency_error(s, "01031500") is None
    s.forcing["data"]["gauge_id"] = "1031501"
    assert basin_consistency_error(s, "01031500")["code"] == "BASIN_MISMATCH"


def test_n1_legacy_job_found_by_path_fallback_is_not_reported_missing(home):
    """A job not in the registry (pre-registry runs under ~/.aihydro/models/runs) still resolves."""
    d = home / "home" / ".aihydro" / "models" / "runs" / "legacyjob"
    d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({
        "job_id": "legacyjob", "status": "complete", "log_path": str(d / "train.log"),
        "partial_results": {"nse": 0.62}}))
    (d / "job_config.json").write_text(json.dumps({"session_id": SID, "framework": "hbv"}))
    r = _gmr(job_id="legacyjob")
    assert r["model_trained"] is True and r["nse"] == 0.62 and r["session_id"] == SID
