"""The model step's observed-flow source is explicit and recorded (P5).

`extract_basin_data` used to call ``pygeohydro.get_camels()`` before it looked at the study's
own flow: a network host nobody listed, and a different observed series. The default is now the
session flow with no CAMELS call; CAMELS is an explicit, recorded opt-in; missing flow is a
structured error, never a silent fallback.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("aihydro_modelling")
pytest.importorskip("aihydro_core")

from aihydro_core.primitives.errors import ToolError

from ai_hydro.modelling import metrics


def _session(with_flow: bool = True):
    return SimpleNamespace(
        watershed={"data": {"area_km2": 100.0, "gauge_lat": 39.2, "gauge_lon": -77.4}},
        camels=None,
        _with_flow=with_flow,
    )


@pytest.fixture
def slots(monkeypatch):
    forcing = {"dates": ["2000-01-01"], "prcp_mm": [1.0], "product": "GRIDMET"}

    def load(session, slot, gauge_id):
        if slot == "streamflow":
            if not session._with_flow:
                raise ValueError(f"No streamflow cached for gauge {gauge_id}. Run fetch_streamflow_data first.")
            return {"dates": ["2000-01-01"], "q_cms": [1.0]}
        return forcing

    monkeypatch.setattr(metrics, "_load_full_data", load)
    return forcing


@pytest.fixture
def fake_pygeohydro(monkeypatch):
    """A pygeohydro whose get_camels records any call (offline: never touches the network)."""
    get_camels = MagicMock(name="get_camels", side_effect=AssertionError("network host reached"))
    monkeypatch.setitem(sys.modules, "pygeohydro", types.SimpleNamespace(get_camels=get_camels))
    return get_camels


def test_default_uses_session_flow_and_never_calls_get_camels(slots, fake_pygeohydro, tmp_path):
    data, label = metrics.extract_basin_data(_session(), "01013500", tmp_path)

    fake_pygeohydro.assert_not_called()
    assert label == "USGS+GRIDMET"
    assert set(data.q_by_date) == {"2000-01-01"}


def test_default_does_not_even_import_the_camels_path(slots, monkeypatch, tmp_path):
    called = MagicMock()
    monkeypatch.setattr(metrics, "fetch_camels_streamflow", called)
    metrics.extract_basin_data(_session(), "01013500", tmp_path)
    called.assert_not_called()


def test_explicit_camels_is_used_and_labelled(slots, monkeypatch, tmp_path):
    seen = MagicMock(return_value={"2000-01-01": 0.5})
    monkeypatch.setattr(metrics, "fetch_camels_streamflow", seen)

    data, label = metrics.extract_basin_data(_session(with_flow=False), "01013500", tmp_path,
                                             streamflow_source="camels")

    seen.assert_called_once()
    assert label == "CAMELS+GRIDMET"
    assert data.q_by_date == {"2000-01-01": 0.5}


def test_record_is_exactly_the_closed_source_enum():
    assert metrics.streamflow_record("session") == {"streamflow_source": "session"}
    assert metrics.streamflow_record("camels") == {"streamflow_source": "camels"}
    assert metrics.streamflow_record(None) == {"streamflow_source": "session"}


def test_camels_unavailable_carries_the_reason(slots, monkeypatch, tmp_path):
    """'not in CAMELS' and 'download failed' are told apart (details + message)."""
    import types
    ds = types.SimpleNamespace(coords={"station_id": types.SimpleNamespace(
        values=types.SimpleNamespace(astype=lambda t: ["99999999"]))})
    monkeypatch.setitem(sys.modules, "pygeohydro", types.SimpleNamespace(get_camels=lambda: (None, ds)))
    with pytest.raises(ToolError) as e:
        metrics.extract_basin_data(_session(), "01013500", tmp_path, streamflow_source="camels")
    assert e.value.details["reason"] == "not_in_camels" and "not_in_camels" in str(e.value.message)

    def boom():
        raise OSError("dns failure")
    monkeypatch.setitem(sys.modules, "pygeohydro", types.SimpleNamespace(get_camels=boom))
    with pytest.raises(ToolError) as e:
        metrics.extract_basin_data(_session(), "01013500", tmp_path, streamflow_source="camels")
    assert e.value.details["reason"].startswith("download_failed") and "dns failure" in e.value.details["reason"]


def test_missing_session_flow_is_a_structured_error_not_a_camels_fallback(slots, fake_pygeohydro, tmp_path):
    with pytest.raises(ToolError) as e:
        metrics.extract_basin_data(_session(with_flow=False), "01013500", tmp_path)
    assert e.value.code == "MISSING_STREAMFLOW"
    assert "streamflow_source='camels'" in (e.value.recovery or "")
    fake_pygeohydro.assert_not_called()


def test_camels_requested_but_unavailable_does_not_fall_back_to_session(slots, monkeypatch, tmp_path):
    monkeypatch.setattr(metrics, "fetch_camels_streamflow", lambda *a, **k: {})
    with pytest.raises(ToolError) as e:
        metrics.extract_basin_data(_session(), "01013500", tmp_path, streamflow_source="camels")
    assert e.value.code == "CAMELS_STREAMFLOW_UNAVAILABLE"


def test_unknown_source_is_refused(slots, tmp_path):
    with pytest.raises(ToolError) as e:
        metrics.extract_basin_data(_session(), "01013500", tmp_path, streamflow_source="usgs-live")
    assert e.value.code == "INVALID_STREAMFLOW_SOURCE"


def test_lineage_and_latitude_pass_only_when_training_data_declares_them(slots, monkeypatch, tmp_path):
    """Works against a modelling release with and without the lineage fields (no dependence on either)."""
    import dataclasses
    from pathlib import Path
    from typing import Any, Optional

    import aihydro_modelling

    @dataclasses.dataclass
    class OldTD:
        gauge_id: str
        output_dir: Path
        forcing: dict
        q_by_date: dict
        static_attrs: dict
        data_source: str

    @dataclasses.dataclass
    class NewTD(OldTD):
        streamflow_source: Optional[str] = None
        streamflow_area_km2: Optional[float] = None
        streamflow_unit_conversion: Optional[str] = None
        streamflow_fallback_reason: Optional[str] = None
        latitude: Optional[float] = None
        latitude_deg: Optional[float] = None

    monkeypatch.setattr(aihydro_modelling, "TrainingData", OldTD)
    old, _ = metrics.extract_basin_data(_session(), "01013500", tmp_path)
    assert not hasattr(old, "streamflow_source")

    monkeypatch.setattr(aihydro_modelling, "TrainingData", NewTD)
    new, _ = metrics.extract_basin_data(_session(), "01013500", tmp_path)
    assert new.streamflow_source == "session"
    assert new.streamflow_area_km2 == 100.0
    assert new.streamflow_fallback_reason is None
    assert new.latitude == new.latitude_deg == 39.2
    assert "m3/s" in new.streamflow_unit_conversion


def test_none_gauge_lat_in_static_attrs_is_replaced_by_the_watershed_value(slots, tmp_path):
    sess = _session()
    sess.camels = {"data": {"gauge_lat": None, "area_gages2": None}}
    data, _ = metrics.extract_basin_data(sess, "01013500", tmp_path)
    assert data.static_attrs["gauge_lat"] == 39.2
    assert data.static_attrs["area_gages2"] == 100.0
