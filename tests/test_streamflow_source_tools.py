"""Tool kickoffs validate `streamflow_source` before spawning a job (P5).

Runs without the optional ``aihydro_modelling`` package: the option is validated first and the
streamflow prerequisite is checked ahead of the modelling gate's deferred refusal.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_tools_refuse_unknown_source_and_missing_flow_before_spawning(monkeypatch, tmp_path):
    from ai_hydro.mcp import tools_modelling as TM

    started = MagicMock()
    monkeypatch.setattr(TM.jobs, "start_job", started)

    bad = TM.train_hydro_model(session_id="s", streamflow_source="nope")
    assert bad["code"] == "INVALID_STREAMFLOW_SOURCE"

    bad = TM.propose_and_train(spec_json="{}", session_id="s", streamflow_source="nope")
    assert bad["code"] == "INVALID_STREAMFLOW_SOURCE"
    started.assert_not_called()


def test_hbv_without_session_flow_is_refused_as_missing_prerequisite(monkeypatch):
    from ai_hydro.mcp import tools_modelling as TM

    session = SimpleNamespace(watershed={"data": {}}, forcing={"data": {}}, streamflow=None,
                              site_id="01013500", workspace_dir=None)
    import ai_hydro.session as S
    monkeypatch.setattr(S.HydroSession, "load", classmethod(lambda cls, sid: session))
    started = MagicMock()
    monkeypatch.setattr(TM.jobs, "start_job", started)

    out = TM.train_hydro_model(session_id="s", framework="hbv")

    assert out["code"] == "MISSING_PREREQUISITES" and "streamflow" in out["message"]
    started.assert_not_called()


def test_run_autoresearch_refuses_unknown_source_before_spawning(monkeypatch):
    from ai_hydro.mcp import tools_modelling as TM

    started = MagicMock()
    monkeypatch.setattr(TM.jobs, "start_job", started)
    out = TM.run_autoresearch(hypothesis="h", session_id="s", streamflow_source="nope")
    assert out["code"] == "INVALID_STREAMFLOW_SOURCE"
    started.assert_not_called()


def test_run_autoresearch_without_session_flow_is_a_missing_prerequisite(monkeypatch):
    pytest = __import__("pytest")
    pytest.importorskip("aihydro_modelling")
    from ai_hydro.mcp import tools_modelling as TM
    import ai_hydro.session as S

    session = SimpleNamespace(watershed={"data": {}}, forcing={"data": {}}, streamflow=None,
                              site_id="01013500", workspace_dir=None)
    monkeypatch.setattr(S.HydroSession, "load", classmethod(lambda cls, sid: session))
    started = MagicMock()
    monkeypatch.setattr(TM.jobs, "start_job", started)
    out = TM.run_autoresearch(hypothesis="h", session_id="s")
    assert out["code"] == "MISSING_PREREQUISITES" and "streamflow" in out["message"]
    started.assert_not_called()


def test_edited_tool_descriptions_keep_100_chars_of_headroom():
    import asyncio
    from ai_hydro.mcp import mcp
    tools = asyncio.run(mcp.get_tools())
    for name in ("propose_and_train", "run_autoresearch"):
        assert len(tools[name].description or "") <= 1400, name
