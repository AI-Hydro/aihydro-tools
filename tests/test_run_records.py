"""
Run-record construction (ADR-001, slice 1a): what is digested, what is not,
and that building a record never raises.
"""
from __future__ import annotations

import math

import pytest

from aihydro_core.records import RunRecord, is_digest, verify_record_dict

from ai_hydro.session import run_records as rr


def _build(**kw):
    base = dict(run_id="t.20261002.s.00000000", tool="demo", session_id="s",
                arguments={"a": 1}, result={"data": {"x": 1.5}})
    base.update(kw)
    return rr.build_run_record(**base)


class TestDigests:
    def test_sealed_and_verifies(self):
        rec = _build()
        assert rec.schema == "aihydro.run/2"
        assert rec.verify()
        assert verify_record_dict(rec.to_dict())
        assert is_digest(rec.input_digest) and is_digest(rec.output_digest) and is_digest(rec.env_digest)
        assert rec.record_error is None

    def test_same_inputs_share_input_digest_across_sessions_and_runs(self):
        a = _build(run_id="a.1", session_id="s1", arguments={"gauge": "01031500", "session_id": "s1"})
        b = _build(run_id="b.2", session_id="s2", arguments={"session_id": "s2", "gauge": "01031500"})
        assert a.input_digest == b.input_digest
        assert a.record_digest != b.record_digest

    def test_different_inputs_differ(self):
        assert _build(arguments={"a": 1}).input_digest != _build(arguments={"a": 2}).input_digest

    def test_private_ctx_session_and_secret_keys_are_excluded_at_any_depth(self):
        clean = {"gauge": "1", "opts": {"n": 3}}
        noisy = {
            "gauge": "1", "ctx": object(), "session": "x", "session_id": "s", "_chat_id": "c",
            "api_key": "k", "opts": {"n": 3, "auth_token": "t", "_private": 1, "client_secret": "z"},
        }
        assert _build(arguments=noisy).input_digest == _build(arguments=clean).input_digest

    def test_session_id_is_only_dropped_at_top_level(self):
        nested = {"filters": {"session_id": "inner"}}
        assert _build(arguments=nested).input_digest != _build(arguments={"filters": {}}).input_digest

    def test_long_strings_are_included_in_full(self):
        # The old run-log scrub kept only the length of long strings; the
        # digest must distinguish two long blobs that differ in the tail.
        base = "g" * 1000
        assert (_build(arguments={"geom": base + "a"}).input_digest
                != _build(arguments={"geom": base + "b"}).input_digest)

    def test_output_digest_covers_data_only(self):
        a = _build(result={"data": {"x": 1}, "_run_id": "r1", "quality_flags": [{"status": "pass"}], "meta": {"t": 1}})
        b = _build(result={"data": {"x": 1}, "_run_id": "r2", "quality_flags": [], "meta": {"t": 2}})
        assert a.output_digest == b.output_digest

    def test_output_digest_without_data_key_excludes_transport_keys_only(self):
        a = _build(result={"value": 3, "_run_id": "r1", "_record_error": "e", "next_steps": [1]})
        b = _build(result={"value": 3})
        c = _build(result={"value": 4})
        assert a.output_digest == b.output_digest != c.output_digest

    def test_nan_and_inf_are_digested_not_rejected(self):
        rec = _build(result={"data": {"x": float("nan"), "y": float("inf")}})
        assert rec.record_error is None and is_digest(rec.output_digest)
        assert rec.output_digest != _build(result={"data": {"x": 1.0, "y": 2.0}}).output_digest


