"""The model step's lineage (DEMO-1 rehearsal finding 4; H005 packets P2 and P6).

* the training job loads the series the study actually retained (no fixed ``streamflow_<gauge>.json``),
  and refuses bytes whose sha256 differs from the one recorded at fetch time;
* the simulated series is referenced by path and sha256 in the result;
* the rows the job writes are sealed like any tool call's;
* ``data_nature`` (aihydro-data >= 0.2.10) is carried next to ``spatial_support`` and is empty, never
  inferred, on a release without it.

All offline. The USGS transport is replaced at the adapter's seam by SYNTHETIC daily values
(the same labelled stand-in as ``test_streamflow_retention_content_addressed``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import fastmcp
import pytest

from ai_hydro.data.nature import DATA_NATURE_CLASSES, clean_nature, nature_of, product_nature
from ai_hydro.modelling import simulation_artifact as sim
from ai_hydro.modelling.retained_series import load_slot_data
from ai_hydro.session import chat_binding, store
from ai_hydro.session import run_records as rr
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

GAUGE = "01013500"
needs_fastmcp2 = pytest.mark.skipif(
    int(fastmcp.__version__.split(".")[0]) >= 3,
    reason="real ai_hydro tools need the pinned FastMCP 2.x API",
)


# ── 1. the job loads the retained series ────────────────────────────────────────────────────────

@pytest.fixture
def world(tmp_path, monkeypatch):
    """Isolated session store plus the real ``fetch_streamflow_data`` over a replayed USGS transport."""
    from _usgs_replay import Replay, ok
    from test_streamflow_retention_content_addressed import _daily_body, call
    from ai_hydro.mcp import app

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "home" / ".aihydro"))
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))

    def override(url, params):
        if url.endswith("/collections/daily/items"):
            start, end = params["time"].split("/")
            return ok(_daily_body(start, end))
        return None

    from aihydro_data.sources import usgs_waterdata as uw
    monkeypatch.setattr(uw, "_http_get", Replay(override))

    def new_session(sid, workspace=None):
        s = HydroSession(sid)
        s.site_id, s.site_type = GAUGE, "usgs_gauge"
        if workspace is not None:
            s.workspace_dir = str(workspace)
        s.set("watershed", {"data": {"area_km2": 250.0, "gauge_id": GAUGE},
                            "meta": {"tool": "delineate_watershed"}})
        s.save()

    def fetch(sid, start="2000-01-01", end="2000-12-31"):
        err, body = call(app.mcp, "fetch_streamflow_data",
                         {"session_id": sid, "gauge_id": GAUGE, "start_date": start, "end_date": end})
        assert not err and not body.get("error"), body
        return body

    return new_session, fetch


@needs_fastmcp2
def test_job_loads_the_fetched_series_when_the_session_has_no_workspace(world, tmp_path):
    """Reproducer of finding 4b, session A: no workspace_dir, so no ``streamflow_<gauge>.json`` exists."""
    new_session, fetch = world
    new_session("study-a")
    fetch("study-a")
    session = HydroSession.load("study-a")

    assert session.workspace_dir is None
    data = load_slot_data(session, "streamflow", GAUGE)

    assert len(data["dates"]) == len(data["q_cms"]) == 366
    assert not list(tmp_path.rglob(f"streamflow_{GAUGE}.json")), "the old fixed name must not be needed"


@needs_fastmcp2
def test_job_loads_the_content_addressed_file_in_a_workspace(world, tmp_path):
    """Reproducer of finding 4b, session B: the file is ``streamflow_<gauge>_<hash>.json``."""
    new_session, fetch = world
    ws = tmp_path / "workspace"
    ws.mkdir()
    new_session("study-b", ws)
    fetch("study-b")
    session = HydroSession.load("study-b")

    (retained,) = ws.glob(f"streamflow_{GAUGE}_*.json")
    assert not (ws / f"streamflow_{GAUGE}.json").exists()
    data = load_slot_data(session, "streamflow", GAUGE)
    assert data["dates"] == json.loads(retained.read_text())["dates"]


@needs_fastmcp2
def test_job_refuses_a_retained_file_whose_sha256_changed(world, tmp_path):
    new_session, fetch = world
    ws = tmp_path / "workspace"
    ws.mkdir()
    new_session("study-b", ws)
    fetch("study-b")
    (retained,) = ws.glob(f"streamflow_{GAUGE}_*.json")
    body = json.loads(retained.read_text())
    body["q_cms"][0] += 1.0                      # one value edited after the fetch
    retained.write_text(json.dumps(body))

    with pytest.raises(ValueError, match="sha256"):
        load_slot_data(HydroSession.load("study-b"), "streamflow", GAUGE)


@needs_fastmcp2
def test_job_does_not_swap_in_a_same_named_legacy_file_when_the_digest_fails(world, tmp_path):
    """A digest mismatch is final: the legacy fixed name is never a way around it."""
    new_session, fetch = world
    ws = tmp_path / "workspace"
    ws.mkdir()
    new_session("study-b", ws)
    fetch("study-b")
    (retained,) = ws.glob(f"streamflow_{GAUGE}_*.json")
    good = retained.read_bytes()
    retained.write_bytes(good.replace(b'"q_cms"', b'"q_cms"', 1) + b" ")
    (ws / f"streamflow_{GAUGE}.json").write_bytes(good)

    with pytest.raises(ValueError, match="sha256"):
        load_slot_data(HydroSession.load("study-b"), "streamflow", GAUGE)


def _slot(path: Path | None, digest: str | None = None, **data):
    d = {"data": dict(data), "meta": {}}
    if path is not None:
        d["data"]["_data_file"] = str(path)
    if digest is not None:
        d["meta"]["retained_series"] = {"path": str(path), "digest": digest}
    return d


def test_missing_retained_file_is_a_refusal_not_a_silent_fallback(tmp_path):
    sess = SimpleNamespace(streamflow=_slot(tmp_path / "gone.json", "sha256:" + "0" * 64),
                           workspace_dir=None, session_id="s")
    with pytest.raises(ValueError, match="not on disk"):
        load_slot_data(sess, "streamflow", GAUGE)


def test_no_slot_is_the_existing_error(tmp_path):
    with pytest.raises(ValueError, match="No streamflow cached"):
        load_slot_data(SimpleNamespace(streamflow=None, workspace_dir=None, session_id="s"),
                       "streamflow", GAUGE)


def test_slot_without_a_pointer_still_reads_the_legacy_workspace_file(tmp_path):
    """Forcing (and sessions older than the pointer) keep the fixed name as a last resort."""
    (tmp_path / f"forcing_{GAUGE}.json").write_text(json.dumps({"dates": ["2000-01-01"], "prcp_mm": [1.0]}))
    sess = SimpleNamespace(forcing={"data": {"product": "GRIDMET"}, "meta": {}},
                           workspace_dir=str(tmp_path), session_id="s")
    assert load_slot_data(sess, "forcing", GAUGE)["dates"] == ["2000-01-01"]


def test_forcing_follows_its_own_data_file_pointer(tmp_path):
    f = tmp_path / "s_forcing.json"
    f.write_text(json.dumps({"dates": ["2000-01-01", "2000-01-02"], "prcp_mm": [1.0, 2.0]}))
    sess = SimpleNamespace(forcing=_slot(f, product="GRIDMET"), workspace_dir=None, session_id="s")
    assert len(load_slot_data(sess, "forcing", GAUGE)["dates"]) == 2


def test_the_file_the_job_read_enters_the_call_lineage_with_its_digest(tmp_path):
    from aihydro_core.records import digest_bytes

    f = tmp_path / f"streamflow_{GAUGE}_abcdef123456.json"
    f.write_text(json.dumps({"dates": ["2000-01-01"], "q_cms": [1.0]}))
    dig = digest_bytes(f.read_bytes())
    sess = SimpleNamespace(streamflow=_slot(f, dig), workspace_dir=None, session_id="s")

    capture, token = rr.begin_capture(tool="propose_and_train")
    try:
        load_slot_data(sess, "streamflow", GAUGE)
    finally:
        rr.end_capture(token)
    assert [r["digest"] for r in capture.input_refs] == [dig]
    assert capture.input_refs[0]["role"] == "served_data"


def test_metrics_load_full_data_delegates_to_the_slot_loader(tmp_path):
    pytest.importorskip("aihydro_modelling")
    from ai_hydro.modelling import metrics

    f = tmp_path / "x.json"
    f.write_text(json.dumps({"dates": ["2000-01-01"], "q_cms": [2.0]}))
    sess = SimpleNamespace(streamflow=_slot(f), workspace_dir=None, session_id="s")
    assert metrics._load_full_data(sess, "streamflow", GAUGE)["q_cms"] == [2.0]


# ── 2. the simulated series is referenced by path and sha256 ───────────────────────────────────

def _write_sim(path: Path, text="date,sim\n2000-01-01,1.0\n") -> str:
    path.write_text(text)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_modelling_070_series_is_referenced_and_redigested(tmp_path):
    csv_path = tmp_path / "simulation.csv"
    sha = _write_sim(csv_path)
    ref = sim.simulation_record({"simulation_csv": str(csv_path), "simulation_sha256": sha,
                                 "simulation_rows": 1, "simulation_columns": ["date", "sim"],
                                 "aihydro_modelling_version": "0.7.0"}, tmp_path)
    assert ref["status"] == "stored" and ref["sha256"] == sha and ref["sha256_verified"] is True
    assert ref["path"] == str(csv_path) and ref["scope"] == "full_run" and ref["producer_version"] == "0.7.0"


def test_modelling_070_series_with_a_wrong_digest_is_flagged_not_trusted(tmp_path):
    csv_path = tmp_path / "simulation.csv"
    _write_sim(csv_path)
    ref = sim.simulation_record({"simulation_csv": str(csv_path), "simulation_sha256": "0" * 64}, tmp_path)
    assert ref["sha256_verified"] is False and ref["sha256_on_disk"] != "0" * 64


def test_modelling_070_series_file_gone_is_stated(tmp_path):
    ref = sim.simulation_record({"simulation_csv": str(tmp_path / "gone.csv"),
                                 "simulation_sha256": "a" * 64}, tmp_path)
    assert ref["status"] == "file_missing" and ref["sha256_verified"] is False


def test_modelling_06x_scored_pairs_are_written_and_labelled(tmp_path):
    model_dir = tmp_path / "model"
    ref = sim.simulation_record({"model_dir": str(model_dir), "scored_period": ["2007-10-01", "2007-10-03"],
                                 "aihydro_modelling_version": "0.6.2"}, tmp_path,
                                eval_obs=[1.0, 2.0, 3.0], eval_pred=[1.5, float("nan"), 2.5])
    f = Path(ref["path"])
    assert f == model_dir / sim.SCORED_TEST_FILE and f.is_file()
    assert hashlib.sha256(f.read_bytes()).hexdigest() == ref["sha256"]
    assert f.read_text().splitlines() == ["k,obs_mm_day,sim_mm_day", "0,1.0,1.5", "1,2.0,", "2,3.0,2.5"]
    assert ref["scope"] == "scored_test_days_only" and ref["rows"] == 3
    assert "dates" in ref and ref["dates"].startswith("not recorded")
    assert ref["producer"] == "aihydro_tools"


def test_modelling_06x_spec_path_has_no_series_and_says_so(tmp_path):
    ref = sim.simulation_record({"aihydro_modelling_version": "0.6.2"}, tmp_path)
    assert ref["status"] == "not_available" and "0.7.0" in ref["reason"]
    assert "path" not in ref and "sha256" not in ref        # nothing is invented


def test_mismatched_pair_lengths_are_not_written(tmp_path):
    ref = sim.simulation_record({}, tmp_path, eval_obs=[1.0, 2.0], eval_pred=[1.0])
    assert ref["status"] == "not_available" and not list(tmp_path.glob("*.csv"))


def _spec_job(tmp_path, monkeypatch, train_result):
    """Run the spec path of the real runner with the modelling calls stubbed (sealing is exercised for real)."""
    import sys

    pytest.importorskip("aihydro_modelling")
    import aihydro_modelling.train  # noqa: F401
    import ai_hydro.modelling.metrics as metrics_mod
    import ai_hydro.modelling.spec_gate as gate
    from aihydro_modelling.spec import ModelSpec
    from ai_hydro.modelling import runner

    monkeypatch.setattr(metrics_mod, "extract_basin_data", lambda *a, **k: (SimpleNamespace(), "X"))
    monkeypatch.setattr(gate, "require_trainable", lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["aihydro_modelling.train"], "train",
                        lambda spec, data: SimpleNamespace(data=dict(train_result)))
    art = tmp_path / "job"
    art.mkdir()
    (art / "job_config.json").write_text(json.dumps(
        {"job_id": "j1", "session_id": "job-sess", "run_id": "j1",
         "spec": ModelSpec(backend="hbv").model_dump()}))
    runner.run(art)
    return json.loads((art / "status.json").read_text())


@pytest.fixture
def job_world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "home" / ".aihydro"))
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()
    s = HydroSession("job-sess")
    s.site_id, s.site_type = GAUGE, "usgs_gauge"
    s.save()
    return tmp_path


def test_job_result_carries_the_series_reference(job_world, monkeypatch):
    csv_path = job_world / "simulation.csv"
    sha = _write_sim(csv_path)
    status = _spec_job(job_world, monkeypatch, {"nse": 0.7, "simulation_csv": str(csv_path),
                                                "simulation_sha256": sha})
    ref = status["partial_results"]["simulation_series"]
    assert status["status"] == "complete" and ref["sha256"] == sha and ref["status"] == "stored"


def test_job_result_without_a_series_says_not_available(job_world, monkeypatch):
    status = _spec_job(job_world, monkeypatch, {"nse": 0.7})
    assert status["partial_results"]["simulation_series"]["status"] == "not_available"


# ── 3. the job's own rows are sealed (P6) ────────────────────────────────────────────────────────

def test_job_rows_are_sealed_and_tamper_evident(job_world, monkeypatch):
    csv_path = job_world / "simulation.csv"
    sha = _write_sim(csv_path)
    status = _spec_job(job_world, monkeypatch, {"nse": 0.7, "simulation_csv": str(csv_path),
                                                "simulation_sha256": sha})
    result = status["partial_results"]
    log = HydroSession.load("job-sess").get("_run_log")

    assert log, "the job wrote no run-log rows"
    rows = {rid: row for rid, row in log.items()}
    assert any(r.get("slot") == "model" for r in rows.values()), "no model slot row was written"
    for rid, row in rows.items():
        check = rr.verify_run_log_entry(row)
        assert check["has_record"] and check["record_ok"] and check["entry_ok"] is True, (rid, check)
        rec = row["record"]
        assert rec["input_digest"].startswith("sha256:") and rec["output_digest"].startswith("sha256:")
        assert rec["tool"] == "propose_and_train"
        assert rec["extra"]["executed_by"] == "background_job"
        assert rec["extra"]["simulation_sha256"] == sha
    assert rr.coverage_summary(log)["legacy_unrecorded"] == 0

    # The output digest is over the result the job reports (without transport keys).
    from aihydro_core.records import digest
    expected = digest(rr.output_payload(result))
    assert expected in {r["record"]["output_digest"] for r in rows.values()}

    # Flipping one stored field fails the seal.
    model_row = next(r for r in rows.values() if r.get("slot") == "model")
    forged = json.loads(json.dumps(model_row))
    forged["key_outputs"] = {**(forged.get("key_outputs") or {}), "nse": 0.99}
    assert rr.verify_run_log_entry(forged)["entry_ok"] is False


def test_job_seal_is_best_effort(job_world):
    """No session, no capture: sealing returns quietly and never raises."""
    from ai_hydro.modelling.job_seal import JobSeal

    seal = JobSeal("propose_and_train", "no-such-session", {"session_id": "no-such-session"})
    assert seal.seal({"nse": 0.1}).label == "no_session"
    seal.close()


# ── 4. data_nature (P2) ──────────────────────────────────────────────────────────────────────────

def test_six_classes_only():
    assert DATA_NATURE_CLASSES == ("gauge_observed", "gridded_obs_derived", "reanalysis",
                                   "modelled", "satellite_derived", "mapped")
    assert clean_nature("modelled") == "modelled"
    for bad in ("observed", "Modelled", "", None, 3, ["modelled"]):
        assert clean_nature(bad) == ""


def test_nature_is_carried_as_recorded_and_empty_on_a_release_without_it():
    assert nature_of(SimpleNamespace(data_nature="gauge_observed")) == "gauge_observed"
    assert nature_of({"data_nature": "reanalysis"}) == "reanalysis"
    assert nature_of(SimpleNamespace(spatial_support="point")) == ""        # aihydro-data 0.2.9 FetchResult
    assert nature_of({"spatial_support": "point"}) == ""
    assert nature_of(None) == ""


def test_product_nature_is_the_registry_label_or_empty():
    value = product_nature("NWIS_STREAMFLOW")
    assert value in ("", "gauge_observed")           # "" on aihydro-data < 0.2.10
    assert product_nature("NO_SUCH_PRODUCT") == ""


def test_product_nature_reads_the_registry_when_the_field_exists(monkeypatch):
    import aihydro_data

    monkeypatch.setattr(aihydro_data, "get_product", lambda pid: SimpleNamespace(data_nature="modelled"))
    assert product_nature("GEOGLOWS") == "modelled"
    monkeypatch.setattr(aihydro_data, "get_product", lambda pid: SimpleNamespace(data_nature="made-up"))
    assert product_nature("GEOGLOWS") == ""


def _fetched(nature=None):
    import pandas as pd

    kw = {"data_nature": nature} if nature is not None else {}
    return SimpleNamespace(
        data=pd.DataFrame({"date": pd.date_range("2000-01-01", periods=3), "streamflow": [1.0, 2.0, 3.0]}),
        variable="streamflow", product="GEOGLOWS", source="s3", units="m3/s", timestep="daily",
        spatial_support="reach", aggregation_actual="", cache_key="ck", **kw)


@pytest.mark.parametrize("nature,expected", [("modelled", "modelled"), (None, "")])
def test_retained_series_payload_carries_nature_beside_spatial_support(nature, expected):
    from ai_hydro.mcp.tools_data_fetch import _series_payload

    payload = _series_payload(_fetched(nature), "streamflow")
    assert payload["spatial_support"] == "reach"
    assert payload["data_nature"] == expected


def test_run_series_meta_echoes_data_nature():
    from ai_hydro.session import series

    assert "data_nature" in series._META_KEYS


def _trail(result, nature):
    result = {**result, **({"data_nature": nature} if nature is not None else {})}
    return rr.attempt_trail(result, None, None, tool="data_fetch")


_HISTORY = [{"product": "GEOGLOWS", "outcome": "served", "reason": "", "source": "s3"}]


def test_served_attempt_in_the_sealed_trail_records_nature():
    t = _trail({"product": "GEOGLOWS", "fallback_history": _HISTORY, "spatial_support": "reach"}, "modelled")
    (a,) = t["attempts"]
    assert a["data_nature"] == "modelled"


def test_trail_is_unchanged_when_the_data_layer_records_no_nature():
    """aihydro-data 0.2.9: no key, so the trail and the record digest are the ones earlier versions produced."""
    t = _trail({"product": "GEOGLOWS", "fallback_history": _HISTORY}, None)
    assert all("data_nature" not in a for a in t["attempts"])
    t2 = _trail({"product": "GEOGLOWS", "fallback_history": _HISTORY}, "not-a-class")
    assert all("data_nature" not in a for a in t2["attempts"])


def test_only_the_served_product_gets_the_label():
    history = [{"product": "A", "outcome": "failed", "reason": "x"},
               {"product": "B", "outcome": "served", "reason": ""}]
    t = _trail({"product": "B", "fallback_history": history}, "reanalysis")
    by = {a["source"]: a for a in t["attempts"]}
    assert by["B"]["data_nature"] == "reanalysis" and "data_nature" not in by["A"]


def test_flipping_the_nature_fails_the_seal():
    t = _trail({"product": "GEOGLOWS", "fallback_history": _HISTORY}, "modelled")
    rec = rr.build_run_record(run_id="r", tool="data_fetch", session_id="s", arguments={},
                              result={"data": {"x": 1}}, extra={"attempt_trail": t})
    assert rec.verify()
    forged = rec.to_dict()
    forged["extra"]["attempt_trail"]["attempts"][0]["data_nature"] = "gauge_observed"
    from aihydro_core.records import RunRecord
    assert RunRecord.from_dict(forged).verify() is False


def test_streamflow_slot_from_a_data_fetch_result_carries_nature():
    import pandas as pd
    from ai_hydro.mcp.streamflow_slot import slot_from_fetch_result

    df = pd.DataFrame({"date": pd.date_range("2000-01-01", periods=3), "streamflow": [1.0, 2.0, 3.0],
                       "approval_code": ["A"] * 3, "qualifier_code": [""] * 3})
    fetched = SimpleNamespace(data=df)
    base = {"product": "NWIS_STREAMFLOW", "source": "direct_api", "site_resolution": {"site_name": "x"}}
    with_nature = slot_from_fetch_result({**base, "data_nature": "gauge_observed"}, fetched,
                                         gauge_id=GAUGE, start="2000-01-01", end="2000-01-03")
    without = slot_from_fetch_result(base, fetched, gauge_id=GAUGE, start="2000-01-01", end="2000-01-03")
    assert with_nature["data"]["data_nature"] == "gauge_observed"
    assert without["data"]["data_nature"] == ""


@needs_fastmcp2
def test_fetch_streamflow_data_output_carries_the_registry_label_or_empty(world):
    new_session, fetch = world
    new_session("study-a")
    body = fetch("study-a")
    assert body["data"]["data_nature"] in ("", "gauge_observed")


@needs_fastmcp2
def test_fetch_streamflow_data_label_when_the_registry_declares_one(world, monkeypatch):
    import aihydro_data

    new_session, fetch = world
    monkeypatch.setattr(aihydro_data, "get_product", lambda pid: SimpleNamespace(data_nature="gauge_observed"))
    new_session("study-a")
    assert fetch("study-a")["data"]["data_nature"] == "gauge_observed"


def test_legacy_path_rows_are_sealed_under_the_tool_it_was_launched_as(job_world, monkeypatch):
    import sys
    import types

    pytest.importorskip("aihydro_modelling")
    from ai_hydro.modelling import runner

    csv_path = job_world / "simulation.csv"
    sha = _write_sim(csv_path)
    # A stand-in module, so the real hbv module is never imported here (its import binds
    # extract_basin_data, which other tests patch before their first import of it).
    fake = types.ModuleType("ai_hydro.modelling.conceptual.hbv")
    fake.train_hbv_light = lambda **k: {
        "nse": 0.6, "simulation_series": sim.simulation_record(
            {"simulation_csv": str(csv_path), "simulation_sha256": sha}, k["output_dir"])}
    monkeypatch.setitem(sys.modules, "ai_hydro.modelling.conceptual.hbv", fake)
    art = job_world / "job"
    art.mkdir()
    (art / "job_config.json").write_text(json.dumps(
        {"job_id": "j2", "session_id": "job-sess", "framework": "hbv",
         "train_start": "2000-10-01", "train_end": "2003-09-30", "val_start": "2003-10-01",
         "val_end": "2005-09-30", "test_start": "2005-10-01", "test_end": "2007-09-30"}))
    runner.run(art)

    assert json.loads((art / "status.json").read_text())["status"] == "complete"
    log = HydroSession.load("job-sess").get("_run_log")
    (row,) = [r for r in log.values() if r.get("slot") == "model"]
    check = rr.verify_run_log_entry(row)
    assert check["record_ok"] and check["entry_ok"] is True
    assert row["record"]["tool"] == "train_hydro_model"
    assert row["record"]["extra"]["simulation_sha256"] == sha
