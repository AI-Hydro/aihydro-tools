"""
Tests for recording scrubbed tool inputs in run-log entries (2026-07-09
extension feature audit, F-2a).

Covers:
  - _scrub_tool_inputs: scalars kept, secrets/ctx/session_id dropped,
    long strings recorded by length only, lists/dicts dropped
  - post_run(..., inputs=...) -> _write_run_log: the "inputs" key round-trips
    into the session's _run_log entry, scrubbed
  - post_run without inputs (existing callers): no "inputs" key is added,
    proving this is additive and doesn't regress the un-migrated call sites
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from ai_hydro.mcp import enforcement
from ai_hydro.session.store import HydroSession


def _make_session(session_id: str, sessions_dir: Path) -> HydroSession:
    import ai_hydro.session.store as _store
    with patch.object(_store, "_SESSIONS_DIR", sessions_dir):
        s = HydroSession(session_id=session_id)
        s._storage_dir = sessions_dir
        s.save()
    return s


class TestScrubToolInputs(unittest.TestCase):
    def test_keeps_scalars(self):
        scrubbed = enforcement._scrub_tool_inputs(
            {"gauge_id": "01031500", "resolution": 30, "manning_n": 0.035, "create_map": True, "hindcast_date": None}
        )
        self.assertEqual(
            scrubbed,
            {"gauge_id": "01031500", "resolution": 30, "manning_n": 0.035, "create_map": True, "hindcast_date": None},
        )

    def test_drops_ctx_session_and_private_keys(self):
        scrubbed = enforcement._scrub_tool_inputs(
            {"ctx": object(), "session": object(), "session_id": "abc", "_internal": 1, "gauge_id": "01031500"}
        )
        self.assertEqual(scrubbed, {"gauge_id": "01031500"})

    def test_drops_secret_shaped_keys(self):
        scrubbed = enforcement._scrub_tool_inputs(
            {"api_key": "sk-abc", "auth_token": "tok", "db_password": "hunter2", "gee_credential": "x", "gauge_id": "01031500"}
        )
        self.assertEqual(scrubbed, {"gauge_id": "01031500"})

    def test_long_string_recorded_by_length_only(self):
        blob = "x" * 5000
        scrubbed = enforcement._scrub_tool_inputs({"geometry_geojson": blob})
        self.assertEqual(scrubbed["geometry_geojson"], "<5000 chars, omitted>")

    def test_drops_lists_and_dicts(self):
        scrubbed = enforcement._scrub_tool_inputs({"feature": ["a", "b"], "nested": {"x": 1}, "gauge_id": "01031500"})
        self.assertEqual(scrubbed, {"gauge_id": "01031500"})

    def test_empty_or_none_input(self):
        self.assertEqual(enforcement._scrub_tool_inputs(None), {})
        self.assertEqual(enforcement._scrub_tool_inputs({}), {})


class TestPostRunInputsRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.sessions_dir = Path(self._tmp.name)
        self.session_id = "test_inputs_session"
        _make_session(self.session_id, self.sessions_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_inputs_round_trip_into_run_log_entry(self):
        import ai_hydro.session.store as _store

        with patch.object(_store, "_SESSIONS_DIR", self.sessions_dir):
            result = {"data": {"kge": 0.8}, "quality_flags": []}
            result = enforcement.post_run(
                "extract_hydrological_signatures",
                self.session_id,
                result,
                inputs={"start_date": "1989-10-01", "end_date": "2009-09-30", "api_key": "should-not-appear"},
            )
            run_id = result["_run_id"]

            session = HydroSession.load(self.session_id)
            run_log = session.get("_run_log")
            entry = run_log[run_id]
            self.assertEqual(
                entry["inputs"], {"start_date": "1989-10-01", "end_date": "2009-09-30"}
            )
            self.assertNotIn("api_key", entry["inputs"])

    def test_no_inputs_kwarg_produces_no_inputs_key(self):
        """Un-migrated call sites (inputs=None, the default) must not regress."""
        import ai_hydro.session.store as _store

        with patch.object(_store, "_SESSIONS_DIR", self.sessions_dir):
            result = {"data": {"area_km2": 113.2}, "quality_flags": []}
            result = enforcement.post_run("delineate_watershed", self.session_id, result)
            run_id = result["_run_id"]

            session = HydroSession.load(self.session_id)
            run_log = session.get("_run_log")
            entry = run_log[run_id]
            self.assertNotIn("inputs", entry)


if __name__ == "__main__":
    unittest.main()
