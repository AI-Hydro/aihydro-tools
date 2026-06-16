"""
Tests for MCP resource layer — Phase 3.2 Headless platform.

Verifies that:
  1. Knowledge resources continue to work (backward-compat)
  2. Session list resource enumerates sessions correctly
  3. Session summary resource returns expected fields
  4. Claims resource serves the full ledger as JSON
  5. Evidence board resource groups claims by status in kanban column order
  6. Experiments resource serves the _experiments slot
  7. All resources degrade gracefully (missing session → JSON error, not exception)
  8. Resources are headless-safe: no VS Code / chat binding needed
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(session_id: str, tmp_dir: Path, *, claims: dict | None = None,
                  experiments: dict | None = None,
                  site_id: str = "", site_name: str = "") -> None:
    """Write a minimal session JSON into tmp_dir so resources can load it."""
    with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_dir):
        from ai_hydro.session.store import HydroSession
        s = HydroSession(session_id)
        s.site_id = site_id
        s.site_name = site_name
        if claims:
            s.claims = claims
        if experiments:
            s.set("_experiments", experiments)
        s.save()


SAMPLE_CLAIMS = {
    "c-001": {
        "id": "c-001",
        "claim": "Runoff ratio increases under RCP 8.5.",
        "claim_type": "empirical_result",
        "status": "supported",
        "confidence": "medium",
        "evidence_spans": [{"source_type": "run", "source_id": "r-001", "metric_ref": "runoff_ratio"}],
        "created_at": "2026-06-13T10:00:00+00:00",
    },
    "c-002": {
        "id": "c-002",
        "claim": "Baseflow dominates summer low-flow.",
        "claim_type": "hypothesis",
        "status": "proposed",
        "confidence": "low",
        "evidence_spans": [],
        "created_at": "2026-06-13T11:00:00+00:00",
    },
    "c-003": {
        "id": "c-003",
        "claim": "Model over-estimates peak flows.",
        "claim_type": "negative_result",
        "status": "contradicted",
        "confidence": "high",
        "evidence_spans": [],
        "created_at": "2026-06-13T12:00:00+00:00",
    },
}

SAMPLE_EXPERIMENTS = {
    "exp-001": {
        "definition": {
            "experiment_id": "exp-001",
            "name": "Signature sweep",
            "tool": "extract_hydrological_signatures",
            "features": ["01013500", "01031500"],
            "params": {},
            "metrics": ["q_mean", "runoff_ratio"],
            "params_hash": "abc123",
            "created_at": "2026-06-13T09:00:00+00:00",
        },
        "results": {
            "experiment_id": "exp-001",
            "status": "complete",
            "run_ids": {"01013500": "r-001", "01031500": "r-002"},
            "rows": [],
            "n_success": 2,
            "n_error": 0,
        },
    }
}


# ---------------------------------------------------------------------------
# Knowledge resource backward-compat
# ---------------------------------------------------------------------------

class TestKnowledgeResources(unittest.TestCase):
    """Existing knowledge resources must still work after Phase 3.2 additions."""

    def test_knowledge_catalog_importable(self):
        from ai_hydro.mcp.resources import knowledge_catalog
        result = json.loads(knowledge_catalog())
        self.assertIn("library_cards", result)
        self.assertIn("n_library_cards", result)
        self.assertIn("uri_pattern", result)

    def test_library_card_unknown_name(self):
        from ai_hydro.mcp.resources import library_card
        result = json.loads(library_card("definitely_not_a_real_library"))
        self.assertTrue(result.get("error"))
        self.assertEqual(result.get("code"), "NOT_FOUND")


# ---------------------------------------------------------------------------
# Session list resource
# ---------------------------------------------------------------------------

class TestSessionListResource(unittest.TestCase):
    def test_empty_sessions_dir(self):
        from ai_hydro.mcp.resources import session_list
        with tempfile.TemporaryDirectory() as tmp:
            with patch("ai_hydro.session.store._SESSIONS_DIR", Path(tmp)):
                result = json.loads(session_list())
        self.assertEqual(result["n_sessions"], 0)
        self.assertIsInstance(result["sessions"], list)

    def test_lists_created_sessions(self):
        from ai_hydro.mcp.resources import session_list
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("test-gauge-01", tmp_path, site_id="01013500",
                              site_name="Piscataquis River")
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                    result = json.loads(session_list())
        self.assertEqual(result["n_sessions"], 1)
        entry = result["sessions"][0]
        self.assertEqual(entry["session_id"], "test-gauge-01")
        self.assertEqual(entry["site_id"], "01013500")
        self.assertEqual(entry["site_name"], "Piscataquis River")
        self.assertIn("uri", entry)
        self.assertIn("test-gauge-01", entry["uri"])

    def test_lists_n_claims_and_experiments(self):
        from ai_hydro.mcp.resources import session_list
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("gauge-x", tmp_path, claims=SAMPLE_CLAIMS,
                              experiments=SAMPLE_EXPERIMENTS)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                    result = json.loads(session_list())
        entry = result["sessions"][0]
        self.assertEqual(entry["n_claims"], 3)
        self.assertEqual(entry["n_experiments"], 1)


# ---------------------------------------------------------------------------
# Session summary resource
# ---------------------------------------------------------------------------

class TestSessionSummaryResource(unittest.TestCase):
    def test_missing_session_returns_error(self):
        from ai_hydro.mcp.resources import session_summary
        with tempfile.TemporaryDirectory() as tmp:
            with patch("ai_hydro.session.store.SESSIONS_DIR", Path(tmp)):
                result = json.loads(session_summary("nonexistent-session-xyz"))
        self.assertTrue(result.get("error"))
        self.assertEqual(result.get("code"), "SESSION_NOT_FOUND")

    def test_summary_fields(self):
        from ai_hydro.mcp.resources import session_summary
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("summ-001", tmp_path, claims=SAMPLE_CLAIMS)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_summary("summ-001"))
        self.assertEqual(result["session_id"], "summ-001")
        self.assertEqual(result["n_claims"], 3)
        self.assertIn("claims_by_status", result)
        self.assertIn("resources", result)

    def test_summary_resources_block_has_correct_uris(self):
        from ai_hydro.mcp.resources import session_summary
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("summ-002", tmp_path)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_summary("summ-002"))
        res = result["resources"]
        self.assertIn("summ-002", res["claims"])
        self.assertIn("summ-002", res["evidence_board"])
        self.assertIn("summ-002", res["experiments"])

    def test_claims_by_status_counts(self):
        from ai_hydro.mcp.resources import session_summary
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("summ-003", tmp_path, claims=SAMPLE_CLAIMS)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_summary("summ-003"))
        cbs = result["claims_by_status"]
        self.assertEqual(cbs.get("supported"), 1)
        self.assertEqual(cbs.get("proposed"), 1)
        self.assertEqual(cbs.get("contradicted"), 1)


# ---------------------------------------------------------------------------
# Claims ledger resource
# ---------------------------------------------------------------------------

class TestSessionClaimsResource(unittest.TestCase):
    def test_missing_session_error(self):
        from ai_hydro.mcp.resources import session_claims
        with tempfile.TemporaryDirectory() as tmp:
            with patch("ai_hydro.session.store.SESSIONS_DIR", Path(tmp)):
                result = json.loads(session_claims("no-such-session"))
        self.assertTrue(result.get("error"))

    def test_returns_all_claims(self):
        from ai_hydro.mcp.resources import session_claims
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("clm-001", tmp_path, claims=SAMPLE_CLAIMS)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_claims("clm-001"))
        self.assertEqual(result["n_claims"], 3)
        self.assertEqual(len(result["claims"]), 3)

    def test_claims_sorted_by_created_at(self):
        from ai_hydro.mcp.resources import session_claims
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("clm-002", tmp_path, claims=SAMPLE_CLAIMS)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_claims("clm-002"))
        ids = [c["id"] for c in result["claims"]]
        self.assertEqual(ids, ["c-001", "c-002", "c-003"])

    def test_claims_contain_evidence_spans(self):
        from ai_hydro.mcp.resources import session_claims
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("clm-003", tmp_path, claims=SAMPLE_CLAIMS)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_claims("clm-003"))
        supported = next(c for c in result["claims"] if c["id"] == "c-001")
        self.assertEqual(len(supported["evidence_spans"]), 1)
        self.assertEqual(supported["evidence_spans"][0]["source_id"], "r-001")

    def test_empty_session_returns_empty_list(self):
        from ai_hydro.mcp.resources import session_claims
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("clm-empty", tmp_path)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_claims("clm-empty"))
        self.assertEqual(result["n_claims"], 0)
        self.assertEqual(result["claims"], [])


# ---------------------------------------------------------------------------
# Evidence board resource
# ---------------------------------------------------------------------------

class TestSessionEvidenceBoardResource(unittest.TestCase):
    def test_missing_session_error(self):
        from ai_hydro.mcp.resources import session_evidence_board
        with tempfile.TemporaryDirectory() as tmp:
            with patch("ai_hydro.session.store.SESSIONS_DIR", Path(tmp)):
                result = json.loads(session_evidence_board("no-session"))
        self.assertTrue(result.get("error"))

    def test_columns_cover_all_known_statuses(self):
        from ai_hydro.mcp.resources import session_evidence_board
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("board-001", tmp_path, claims=SAMPLE_CLAIMS)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_evidence_board("board-001"))
        statuses = {col["status"] for col in result["columns"]}
        for expected in ("proposed", "tested", "supported", "contradicted",
                         "retracted", "stale", "weakly_supported"):
            self.assertIn(expected, statuses)

    def test_claims_land_in_correct_columns(self):
        from ai_hydro.mcp.resources import session_evidence_board
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("board-002", tmp_path, claims=SAMPLE_CLAIMS)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_evidence_board("board-002"))
        col_by_status = {col["status"]: col for col in result["columns"]}
        self.assertEqual(col_by_status["supported"]["n"], 1)
        self.assertEqual(col_by_status["proposed"]["n"], 1)
        self.assertEqual(col_by_status["contradicted"]["n"], 1)
        self.assertEqual(col_by_status["tested"]["n"], 0)

    def test_total_claim_count(self):
        from ai_hydro.mcp.resources import session_evidence_board
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("board-003", tmp_path, claims=SAMPLE_CLAIMS)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_evidence_board("board-003"))
        self.assertEqual(result["n_claims"], 3)
        self.assertEqual(result["n_unknown_status"], 0)

    def test_unknown_status_goes_to_unknown_bucket(self):
        from ai_hydro.mcp.resources import session_evidence_board
        alien_claims = {
            "c-x": {"id": "c-x", "claim": "Test.", "claim_type": "hypothesis",
                     "status": "purple_unicorn", "confidence": "low",
                     "evidence_spans": [], "created_at": "2026-06-13T00:00:00+00:00"}
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("board-004", tmp_path, claims=alien_claims)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_evidence_board("board-004"))
        self.assertEqual(result["n_unknown_status"], 1)
        self.assertEqual(result["unknown"][0]["id"], "c-x")

    def test_empty_session_all_columns_zero(self):
        from ai_hydro.mcp.resources import session_evidence_board
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("board-empty", tmp_path)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_evidence_board("board-empty"))
        total_in_cols = sum(col["n"] for col in result["columns"])
        self.assertEqual(total_in_cols, 0)
        self.assertEqual(result["n_claims"], 0)


# ---------------------------------------------------------------------------
# Experiments resource
# ---------------------------------------------------------------------------

class TestSessionExperimentsResource(unittest.TestCase):
    def test_missing_session_error(self):
        from ai_hydro.mcp.resources import session_experiments
        with tempfile.TemporaryDirectory() as tmp:
            with patch("ai_hydro.session.store.SESSIONS_DIR", Path(tmp)):
                result = json.loads(session_experiments("no-session"))
        self.assertTrue(result.get("error"))

    def test_empty_experiments(self):
        from ai_hydro.mcp.resources import session_experiments
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("exp-empty", tmp_path)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_experiments("exp-empty"))
        self.assertEqual(result["n_experiments"], 0)
        self.assertEqual(result["experiments"], [])

    def test_returns_experiments(self):
        from ai_hydro.mcp.resources import session_experiments
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("exp-001", tmp_path, experiments=SAMPLE_EXPERIMENTS)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_experiments("exp-001"))
        self.assertEqual(result["n_experiments"], 1)
        exp = result["experiments"][0]
        self.assertIn("definition", exp)
        self.assertEqual(exp["definition"]["experiment_id"], "exp-001")

    def test_experiment_has_results(self):
        from ai_hydro.mcp.resources import session_experiments
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("exp-002", tmp_path, experiments=SAMPLE_EXPERIMENTS)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_experiments("exp-002"))
        exp = result["experiments"][0]
        self.assertIn("results", exp)
        self.assertEqual(exp["results"]["status"], "complete")
        self.assertEqual(exp["results"]["n_success"], 2)


# ---------------------------------------------------------------------------
# Headless mode: no chat binding needed
# ---------------------------------------------------------------------------

class TestHeadlessMode(unittest.TestCase):
    """Resources must work without ACTIVE_CHAT_ID or VS Code injection."""

    def test_claims_resource_with_no_chat_context(self):
        """Read claims resource with only session_id — no _chat_id, no ContextVar."""
        from ai_hydro.mcp.resources import session_claims
        from ai_hydro.mcp.app import ACTIVE_CHAT_ID
        # Confirm ContextVar is None (no extension injecting _chat_id)
        self.assertIsNone(ACTIVE_CHAT_ID.get())
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("headless-001", tmp_path, claims=SAMPLE_CLAIMS)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_claims("headless-001"))
        self.assertEqual(result["n_claims"], 3)
        self.assertFalse(result.get("error", False))

    def test_evidence_board_resource_with_no_chat_context(self):
        """Evidence board readable with only session_id, no extension context."""
        from ai_hydro.mcp.resources import session_evidence_board
        from ai_hydro.mcp.app import ACTIVE_CHAT_ID
        self.assertIsNone(ACTIVE_CHAT_ID.get())
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                _make_session("headless-002", tmp_path, claims=SAMPLE_CLAIMS)
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                result = json.loads(session_evidence_board("headless-002"))
        self.assertNotIn("error", result)
        self.assertIn("columns", result)
