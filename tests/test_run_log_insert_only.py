"""
Insert-only run-log rows (ADR-001, slice 1a, acceptance criterion 6).

A row that carries a sealed ``aihydro.run/2`` record can never be mutated by a
same-id write. Identical writes are no-ops. Legacy (unsealed) rows keep the
old upsert behaviour so bench fixtures and whole-dict ``set("_run_log")``
writers continue to work.
"""
from __future__ import annotations

import json
import re
import sqlite3
from unittest.mock import patch

import pytest

from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession, _run_log_read_one, _run_log_record

SID = "insert-only"


@pytest.fixture
def sessions(tmp_path):
    with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
        HydroSession(SID).save()
        yield tmp_path


def _entry(run_id="r1", **kw):
    entry = {"run_id": run_id, "tool_name": "demo", "session_id": SID,
             "timestamp": "2026-10-02T00:00:00+00:00", "key_outputs": {"nse": 0.8}}
    entry.update(kw)
    return entry


def _sealed(entry, **kw):
    record = rr.build_run_record(run_id=entry["run_id"], tool="demo", session_id=SID,
                                 arguments={"a": 1}, result={"data": {"nse": 0.8}},
                                 entry=entry, **kw)
    return {**entry, "record": record.to_dict()}


def _stored(run_id="r1"):
    return _run_log_read_one(SID, run_id)


def test_new_row_is_inserted_verbatim(sessions):
    assert _run_log_record(SID, "r1", _entry()) == "inserted"
    assert _stored() == _entry()


def test_identical_sealed_rewrite_is_a_noop(sessions):
    sealed = _sealed(_entry())
    assert _run_log_record(SID, "r1", sealed) == "inserted"
    before = _stored()
    assert _run_log_record(SID, "r1", sealed) == "noop"
    assert _stored() == before


def test_different_record_for_existing_run_id_is_refused_and_logged(sessions, caplog):
    first = _sealed(_entry())
    second = _sealed(_entry(), extra={"note": "a different sealed record"})
    assert first["record"]["record_digest"] != second["record"]["record_digest"]
    _run_log_record(SID, "r1", first)
    with caplog.at_level("WARNING", logger="ai_hydro.session.store"):
        assert _run_log_record(SID, "r1", second) == "refused"
    assert _stored() == first
    assert "Refused write to sealed run-log row r1" in caplog.text


def test_changed_legacy_fields_on_a_sealed_row_are_refused(sessions):
    sealed = _sealed(_entry())
    _run_log_record(SID, "r1", sealed)
    tampered = {**sealed, "key_outputs": {"nse": 0.99}}
    assert _run_log_record(SID, "r1", tampered) == "refused"
    assert _stored()["key_outputs"] == {"nse": 0.8}
    # record-less overwrite with different fields is refused too
    assert _run_log_record(SID, "r1", _entry(key_outputs={"nse": 0.1})) == "refused"
    assert _stored() == sealed


def test_stale_whole_dict_snapshot_never_drops_the_record(sessions):
    """A writer that read the log before the record was attached re-sends its
    snapshot via the legacy whole-dict API. That must not strip the record."""
    snapshot = {"r1": _entry()}                 # what the stale writer saw
    _run_log_record(SID, "r1", _sealed(_entry()))
    HydroSession.load(SID).set("_run_log", snapshot)
    assert "record" in _stored()
    assert rr.verify_run_log_entry(_stored())["record_ok"] is True


def test_legacy_set_run_log_still_works_for_bare_dicts_and_upserts(sessions):
    s = HydroSession.load(SID)
    s.set("_run_log", {"bench": {"nse": 0.8}, "r2": _entry("r2")})
    assert s.get("_run_log")["bench"] == {"nse": 0.8}
    s.set("_run_log", {"bench": {"nse": 0.7}})            # unsealed rows stay upsertable
    assert s.get("_run_log")["bench"] == {"nse": 0.7}
    assert s.get("_run_log")["r2"]["key_outputs"] == {"nse": 0.8}


def test_attaching_a_record_to_an_unsealed_row(sessions):
    _run_log_record(SID, "r1", _entry())
    assert _run_log_record(SID, "r1", _sealed(_entry())) == "replaced"
    assert rr.verify_run_log_entry(_stored()) == {
        "has_record": True, "record_ok": True, "entry_ok": True, "record_error": None}


