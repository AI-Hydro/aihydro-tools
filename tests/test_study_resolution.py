"""Study resolution chain (2.8.2): workspace single study, labelled candidates, off-by-default guess,
per-call workspace beating the global file, and the recovery tools reading the injected chat id."""
from __future__ import annotations

import json

import pytest

from binding_helpers import CHAT_A, CHAT_B, call, ctx, make_study

from ai_hydro.mcp.helpers import SessionResolutionError, _resolve_session_with_rule
from ai_hydro.registry.paths import aihydro_home
from ai_hydro.session.chat_binding import get_binding_store


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "try"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _no_eval_or_fallback(monkeypatch):
    monkeypatch.delenv("AIHYDRO_RECENT_SESSION_FALLBACK", raising=False)
    monkeypatch.delenv("AIHYDRO_EVAL_NONCE", raising=False)


def test_recent_fallback_is_off_by_default(ws):
    make_study("only_elsewhere")                                    # no workspace: not in this folder
    from ai_hydro.mcp.helpers import recent_fallback_enabled

    assert recent_fallback_enabled() is False
    with pytest.raises(SessionResolutionError):
        _resolve_session_with_rule(None, None)


@pytest.mark.parametrize("value", ["on", "1", "true", "yes"])
def test_recent_fallback_is_an_opt_in(monkeypatch, value):
    from ai_hydro.mcp.helpers import recent_fallback_enabled

    monkeypatch.setenv("AIHYDRO_RECENT_SESSION_FALLBACK", value)
    assert recent_fallback_enabled() is True
    monkeypatch.setenv("AIHYDRO_EVAL_NONCE", "n")
    assert recent_fallback_enabled() is False                       # never in an evaluation run


def test_read_only_call_resolves_the_one_study_in_the_workspace_without_binding(ws):
    make_study("01031500", workspace=str(ws), site_id="01031500", site_name="Piscataquis River", site_type="usgs_gauge")
    res, body = call("list_features", {}, meta=ctx(CHAT_A, ws))
    assert body["session_id"] == "01031500"
    assert body["session_note"].startswith("Used the only study in this workspace: Piscataquis River (USGS 01031500)")
    assert get_binding_store().lookup_study(CHAT_A) is None         # a read-only call never binds
    assert "session_binding" not in body


def test_a_writer_resolves_the_one_study_binds_the_chat_and_says_so(ws):
    make_study("01031500", workspace=str(ws), site_id="01031500", site_name="Piscataquis River", site_type="usgs_gauge")
    poly = '{"type": "Polygon", "coordinates": [[[-69.5, 45.0], [-69.0, 45.0], [-69.0, 45.4], [-69.5, 45.0]]]}'
    res, body = call("register_feature", {"geojson": poly, "name": "roi"}, meta=ctx(CHAT_A, ws))
    assert not res.is_error and not body.get("error"), body
    assert get_binding_store().lookup_study(CHAT_A) == "01031500"
    assert body["session_binding"] == {
        "chat_id": CHAT_A, "study_id": "01031500", "label": "Piscataquis River (USGS 01031500)",
        "previous_study_id": None, "bound_by": "workspace_single_study"}


def test_two_studies_in_the_folder_give_labelled_candidates_and_a_recovery_tool(ws):
    make_study("01031500", workspace=str(ws), site_id="01031500", site_name="Piscataquis River", site_type="usgs_gauge")
    make_study("piscataquis-01031500", workspace=str(ws))
    res, body = call("list_features", {}, meta=ctx(CHAT_A, ws))
    assert body["code"] == "SESSION_RESOLUTION_FAILED"
    ids = {c["study_id"] for c in body["candidates"]}
    assert ids == {"01031500", "piscataquis-01031500"}
    assert all(c["label"] and c["label"] != c["study_id"] and "last_run" in c for c in body["candidates"])
    assert "aihydro_rebind_chat" in body["next_tools"]
    assert "aihydro_rebind_chat" in body["recovery"]


def test_candidates_are_capped_at_five_and_hidden_studies_are_not_offered(ws):
    for i in range(8):
        make_study(f"basin_{i}", workspace=str(ws))
    make_study("test-promotion")                                   # hidden: test id, no workspace
    other = ws.parent / "elsewhere"
    other.mkdir()
    res, body = call("list_features", {}, meta=ctx(CHAT_A, ws))
    assert len(body["candidates"]) == 5
    assert all(c["study_id"].startswith("basin_") for c in body["candidates"])
    # a workspace with no study of its own is offered the recent visible ones, never the hidden ones
    res, body = call("list_features", {}, meta=ctx(CHAT_A, other))
    assert body["candidates"] and all(not c["study_id"].startswith("test-") for c in body["candidates"])