class TestNeverRaises:
    class Abbreviated:
        def __str__(self):
            return "<frame 3 rows ...>"

    def test_unencodable_output_is_recorded_not_raised(self):
        rec = _build(result={"data": {"frame": self.Abbreviated()}})
        assert rec.output_digest is None
        assert rec.record_error and "output_digest" in rec.record_error and "unencodable" in rec.record_error
        assert rec.verify()          # still sealed: the error is part of the record

    def test_unencodable_input_is_recorded_not_raised(self):
        rec = _build(arguments={"obj": self.Abbreviated()})
        assert rec.input_digest is None and "input_digest" in rec.record_error and rec.verify()

    def test_output_skip_reason_is_explicit(self):
        rec = _build(output_skip_reason="too big")
        assert rec.output_digest is None and "skipped (too big)" in rec.record_error and rec.verify()

    def test_circular_structure_does_not_raise(self):
        loop: dict = {}
        loop["self"] = loop
        rec = _build(result={"data": loop})
        assert rec.output_digest is None and rec.record_error

    def test_garbage_arguments_do_not_raise(self):
        for bad in (None, 5, "x", [object()], {1: object()}):
            rec = _build(arguments=bad)
            assert rec.verify()

    def test_empty_run_id_falls_back_to_a_record_that_says_it_failed(self):
        rec = rr.build_run_record(run_id="", tool="demo", session_id="s")
        assert rec.record_error and rec.verify()


class TestVersionAndEnvironment:
    def test_meta_version_wins_and_is_labelled(self):
        rec = _build(result={"data": {}, "meta": {"version": "9.9.9"}})
        assert (rec.tool_version, rec.version_source) == ("9.9.9", "result_meta")

    def test_falls_back_to_distribution_version_and_says_so(self):
        rec = _build(result={"data": {}})
        assert rec.version_source in ("distribution", "unavailable")
        if rec.version_source == "distribution":
            assert rec.tool_version

    def test_environment_fingerprint_is_memoised(self):
        assert rr.process_environment() is rr.process_environment()
        assert _build().env_digest == _build(run_id="other").env_digest

    def test_environment_names_tool_distributions(self):
        fingerprint, _ = rr.process_environment()
        assert "aihydro-core" in fingerprint["distributions"]


class TestEntryBinding:
    ENTRY = {"run_id": "t.1", "tool_name": "demo", "key_outputs": {"nse": 0.8}}

    def _entry_with_record(self):
        rec = _build(run_id="t.1", entry=self.ENTRY)
        return {**self.ENTRY, "record": rec.to_dict()}

    def test_untampered_row_verifies(self):
        check = rr.verify_run_log_entry(self._entry_with_record())
        assert check == {"has_record": True, "record_ok": True, "entry_ok": True, "record_error": None}

    def test_editing_key_outputs_of_a_sealed_row_is_detected(self):
        entry = self._entry_with_record()
        entry["key_outputs"]["nse"] = 0.99
        check = rr.verify_run_log_entry(entry)
        assert check["record_ok"] is True and check["entry_ok"] is False

    def test_editing_the_record_is_detected(self):
        entry = self._entry_with_record()
        entry["record"]["status"] = "error"
        assert rr.verify_run_log_entry(entry)["record_ok"] is False

    def test_legacy_row_has_no_record(self):
        assert rr.verify_run_log_entry({"nse": 0.8})["has_record"] is False
        assert rr.verify_run_log_entry("not a dict")["has_record"] is False


class TestCoverageSummary:
    def test_counts_and_problems(self):
        good = {**TestEntryBinding.ENTRY, "record": _build(run_id="t.1", entry=TestEntryBinding.ENTRY).to_dict()}
        bad_entry = {"run_id": "t.2", "tool_name": "demo", "key_outputs": {"nse": 0.5}}
        bad = {**bad_entry, "record": _build(run_id="t.2", entry=bad_entry).to_dict()}
        bad["key_outputs"]["nse"] = 0.6
        errored = {"run_id": "t.3", "record": _build(run_id="t.3", result={"data": {"f": TestNeverRaises.Abbreviated()}}).to_dict()}
        summary = rr.coverage_summary({"t.1": good, "t.2": bad, "t.3": errored, "old": {"nse": 0.1}})
        assert summary["run_log_rows"] == 4
        assert summary["v2_records"] == 3
        assert summary["legacy_unrecorded"] == 1
        assert summary["v2_verified"] == 2          # t.1 and t.3 (t.3 verifies; its error is the point)
        assert summary["record_errors"] == 1
        assert {p["run_id"] for p in summary["problems"]} == {"t.2", "t.3"}
        assert math.isclose(summary["coverage"], 0.75)

    def test_empty_log(self):
        assert rr.coverage_summary({})["coverage"] is None
