"""
Tests for Phase 2.1 — Fleet-scale experiment tools.

Covers:
  - define_experiment: valid definition, already_exists, unsupported tool, validation
  - run_experiment: success path (mocked), partial error, not-found
  - get_experiment_table: column/row structure, aggregates, CI columns, pending state
"""
from __future__ import annotations

import json
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ai_hydro.mcp.tools_experiments import (
    define_experiment,
    get_experiment_table,
    run_experiment,
)
from ai_hydro.session.store import HydroSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(tmp_path: Path) -> HydroSession:
    from ai_hydro.session.store import SESSIONS_DIR
    from unittest.mock import patch as _patch
    import ai_hydro.session.store as _store

    session_id = "test-exp-session-001"
    with _patch.object(_store, "SESSIONS_DIR", tmp_path):
        s = HydroSession(session_id=session_id)
        s._storage_dir = tmp_path
        s.save()
    return s


def _session_with_patch(tmp_path: Path):
    """Return (session, patcher) so caller can stop the patcher."""
    import ai_hydro.session.store as _store
    from unittest.mock import patch as _patch

    patcher = _patch.object(_store, "SESSIONS_DIR", tmp_path)
    patcher.start()
    session_id = "test-exp-session-001"
    s = HydroSession(session_id=session_id)
    s._storage_dir = tmp_path
    s.save()
    return s, patcher


def _define_with_patch(tmp_path: Path, *, name="Test Exp", tool="extract_hydrological_signatures",
                        features=None, params=None, metrics=None):
    features = features or ["basin_001", "basin_002"]
    import ai_hydro.session.store as _store
    from unittest.mock import patch as _patch

    with _patch.object(_store, "SESSIONS_DIR", tmp_path):
        s = HydroSession(session_id="test-exp-session-001")
        s._storage_dir = tmp_path
        s.save()
        return define_experiment(
            session_id="test-exp-session-001",
            name=name,
            tool=tool,
            features=features,
            params=params or {},
            metrics=metrics,
        )


# ---------------------------------------------------------------------------
# TestDefineExperiment
# ---------------------------------------------------------------------------

class TestDefineExperiment(unittest.TestCase):

    def setUp(self):
        import tempfile, ai_hydro.session.store as _store
        self.tmp = Path(tempfile.mkdtemp())
        self._patcher = patch.object(_store, "SESSIONS_DIR", self.tmp)
        self._patcher.start()
        self._sess = HydroSession(session_id="test-exp-session-001")
        self._sess._storage_dir = self.tmp
        self._sess.save()

    def tearDown(self):
        self._patcher.stop()

    def _define(self, **kwargs):
        defaults = dict(
            session_id="test-exp-session-001",
            name="My Experiment",
            tool="extract_hydrological_signatures",
            features=["b001", "b002"],
        )
        defaults.update(kwargs)
        return define_experiment(**defaults)

    def test_define_returns_experiment_id(self):
        r = self._define()
        self.assertIn("experiment_id", r)
        self.assertTrue(r["experiment_id"].startswith("exp."))

    def test_define_stores_n_features(self):
        r = self._define(features=["a", "b", "c"])
        self.assertEqual(r["n_features"], 3)

    def test_define_uses_default_metrics(self):
        r = self._define()
        self.assertIn("metrics", r)
        self.assertIn("q_mean", r["metrics"])

    def test_define_accepts_custom_metrics(self):
        r = self._define(metrics=["q_mean", "runoff_ratio"])
        self.assertEqual(r["metrics"], ["q_mean", "runoff_ratio"])

    def test_define_already_exists_same_call(self):
        r1 = self._define()
        r2 = self._define()
        self.assertEqual(r2["status"], "already_exists")
        self.assertEqual(r1["experiment_id"], r2["experiment_id"])

    def test_define_unsupported_tool(self):
        r = self._define(tool="nonexistent_tool")
        self.assertIn("error", r)
        self.assertIn("nonexistent_tool", r.get("message", r.get("error", "")))

    def test_define_empty_features_error(self):
        r = self._define(features=[])
        self.assertIn("error", r)

    def test_define_empty_name_error(self):
        r = self._define(name="  ")
        self.assertIn("error", r)

    def test_define_geomorphic_tool(self):
        r = self._define(tool="extract_geomorphic_parameters")
        self.assertNotIn("error", r)
        self.assertIn("area_km2", r["metrics"])

    def test_define_baseflow_tool(self):
        r = self._define(tool="separate_baseflow")
        self.assertNotIn("error", r)
        self.assertIn("baseflow_index", r["metrics"])

    def test_define_params_hash_stable(self):
        r1 = self._define(params={"start": "1980-01-01"})
        # Different features → different experiment_id, but params_hash same
        self._sess.set("_experiments", None)
        self._sess.save()
        r2 = self._define(features=["c001"], params={"start": "1980-01-01"})
        self.assertEqual(r1["params_hash"], r2["params_hash"])