def test_the_audit_and_state_changing_admin_tools_never_infer_a_study(ws):
    make_study("01031500", workspace=str(ws))
    with pytest.raises(SessionResolutionError):
        _resolve_session_with_rule(None, CHAT_A, allow_recent_fallback=False)   # the audit's call
    res, body = call("clear_session", {}, meta=ctx(CHAT_A, ws))                 # admin, writes
    assert body["code"] == "SESSION_RESOLUTION_FAILED"


def test_an_evaluation_run_never_infers_a_study(ws, monkeypatch):
    make_study("01031500", workspace=str(ws))
    monkeypatch.setattr("ai_hydro.mcp.helpers._in_eval", lambda: True)
    res, body = call("list_features", {}, meta=ctx(CHAT_A, ws))
    assert body["code"] == "SESSION_RESOLUTION_FAILED"


def test_a_study_with_no_workspace_is_not_inferred_for_a_folder(ws):
    make_study("elsewhere")                                         # no workspace_dir
    res, body = call("list_features", {}, meta=ctx(CHAT_A, ws))
    assert body["code"] == "SESSION_RESOLUTION_FAILED"


# ---------------------------------------------------- the global workspace file

def _write_global_workspace(path):
    f = aihydro_home() / "active_workspace.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"workspace": str(path)}))


def test_active_workspace_json_is_ignored_when_the_call_carries_its_own_context(ws, tmp_path):
    other = tmp_path / "other_window"
    other.mkdir()
    make_study("mine", workspace=str(ws))
    make_study("theirs", workspace=str(other))
    _write_global_workspace(other)                                  # the last-focused window
    res, body = call("list_features", {}, meta=ctx(CHAT_A, ws))
    assert body["session_id"] == "mine"
    # _meta without a workspace does not fall back to the global file either
    res, body = call("list_features", {}, meta=ctx(CHAT_A))
    assert body["code"] == "SESSION_RESOLUTION_FAILED"


def test_active_workspace_json_still_serves_a_call_with_no_context(ws):
    make_study("mine", workspace=str(ws))
    _write_global_workspace(ws)
    res, body = call("list_features", {})
    assert body["session_id"] == "mine"


# ------------------------------------------------------------ recovery tools

def test_rebind_with_no_chat_id_binds_and_chat_status_reports_it(ws):
    make_study("01031500", workspace=str(ws), site_id="01031500", site_name="Piscataquis River", site_type="usgs_gauge")
    make_study("other", workspace=str(ws))
    res, body = call("aihydro_rebind_chat", {"study_id": "01031500"}, meta=ctx(CHAT_A, ws))
    assert body["bound_to"] == "01031500" and body["chat_id"] == CHAT_A
    assert body["session_binding"]["study_id"] == "01031500"
    res, status = call("aihydro_chat_status", {}, meta=ctx(CHAT_A, ws))
    assert status["bound"] is True and status["study_id"] == "01031500" and status["chat_id"] == CHAT_A
    # and the next read-only call resolves by the binding
    res, body = call("list_features", {}, meta=ctx(CHAT_A, ws))
    assert body["session_id"] == "01031500" and "session_note" not in body


def test_rebinding_one_chat_leaves_the_other_chat_bound_to_the_same_study(ws):
    make_study("s1", workspace=str(ws))
    make_study("s2", workspace=str(ws))
    call("aihydro_rebind_chat", {"study_id": "s1"}, meta=ctx(CHAT_A, ws))
    call("aihydro_rebind_chat", {"study_id": "s1"}, meta=ctx(CHAT_B, ws))
    call("aihydro_rebind_chat", {"study_id": "s2"}, meta=ctx(CHAT_A, ws))
    store = get_binding_store()
    assert store.lookup_study(CHAT_A) == "s2" and store.lookup_study(CHAT_B) == "s1"


def test_an_explicit_chat_id_must_equal_the_injected_one(ws):
    make_study("s1", workspace=str(ws))
    res, body = call("aihydro_rebind_chat", {"study_id": "s1", "chat_id": CHAT_B}, meta=ctx(CHAT_A, ws))
    assert body["code"] == "CHAT_ID_MISMATCH"
    assert get_binding_store().lookup_study(CHAT_B) is None and get_binding_store().lookup_study(CHAT_A) is None