def test_attach_against_a_row_that_changed_is_stale_and_not_written(sessions):
    base = _entry()
    sealed = _sealed(base)                       # built against `base`
    _run_log_record(SID, "r1", {**base, "key_outputs": {"nse": 0.5}})   # row moved on
    assert _run_log_record(SID, "r1", sealed) == "stale"
    assert "record" not in _stored()


def test_record_that_does_not_verify_is_refused(sessions):
    sealed = _sealed(_entry())
    sealed["record"]["status"] = "error"         # edited after sealing
    assert _run_log_record(SID, "r1", sealed) == "refused"
    assert _stored() is None


def test_record_for_a_different_run_id_is_refused(sessions):
    sealed = _sealed(_entry("other"))
    assert _run_log_record(SID, "r1", sealed) == "refused"


def test_nan_in_key_outputs_compares_equal_to_itself(sessions):
    entry = _entry(key_outputs={"x": float("nan")})
    sealed = _sealed(entry)
    assert _run_log_record(SID, "r1", sealed) == "inserted"
    assert _run_log_record(SID, "r1", sealed) == "noop"


def test_row_equality_survives_a_json_round_trip_of_the_stored_row(sessions):
    sealed = _sealed(_entry(key_outputs={"nse": 0.1 + 0.2, "n": 3}))
    _run_log_record(SID, "r1", sealed)
    assert _run_log_record(SID, "r1", json.loads(json.dumps(sealed))) == "noop"


def test_concurrent_attach_and_stale_snapshot_leave_one_record(sessions):
    """Attach then re-send the pre-attach snapshot many times, interleaved."""
    base = _entry()
    _run_log_record(SID, "r1", base)
    sealed = _sealed(base)
    for _ in range(5):
        _run_log_record(SID, "r1", sealed)
        HydroSession.load(SID).set("_run_log", {"r1": base})
    assert _stored() == sealed
    with sqlite3.connect(str(store._run_log_db_path(SID))) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


# ------------------------------------------------------------------ run ids
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")           # audit/grammar.py _RUN_MARKER_RE id class


def test_generated_ids_match_the_auditor_grammar_and_carry_32_bits(sessions):
    from ai_hydro.mcp.enforcement import _generate_run_id

    run_id = _generate_run_id("extract_hydrological_signatures", SID)
    assert RUN_ID_RE.match(run_id)
    assert re.match(r"^sigs\.\d{8}\.inserto\.[0-9a-f]{8}$", run_id)


def test_unique_id_regenerates_on_collision(sessions):
    from ai_hydro.mcp import enforcement

    taken = enforcement._generate_run_id("demo", SID)
    _run_log_record(SID, taken, _entry(taken))
    fresh_then = iter([taken, taken, "demo.20261002.insertonl.deadbeef"])
    with patch.object(enforcement, "_generate_run_id", lambda t, s: next(fresh_then)):
        assert enforcement._generate_unique_run_id("demo", SID) == "demo.20261002.insertonl.deadbeef"


def test_unique_id_widens_if_collisions_persist(sessions):
    from ai_hydro.mcp import enforcement

    taken = "demo.20261002.insertonl.aaaaaaaa"
    _run_log_record(SID, taken, _entry(taken))
    with patch.object(enforcement, "_generate_run_id", lambda t, s: taken):
        widened = enforcement._generate_unique_run_id("demo", SID)
    assert widened != taken and widened.startswith(taken) and RUN_ID_RE.match(widened)


def test_post_run_cannot_replace_a_sealed_row_even_on_an_id_collision(sessions):
    from ai_hydro.mcp import enforcement

    taken = "demo.20261002.insertonl.aaaaaaaa"
    sealed = _sealed(_entry(taken))
    _run_log_record(SID, taken, sealed)
    # Force the writer (bypassing the collision check) onto the sealed id.
    enforcement._write_run_log(SID, taken, "demo", {"data": {"nse": 0.1}})
    assert _run_log_read_one(SID, taken) == sealed


def test_old_four_hex_ids_remain_valid_everywhere(sessions):
    legacy = "sigs.20260508.01031500.a3f2"
    assert RUN_ID_RE.match(legacy)
    assert _run_log_record(SID, legacy, _entry(legacy)) == "inserted"