# ---------------------------------------------------------------------------
# TestRunExperiment
# ---------------------------------------------------------------------------

def _make_sig_result(feature: str, run_id_suffix: str = "1111") -> dict:
    """Minimal extract_hydrological_signatures-style result."""
    return {
        "_run_id": f"sig.2024.{feature}.{run_id_suffix}",
        "data": {
            "q_mean": 0.5 + hash(feature) % 10 * 0.01,
            "runoff_ratio": 0.4,
            "baseflow_index": 0.3,
            "_uncertainty": {
                "q_mean": {"ci_low": 0.45, "ci_high": 0.55},
            },
        },
    }


class TestRunExperiment(unittest.TestCase):

    def setUp(self):
        import tempfile, ai_hydro.session.store as _store
        self.tmp = Path(tempfile.mkdtemp())
        self._patcher = patch.object(_store, "SESSIONS_DIR", self.tmp)
        self._patcher.start()
        self._sess = HydroSession(session_id="test-exp-session-001")
        self._sess._storage_dir = self.tmp
        self._sess.save()

        # Define an experiment to run
        r = define_experiment(
            session_id="test-exp-session-001",
            name="Run Test",
            tool="extract_hydrological_signatures",
            features=["b001", "b002", "b003"],
            metrics=["q_mean", "runoff_ratio"],
        )
        self.exp_id = r["experiment_id"]

    def tearDown(self):
        self._patcher.stop()

    def _mock_runner(self, side_effect=None):
        def runner(session_id, feature, **kw):
            if side_effect and feature in side_effect:
                raise side_effect[feature]
            return _make_sig_result(feature)
        return runner

    def test_run_complete_success(self):
        with patch("ai_hydro.mcp.tools_experiments._get_runners") as mock_runners:
            mock_runners.return_value = {"extract_hydrological_signatures": self._mock_runner()}
            r = run_experiment(session_id="test-exp-session-001", experiment_id=self.exp_id)
        self.assertEqual(r["status"], "complete")
        self.assertEqual(r["n_success"], 3)
        self.assertEqual(r["n_error"], 0)

    def test_run_stores_run_ids(self):
        with patch("ai_hydro.mcp.tools_experiments._get_runners") as mock_runners:
            mock_runners.return_value = {"extract_hydrological_signatures": self._mock_runner()}
            r = run_experiment(session_id="test-exp-session-001", experiment_id=self.exp_id)
        self.assertIn("b001", r["run_ids"])

    def test_run_partial_error(self):
        se = {"b002": RuntimeError("basin not found")}
        with patch("ai_hydro.mcp.tools_experiments._get_runners") as mock_runners:
            mock_runners.return_value = {"extract_hydrological_signatures": self._mock_runner(side_effect=se)}
            r = run_experiment(session_id="test-exp-session-001", experiment_id=self.exp_id)
        self.assertEqual(r["status"], "partial")
        self.assertEqual(r["n_error"], 1)
        self.assertIn("b002", r.get("errors", {}))

    def test_run_not_found_error(self):
        r = run_experiment(session_id="test-exp-session-001", experiment_id="exp.doesnotexist")
        self.assertIn("error", r)

    def test_run_all_fail_status_error(self):
        se = {"b001": RuntimeError("x"), "b002": RuntimeError("x"), "b003": RuntimeError("x")}
        with patch("ai_hydro.mcp.tools_experiments._get_runners") as mock_runners:
            mock_runners.return_value = {"extract_hydrological_signatures": self._mock_runner(side_effect=se)}
            r = run_experiment(session_id="test-exp-session-001", experiment_id=self.exp_id)
        self.assertEqual(r["status"], "error")