def test_a_study_id_as_chat_id_is_refused_outside_a_chat_context(ws):
    make_study("s1", workspace=str(ws))
    res, body = call("aihydro_rebind_chat", {"study_id": "s1", "chat_id": "s1"})
    assert body["code"] == "INVALID_CHAT_ID"
    res, body = call("aihydro_rebind_chat", {"study_id": "s1"})
    assert body["code"] == "NO_CHAT_CONTEXT"


def test_rebinding_a_missing_study_lists_candidates_and_creates_nothing(ws):
    make_study("real", workspace=str(ws))
    res, body = call("aihydro_rebind_chat", {"study_id": "typo"}, meta=ctx(CHAT_A, ws))
    assert body["code"] == "STUDY_NOT_FOUND"
    assert {c["study_id"] for c in body["candidates"]} == {"real"}
    from ai_hydro.session.study_index import study_exists

    assert not study_exists("typo")


def test_chat_status_output_shape_is_frozen(ws):
    """The answer audit and the 0.4.0 header read these keys; changing them breaks both."""
    make_study("s1", workspace=str(ws))
    make_study("s2", workspace=str(ws))
    res, unbound = call("aihydro_chat_status", {}, meta=ctx(CHAT_A, ws))
    assert set(unbound) == {"bound", "study_id", "chat_id", "message"}
    assert unbound["bound"] is False and unbound["study_id"] is None and unbound["chat_id"] == CHAT_A
    assert "s1" in unbound["message"] and "aihydro_rebind_chat" in unbound["message"]
    call("aihydro_rebind_chat", {"study_id": "s1"}, meta=ctx(CHAT_A, ws))
    res, bound = call("aihydro_chat_status", {}, meta=ctx(CHAT_A, ws))
    assert set(bound) == {"bound", "study_id", "chat_id", "computed_slots", "notes", "message"}
    assert bound["bound"] is True and bound["study_id"] == "s1"
    assert isinstance(bound["computed_slots"], list) and isinstance(bound["notes"], int)


def test_start_session_with_an_id_binds_the_chat(ws):
    res, body = call("start_session", {"session_id": "fresh-study"}, meta=ctx(CHAT_A, ws))
    assert not res.is_error
    assert get_binding_store().lookup_study(CHAT_A) == "fresh-study"
    assert body["session_binding"]["bound_by"] == "start_session"


def test_start_session_without_an_id_or_with_a_shard_does_not_bind(ws):
    call("start_session", {}, meta=ctx(CHAT_A, ws))
    call("start_session", {"session_id": "forked", "shard_id": "w1"}, meta=ctx(CHAT_B, ws))
    assert get_binding_store().lookup_study(CHAT_A) is None
    assert get_binding_store().lookup_study(CHAT_B) is None


def test_start_session_does_not_bind_in_an_evaluation_run(ws, monkeypatch):
    monkeypatch.setattr("ai_hydro.mcp.eval_condition.in_evaluation_run", lambda: True)
    call("start_session", {"session_id": "evalstudy"}, meta=ctx(CHAT_A, ws))
    assert get_binding_store().lookup_study(CHAT_A) is None


def test_a_binding_change_is_reported_once_and_not_when_nothing_changed(ws):
    make_study("s1", workspace=str(ws))
    res, first = call("add_note", {"session_id": "s1", "note": "a"}, meta=ctx(CHAT_A, ws))
    assert first["session_binding"]["study_id"] == "s1" and first["session_binding"]["previous_study_id"] is None
    res, second = call("add_note", {"session_id": "s1", "note": "b"}, meta=ctx(CHAT_A, ws))
    assert "session_binding" not in second


def test_capabilities_resource_advertises_the_flags():
    from binding_helpers import read_resource

    flags = read_resource("aihydro://capabilities")["capabilities"]
    for needed in ("binding.v2", "binding.session_binding_field", "study.by_artifact", "study.list", "artifact.recorder"):
        assert needed in flags


