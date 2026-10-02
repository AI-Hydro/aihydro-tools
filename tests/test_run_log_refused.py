"""A writer's refused body against a sealed row is reported (skeptic-slice3 R3)."""
from __future__ import annotations

from ai_hydro.session import run_records as rr
from ai_hydro.session import store

from test_run_record_middleware import SID, env  # noqa: F401

ENTRY = {"tool_name": "t", "timestamp": "1", "key_outputs": {"a": 1}}


def _row(rid):
    return store._run_log_read_one(SID, rid)


def test_writer_refused_by_sealed_row_is_reported(env):
    sealed = rr.build_run_record(run_id="w1", tool="t", session_id=SID, entry=ENTRY).to_dict()
    assert store._run_log_record(SID, "w1", {**ENTRY, "record": sealed}) == "inserted"
    capture, token = rr.begin_capture()
    try:
        status = store._run_log_record(SID, "w1", {**ENTRY, "key_outputs": {"a": 99}}, writer="put_result")
    finally:
        rr.end_capture(token)
    assert status == "refused"
    outcome = rr.record_call(
        tool="t", arguments={}, result={"data": {}}, failure=None, capture=capture,
        chat_id=None, id_factory=lambda t, s: "x.1")
    assert "w1: writer_row_refused" in outcome.record_error
    assert _row("w1")["key_outputs"] == {"a": 1}       # sealed body kept
    assert _row("w1")["record"]["record_digest"] == sealed["record_digest"]