# ---------------------------------------------------------------------------
# TestGetExperimentTable
# ---------------------------------------------------------------------------

class TestGetExperimentTable(unittest.TestCase):

    def setUp(self):
        import tempfile, ai_hydro.session.store as _store
        self.tmp = Path(tempfile.mkdtemp())
        self._patcher = patch.object(_store, "SESSIONS_DIR", self.tmp)
        self._patcher.start()
        self._sess = HydroSession(session_id="test-exp-session-001")
        self._sess._storage_dir = self.tmp
        self._sess.save()

        # Define + run
        r = define_experiment(
            session_id="test-exp-session-001",
            name="Table Test",
            tool="extract_hydrological_signatures",
            features=["b001", "b002"],
            metrics=["q_mean", "runoff_ratio"],
        )
        self.exp_id = r["experiment_id"]

        def runner(session_id, feature, **kw):
            return _make_sig_result(feature)

        with patch("ai_hydro.mcp.tools_experiments._get_runners") as mock_runners:
            mock_runners.return_value = {"extract_hydrological_signatures": runner}
            run_experiment(session_id="test-exp-session-001", experiment_id=self.exp_id)

    def tearDown(self):
        self._patcher.stop()

    def test_table_has_correct_columns(self):
        r = get_experiment_table(session_id="test-exp-session-001", experiment_id=self.exp_id)
        self.assertIn("feature_id", r["columns"])
        self.assertIn("q_mean", r["columns"])
        self.assertIn("runoff_ratio", r["columns"])
        self.assertIn("run_id", r["columns"])

    def test_table_has_ci_columns(self):
        r = get_experiment_table(session_id="test-exp-session-001", experiment_id=self.exp_id)
        self.assertIn("q_mean_ci_low", r["columns"])
        self.assertIn("q_mean_ci_high", r["columns"])

    def test_table_row_count(self):
        r = get_experiment_table(session_id="test-exp-session-001", experiment_id=self.exp_id)
        self.assertEqual(r["n_rows"], 2)

    def test_table_aggregate_stats(self):
        r = get_experiment_table(session_id="test-exp-session-001", experiment_id=self.exp_id)
        agg = r["aggregate_stats"]
        self.assertIn("q_mean", agg)
        self.assertIn("mean", agg["q_mean"])
        self.assertIn("n", agg["q_mean"])
        self.assertEqual(agg["q_mean"]["n"], 2)

    def test_table_rows_have_feature_id(self):
        r = get_experiment_table(session_id="test-exp-session-001", experiment_id=self.exp_id)
        fids = {row["feature_id"] for row in r["rows"]}
        self.assertIn("b001", fids)
        self.assertIn("b002", fids)

    def test_table_rows_have_run_id(self):
        r = get_experiment_table(session_id="test-exp-session-001", experiment_id=self.exp_id)
        for row in r["rows"]:
            self.assertIsNotNone(row.get("run_id"))

    def test_table_note_contains_cite_hint(self):
        r = get_experiment_table(session_id="test-exp-session-001", experiment_id=self.exp_id)
        self.assertIn("source_id", r["note"])

    def test_table_pending_state(self):
        r2 = define_experiment(
            session_id="test-exp-session-001",
            name="Pending Exp",
            tool="separate_baseflow",
            features=["x001"],
            metrics=["baseflow_index"],
        )
        t = get_experiment_table(session_id="test-exp-session-001", experiment_id=r2["experiment_id"])
        self.assertIn("status", t)
        self.assertIn(t["status"], ("pending",))

    def test_table_not_found_error(self):
        r = get_experiment_table(session_id="test-exp-session-001", experiment_id="exp.nope")
        self.assertIn("error", r)

    def test_table_n_with_ci(self):
        r = get_experiment_table(session_id="test-exp-session-001", experiment_id=self.exp_id)
        # Both features have q_mean CI, runoff_ratio has no CI → n_with_ci >= 2
        self.assertGreaterEqual(r["n_with_ci"], 2)


if __name__ == "__main__":
    unittest.main()