def test_a_non_ulid_chat_id_is_not_bound_and_the_skip_is_logged_at_warning(ws, caplog):
    import logging

    from ai_hydro.mcp.helpers import _resolve_session

    make_study("s1", workspace=str(ws))
    with caplog.at_level(logging.WARNING, logger="ai_hydro.mcp"):
        assert _resolve_session("s1", "chat-001") == "s1"          # the call still resolves
    assert get_binding_store().lookup_study("chat-001") is None
    assert any(r.levelno == logging.WARNING and "not a ULID" in r.getMessage() for r in caplog.records)


def test_an_explicit_typo_does_not_bind_the_chat(ws):
    res, body = call("add_note", {"session_id": "Bogus", "note": "x"}, meta=ctx(CHAT_A, ws))
    assert get_binding_store().lookup_study(CHAT_A) is None


def test_start_session_does_not_pull_a_chat_into_a_foreign_workspace_study(ws, tmp_path):
    other = tmp_path / "other_project"
    other.mkdir()
    make_study("theirs", workspace=str(other))
    call("aihydro_rebind_chat", {"study_id": "theirs"}, meta=ctx(CHAT_A, other))
    make_study("mine", workspace=str(ws))
    call("aihydro_rebind_chat", {"study_id": "mine"}, meta=ctx(CHAT_B, ws))
    res, body = call("start_session", {"session_id": "theirs"}, meta=ctx(CHAT_B, ws))
    assert get_binding_store().lookup_study(CHAT_B) == "mine"
    assert "different workspace" in body["session_binding_skipped"]


def test_start_session_binds_an_existing_study_of_this_workspace_and_reports_the_move(ws):
    make_study("a", workspace=str(ws))
    make_study("b", workspace=str(ws))
    call("aihydro_rebind_chat", {"study_id": "a"}, meta=ctx(CHAT_A, ws))
    res, body = call("start_session", {"session_id": "b"}, meta=ctx(CHAT_A, ws))
    assert body["session_binding"]["previous_study_id"] == "a" and body["session_binding"]["study_id"] == "b"


def test_new_study_via_start_session_binds_only_with_a_workspace(ws):
    call("start_session", {"session_id": "new-with-ws"}, meta=ctx(CHAT_A, ws))
    assert get_binding_store().lookup_study(CHAT_A) == "new-with-ws"
    res, body = call("start_session", {"session_id": "new-no-ws"}, meta=ctx(CHAT_B))
    assert get_binding_store().lookup_study(CHAT_B) is None and "without a workspace" in body["session_binding_skipped"]


def test_rebind_refuses_hidden_studies_unless_asked_and_in_eval_runs(ws, monkeypatch):
    make_study("old", workspace=str(ws), archived=True)
    res, body = call("aihydro_rebind_chat", {"study_id": "old"}, meta=ctx(CHAT_A, ws))
    assert body["code"] == "STUDY_HIDDEN" and get_binding_store().lookup_study(CHAT_A) is None
    res, body = call("aihydro_rebind_chat", {"study_id": "old", "include_hidden": True}, meta=ctx(CHAT_A, ws))
    assert body["bound_to"] == "old"
    monkeypatch.setattr("ai_hydro.mcp.eval_condition.in_evaluation_run", lambda: True)
    res, body = call("aihydro_rebind_chat", {"study_id": "old", "include_hidden": True}, meta=ctx(CHAT_B, ws))
    assert body["code"] == "NOT_AVAILABLE_IN_EVALUATION"


def test_workspace_from_the_global_file_serves_reads_but_never_writers(ws):
    make_study("only", workspace=str(ws))
    _write_global_workspace(ws)
    res, body = call("list_features", {})
    assert body["session_id"] == "only"
    poly = '{"type": "Polygon", "coordinates": [[[-69.5, 45.0], [-69.0, 45.0], [-69.0, 45.4], [-69.5, 45.0]]]}'
    res, body = call("register_feature", {"geojson": poly, "name": "roi"})
    assert body.get("code") == "SESSION_RESOLUTION_FAILED"


def test_start_session_never_rehomes_a_study_into_another_workspace(ws, tmp_path):
    from ai_hydro.session import HydroSession

    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    make_study("B", workspace=str(ws2))
    res, body = call("start_session", {"session_id": "B", "workspace_dir": str(ws)}, meta=ctx(CHAT_A, ws))
    assert not res.is_error
    assert "different workspace" in body["session_binding_skipped"]
    assert get_binding_store().lookup_study(CHAT_A) is None
    assert HydroSession.load("B").workspace_dir == str(ws2)                  # unchanged on disk
    assert body["workspace_dir"] == str(ws2)
