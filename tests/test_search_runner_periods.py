"""The autoresearch entry point uses disjoint fit/select/report periods.

Selection runs on val_* for every backend, so a base spec whose val_* overlaps
train_* or test_* must be refused before any compute (R08 / HydroVerse U10a).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("aihydro_modelling")

from ai_hydro.mcp import search_runner


class _Captured(Exception):
    def __init__(self, spec):
        self.spec = spec


@pytest.fixture
def stubbed(monkeypatch):
    import aihydro_modelling.search.loop as loop_mod
    import ai_hydro.modelling.metrics as metrics_mod
    from ai_hydro import session as session_pkg

    monkeypatch.setattr(session_pkg.HydroSession, "load",
                        staticmethod(lambda sid: SimpleNamespace(workspace_dir=None)))
    monkeypatch.setattr(metrics_mod, "extract_basin_data",
                        lambda *a, **k: (SimpleNamespace(gauge_id="g"), "USGS+X"))

    def capture(**kwargs):
        raise _Captured(kwargs["base_spec"])
    monkeypatch.setattr(loop_mod, "run_loop", capture)


@pytest.mark.parametrize("backend", ["hbv", "nh_lstm"])
def test_default_periods_are_disjoint(stubbed, tmp_path, backend):
    from aihydro_modelling.validate import validate

    with pytest.raises(_Captured) as got:
        search_runner._run_inner(
            {"session_id": "s", "backend": backend, "max_experiments": 1},
            tmp_path, "job",
        )
    spec = got.value.spec
    assert spec.train_end < spec.val_start <= spec.val_end < spec.test_start
    assert validate(spec) == []


def test_select_period_inside_training_is_refused(stubbed, tmp_path):
    with pytest.raises(ValueError, match="Autoresearch refused"):
        search_runner._run_inner(
            {"session_id": "s", "backend": "hbv", "max_experiments": 1,
             "train_end": "2007-09-30"},  # swallows the default val window
            tmp_path, "job",
        )


def test_select_period_inside_test_is_refused(stubbed, tmp_path):
    with pytest.raises(ValueError, match="overlaps the test period"):
        search_runner._run_inner(
            {"session_id": "s", "backend": "nh_lstm", "max_experiments": 1,
             "val_start": "2007-10-01", "val_end": "2009-09-30"},  # the old NH default
            tmp_path, "job",
        )
