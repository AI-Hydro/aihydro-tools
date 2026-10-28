"""Acceptance test (T-B1/T-B2): the 2026-10-28 failure, replayed.

A chat launched from the map calls the read-only ``measure_feature`` with no ``session_id``. Before
2.8.2 it ended in ``SESSION_RESOLUTION_FAILED`` with no way out (the recovery tools read no chat id and
bound nothing). The rewritten test (PSMH 7.5, as corrected by the UX program, finding B1):

* the host binds BEFORE the first call (a host-originated ``aihydro_rebind_chat``), because a read-only
  call never binds; the first call then resolves by rule ``chat_binding``;
* with nothing bound, a one-study folder resolves, and a two-study folder answers with labelled
  candidates and a recovery tool that works.

It never ends in SESSION_RESOLUTION_FAILED without a recovery path.
"""
from __future__ import annotations

import pytest

from binding_helpers import CHAT_A, CHAT_B, call, ctx, make_study, read_resource

from ai_hydro.session import HydroSession
from ai_hydro.session.chat_binding import get_binding_store

POLY = '{"type": "Polygon", "coordinates": [[[-69.5, 45.0], [-69.0, 45.0], [-69.0, 45.4], [-69.5, 45.0]]]}'


@pytest.fixture
def folder(tmp_path):
    """The owner's ``try`` folder: two studies for one gauge, one of which owns the layer's file."""
    d = tmp_path / "try"
    d.mkdir()
    make_study("01031500", workspace=str(d), site_id="01031500", site_name="Piscataquis River near Dover-Foxcroft, ME",
               site_type="usgs_gauge")
    make_study("piscataquis-river-dover-foxcroft-01031500", workspace=str(d))
    (d / "watershed_01031500.geojson").write_text(POLY)
    return d


def _measure(chat, ws):
    return call("measure_feature", {"feature": POLY}, meta=ctx(chat, ws))


def test_host_bound_chat_resolves_by_chat_binding_and_the_other_chat_is_unchanged(folder):
    # another chat is already bound to the same study
    assert call("aihydro_rebind_chat", {"study_id": "01031500"}, meta=ctx(CHAT_B, folder))[1]["bound_to"] == "01031500"
    # the map launch: host resolves the layer's study, then binds the NEW chat before its first message
    layer = read_resource(f"aihydro://study/by-artifact?path={folder / 'watershed_01031500.geojson'}")
    assert {c["study_id"] for c in layer["candidates"]} == {"01031500", "piscataquis-river-dover-foxcroft-01031500"}
    chosen = "01031500"                                                     # the user picks from the labelled chip
    host = call("aihydro_rebind_chat", {"study_id": chosen}, meta=ctx(CHAT_A, folder))[1]
    assert host["bound_to"] == chosen
    # the agent's first call is read-only and has no session_id
    res, body = _measure(CHAT_A, folder)
    assert not res.is_error and not body.get("error"), body
    assert body["data"]["area_km2"] > 0
    assert "SESSION_RESOLUTION_FAILED" not in str(body)
    run = [r for r in (HydroSession.load(chosen).get("_run_log") or {}).values() if r.get("tool_name") == "measure_feature"
           or (r.get("record") or {}).get("tool") == "measure_feature"]
    assert run and (run[-1]["record"]["extra"]["session_resolution"] == "chat_binding")
    # the other chat is untouched, and the read-only call bound nothing new
    store = get_binding_store()
    assert store.lookup_study(CHAT_B) == "01031500" and store.lookup_study(CHAT_A) == "01031500"
    assert call("aihydro_chat_status", {}, meta=ctx(CHAT_A, folder))[1]["bound"] is True


def test_unbound_chat_in_a_two_study_folder_gets_labels_and_a_working_recovery(folder):
    res, body = _measure(CHAT_A, folder)
    assert body["code"] == "SESSION_RESOLUTION_FAILED"
    assert len(body["candidates"]) == 2 and all(c["label"] != c["study_id"] for c in body["candidates"])
    labels = {c["study_id"]: c["label"] for c in body["candidates"]}
    assert labels["01031500"] == "Piscataquis River near Dover-Foxcroft, ME (USGS 01031500)"
    assert "aihydro_rebind_chat" in body["next_tools"]
    # follow the recovery path the error names: the agent's own rebind, no chat_id argument
    pick = body["candidates"][0]["study_id"]
    assert call("aihydro_rebind_chat", {"study_id": pick}, meta=ctx(CHAT_A, folder))[1]["bound_to"] == pick
    assert call("aihydro_chat_status", {}, meta=ctx(CHAT_A, folder))[1]["bound"] is True
    res, ok = _measure(CHAT_A, folder)
    assert "error" not in ok and ok["data"]["area_km2"] > 0


def test_one_study_in_the_folder_resolves_on_the_first_call_with_nothing_bound(folder):
    import shutil

    shutil.rmtree(HydroSession._path("piscataquis-river-dover-foxcroft-01031500", None), ignore_errors=True)
    HydroSession._path("piscataquis-river-dover-foxcroft-01031500", None).unlink(missing_ok=True)
    res, body = _measure(CHAT_A, folder)
    assert not body.get("error"), body
    assert body["session_note"].startswith("Used the only study in this workspace")
    assert get_binding_store().lookup_study(CHAT_A) is None                 # read-only: resolved, not bound
