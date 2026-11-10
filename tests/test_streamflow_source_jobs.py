"""`streamflow_source` travels kickoff -> job config -> runner / search_runner -> extract_basin_data,
and lands in the result and the run-log inputs (P5, G1/G2)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("aihydro_modelling")
pytest.importorskip("aihydro_core")


@pytest.fixture
def stub_session(monkeypatch):
    from ai_hydro import session as session_pkg
    sess = SimpleNamespace(workspace_dir=None, site_id="01013500", model=None,
                           add_citations=lambda *a, **k: None, save=lambda: None)
    monkeypatch.setattr(session_pkg.HydroSession, "load", staticmethod(lambda sid: sess))
    return sess


@pytest.mark.parametrize("source", ["session", "camels"])
def test_runner_spec_path_passes_source_and_records_it(stub_session, monkeypatch, tmp_path, source):
    import sys
    import aihydro_modelling.train  # noqa: F401  (ensures the submodule is loaded)
    train_mod = sys.modules["aihydro_modelling.train"]
    import ai_hydro.modelling.metrics as metrics_mod
    import ai_hydro.modelling.spec_gate as gate
    import ai_hydro.mcp.enforcement as enf
    from aihydro_modelling.spec import ModelSpec
    from ai_hydro.modelling import runner

    extract = MagicMock(return_value=(SimpleNamespace(), "X"))
    monkeypatch.setattr(metrics_mod, "extract_basin_data", extract)
    monkeypatch.setattr(gate, "require_trainable", lambda *a, **k: None)
    monkeypatch.setattr(train_mod, "train", lambda spec, data: SimpleNamespace(data={"nse": 0.7}))
    seen = {}

    def post_run(tool, sid, result, inputs=None):
        seen["inputs"] = inputs
        return result
    monkeypatch.setattr(enf, "post_run", post_run)

    cfg = {"job_id": "j", "session_id": "s", "run_id": "j", "streamflow_source": source,
           "spec": ModelSpec(backend="hbv").model_dump()}
    (tmp_path / "job_config.json").write_text(json.dumps(cfg))
    runner.run(tmp_path)

    assert extract.call_args.kwargs["streamflow_source"] == source
    assert seen["inputs"] == {"streamflow_source": source}
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["status"] == "complete"
    assert status["partial_results"]["streamflow_source"] == source


def test_runner_old_job_config_without_the_key_means_session(stub_session, monkeypatch, tmp_path):
    import sys
    import aihydro_modelling.train  # noqa: F401  (ensures the submodule is loaded)
    train_mod = sys.modules["aihydro_modelling.train"]
    import ai_hydro.modelling.metrics as metrics_mod
    import ai_hydro.modelling.spec_gate as gate
    from aihydro_modelling.spec import ModelSpec
    from ai_hydro.modelling import runner

    extract = MagicMock(return_value=(SimpleNamespace(), "X"))
    monkeypatch.setattr(metrics_mod, "extract_basin_data", extract)
    monkeypatch.setattr(gate, "require_trainable", lambda *a, **k: None)
    monkeypatch.setattr(train_mod, "train", lambda spec, data: SimpleNamespace(data={"nse": 0.7}))
    (tmp_path / "job_config.json").write_text(json.dumps(
        {"job_id": "j", "session_id": "s", "spec": ModelSpec(backend="hbv").model_dump()}))
    runner.run(tmp_path)
    assert extract.call_args.kwargs["streamflow_source"] == "session"


def test_search_runner_passes_source_and_records_it(stub_session, monkeypatch, tmp_path):
    import aihydro_modelling.search.loop as loop_mod
    import ai_hydro.modelling.metrics as metrics_mod
    from ai_hydro.mcp import search_runner

    extract = MagicMock(return_value=(SimpleNamespace(), "X"))
    monkeypatch.setattr(metrics_mod, "extract_basin_data", extract)
    result = {"n_experiments": 1, "n_improvements": 0, "budget_exhausted": True,
              "strategy_exhausted": False, "full_retrain_done": False,
              "incumbent_spec": {}, "incumbent_metrics": {}, "incumbent_ci": {}}
    monkeypatch.setattr(loop_mod, "run_loop", lambda **k: result)

    search_runner._run_inner({"session_id": "s", "job_id": "j", "backend": "hbv", "max_experiments": 1,
                              "streamflow_source": "camels"}, tmp_path, "j")

    assert extract.call_args.kwargs["streamflow_source"] == "camels"
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["streamflow_source"] == "camels"
