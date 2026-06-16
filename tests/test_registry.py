"""
Tests for Phase 2.2 — Global claim registry + living claims.

Covers:
  - RegistryStore: append, all_entries, find_by_claim_id, find_by_session,
    mark_stale, mark_retracted, deduplication by registry_id
  - promote_claim_to_registry: real write, gate checks, registry_id returned,
    evidence_versions captured, session claim updated
  - check_registry_staleness: fresh → no stale, data change → stale detected,
    session claim status updated
  - list_registry_claims: unfiltered, by session, by status
  - snapshot_evidence_versions: artifact manifest, slot fallback, run spans
  - check_evidence_staleness: unchanged → empty, changed → source listed
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_hydro.registry import store as reg
from ai_hydro.mcp.tools_ledger import (
    promote_claim_to_registry,
    check_registry_staleness,
    list_registry_claims,
)
from ai_hydro.session.store import HydroSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_registry(tmp_path: Path):
    """Patch registry paths to use tmp_path."""
    claims_file = tmp_path / "claims.jsonl"
    patcher_dir = patch.object(reg, "REGISTRY_DIR", tmp_path)
    patcher_file = patch.object(reg, "CLAIMS_FILE", claims_file)
    return patcher_dir, patcher_file


def _make_session(session_id: str, sessions_dir: Path) -> HydroSession:
    import ai_hydro.session.store as _store
    with patch.object(_store, "SESSIONS_DIR", sessions_dir):
        s = HydroSession(session_id=session_id)
        s._storage_dir = sessions_dir
        s.save()
    return s


def _promoted_claim_dict(claim_id: str = "c-001") -> dict:
    """Build a valid ScientificClaim dict using the correct field names from the model."""
    return {
        "id": claim_id,
        "claim": "Q_mean for 01013500 is 0.45 m3/s",
        "claim_type": "empirical_result",
        "status": "supported",
        "confidence": "high",
        "confidence_rationale": "30-year USGS record.",
        "scope": {"basins": ["01013500"], "period": "1990-2020"},
        "evidence_spans": [
            # source_id matches the session slot name used in staleness tests
            {"source_type": "dataset", "source_id": "streamflow", "metric_ref": "q_mean"},
        ],
        "limitations": ["Single basin only"],
        "uncertainty_verified": True,
    }


# ---------------------------------------------------------------------------
# TestRegistryStore
# ---------------------------------------------------------------------------

class TestRegistryStore(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p1, self.p2 = _tmp_registry(self.tmp)
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()

    def _entry(self, rid="reg.abc.20260101.aaa111", cid="c-001", sid="s-001", status="promoted"):
        return {
            "registry_id": rid, "claim_id": cid, "session_id": sid,
            "status": status, "evidence_versions": {}, "staleness": None,
        }

    def test_append_creates_file(self):
        reg.append(self._entry())
        self.assertTrue((self.tmp / "claims.jsonl").exists())

    def test_append_idempotent_same_registry_id(self):
        reg.append(self._entry())
        reg.append(self._entry())
        self.assertEqual(len(reg.all_entries()), 1)

    def test_append_different_registry_ids(self):
        reg.append(self._entry(rid="reg.1"))
        reg.append(self._entry(rid="reg.2", cid="c-002"))
        self.assertEqual(len(reg.all_entries()), 2)

    def test_find_by_claim_id(self):
        reg.append(self._entry(rid="reg.1", cid="c-001"))
        reg.append(self._entry(rid="reg.2", cid="c-002"))
        found = reg.find_by_claim_id("c-001")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["claim_id"], "c-001")

    def test_find_by_session(self):
        reg.append(self._entry(rid="reg.1", sid="s-001"))
        reg.append(self._entry(rid="reg.2", cid="c-002", sid="s-002"))
        found = reg.find_by_session("s-001")
        self.assertEqual(len(found), 1)

    def test_mark_stale_updates_status(self):
        reg.append(self._entry(rid="reg.1"))
        result = reg.mark_stale("reg.1", stale_sources=["usgs_nwis_01013500"])
        self.assertTrue(result)
        entries = reg.all_entries()
        self.assertEqual(entries[0]["status"], "stale")
        self.assertIsNotNone(entries[0]["staleness"])

    def test_mark_stale_returns_false_for_unknown(self):
        result = reg.mark_stale("reg.nonexistent", stale_sources=["x"])
        self.assertFalse(result)

    def test_mark_retracted(self):
        reg.append(self._entry(rid="reg.1"))
        reg.mark_retracted("reg.1", reason="author withdrew")
        entries = reg.all_entries()
        self.assertEqual(entries[0]["status"], "retracted")

    def test_build_registry_id_deterministic(self):
        rid = reg.build_registry_id("session-abc", "c-001")
        self.assertTrue(rid.startswith("reg."))
        # first 8 chars of "session-abc" = "session-"
        self.assertIn("session-", rid)

    def test_all_entries_empty_when_no_file(self):
        self.assertEqual(reg.all_entries(), [])


# ---------------------------------------------------------------------------
# TestSnapshotEvidenceVersions
# ---------------------------------------------------------------------------

class TestSnapshotEvidenceVersions(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmp / "sessions"
        self.sessions_dir.mkdir()

    def _make_session_with_slot(self, session_id, slot_name, slot_data):
        import ai_hydro.session.store as _store
        with patch.object(_store, "SESSIONS_DIR", self.sessions_dir):
            s = HydroSession(session_id=session_id)
            s._storage_dir = self.sessions_dir
            s.set(slot_name, slot_data)
            s.save()
        return s

    def test_dataset_span_hashes_slot(self):
        session = self._make_session_with_slot(
            "s-snap-001", "streamflow", {"data": {"q_cms": [1.0, 2.0, 3.0], "gauge_id": "01013500"}}
        )
        spans = [{"source_type": "dataset", "source_id": "streamflow"}]
        with patch("ai_hydro.session.store.SESSIONS_DIR", self.sessions_dir):
            versions = reg.snapshot_evidence_versions(session, spans)
        self.assertIn("streamflow", versions)
        self.assertNotEqual(versions["streamflow"], "")

    def test_run_span_uses_source_id_as_version(self):
        import ai_hydro.session.store as _store
        with patch.object(_store, "SESSIONS_DIR", self.sessions_dir):
            session = HydroSession(session_id="s-snap-002")
            session._storage_dir = self.sessions_dir
            session.save()
        spans = [{"source_type": "run", "source_id": "sig.2024.abc.1234"}]
        versions = reg.snapshot_evidence_versions(session, spans)
        self.assertEqual(versions["sig.2024.abc.1234"], "sig.2024.abc.1234")

    def test_unknown_dataset_gets_empty_string(self):
        import ai_hydro.session.store as _store
        with patch.object(_store, "SESSIONS_DIR", self.sessions_dir):
            session = HydroSession(session_id="s-snap-003")
            session._storage_dir = self.sessions_dir
            session.save()
        spans = [{"source_type": "dataset", "source_id": "nonexistent_source"}]
        versions = reg.snapshot_evidence_versions(session, spans)
        self.assertEqual(versions.get("nonexistent_source", ""), "")


# ---------------------------------------------------------------------------
# TestCheckEvidenceStaleness
# ---------------------------------------------------------------------------

class TestCheckEvidenceStaleness(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmp / "sessions"
        self.sessions_dir.mkdir()

    def _session_with_data(self, session_id, slot_data):
        import ai_hydro.session.store as _store
        with patch.object(_store, "SESSIONS_DIR", self.sessions_dir):
            s = HydroSession(session_id=session_id)
            s._storage_dir = self.sessions_dir
            s.set("streamflow", slot_data)
            s.save()
        return s

    def test_unchanged_data_not_stale(self):
        data = {"data": {"q_cms": [1.0, 2.0], "gauge_id": "01013500"}}
        session = self._session_with_data("s-stale-001", data)
        spans = [{"source_type": "dataset", "source_id": "streamflow"}]
        from ai_hydro.session.store import _hash_obj
        ev_versions = {"streamflow": _hash_obj(data)}
        stale = reg.check_evidence_staleness(session, ev_versions, spans)
        self.assertEqual(stale, [])

    def test_changed_data_is_stale(self):
        data_v1 = {"data": {"q_cms": [1.0, 2.0], "gauge_id": "01013500"}}
        data_v2 = {"data": {"q_cms": [1.0, 2.0, 3.0], "gauge_id": "01013500"}}
        session = self._session_with_data("s-stale-002", data_v2)
        spans = [{"source_type": "dataset", "source_id": "streamflow"}]
        from ai_hydro.session.store import _hash_obj
        ev_versions = {"streamflow": _hash_obj(data_v1)}
        stale = reg.check_evidence_staleness(session, ev_versions, spans)
        self.assertIn("streamflow", stale)

    def test_empty_stored_hash_skipped(self):
        session = self._session_with_data("s-stale-003", {"data": {}})
        spans = [{"source_type": "dataset", "source_id": "streamflow"}]
        ev_versions = {"streamflow": ""}  # not snapshotted
        stale = reg.check_evidence_staleness(session, ev_versions, spans)
        self.assertEqual(stale, [])


# ---------------------------------------------------------------------------
# TestPromoteClaimToRegistry (integration)
# ---------------------------------------------------------------------------

class TestPromoteClaimToRegistry(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmp / "sessions"
        self.sessions_dir.mkdir()
        self.reg_dir = self.tmp / "registry"
        self.p_sessions = patch("ai_hydro.session.store.SESSIONS_DIR", self.sessions_dir)
        self.p_reg_dir = patch.object(reg, "REGISTRY_DIR", self.reg_dir)
        self.p_reg_file = patch.object(reg, "CLAIMS_FILE", self.reg_dir / "claims.jsonl")
        self.p_sessions.start()
        self.p_reg_dir.start()
        self.p_reg_file.start()

        # Build a session with a promotable claim
        import ai_hydro.session.store as _store
        with patch.object(_store, "SESSIONS_DIR", self.sessions_dir):
            self.session = HydroSession(session_id="s-promo-001")
            self.session._storage_dir = self.sessions_dir
            self.session.claims["c-001"] = _promoted_claim_dict("c-001")
            self.session.save()

    def tearDown(self):
        self.p_sessions.stop()
        self.p_reg_dir.stop()
        self.p_reg_file.stop()

    def test_promote_returns_registry_id(self):
        r = promote_claim_to_registry("s-promo-001", "c-001", researcher_approved=True)
        self.assertNotIn("error", r)
        self.assertIn("registry_id", r)
        self.assertTrue(r["registry_id"].startswith("reg."))

    def test_promote_writes_to_registry_file(self):
        promote_claim_to_registry("s-promo-001", "c-001", researcher_approved=True)
        entries = reg.all_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["claim_id"], "c-001")

    def test_promote_captures_evidence_versions(self):
        promote_claim_to_registry("s-promo-001", "c-001", researcher_approved=True)
        entry = reg.all_entries()[0]
        self.assertIn("evidence_versions", entry)

    def test_promote_updates_session_claim(self):
        promote_claim_to_registry("s-promo-001", "c-001", researcher_approved=True)
        import ai_hydro.session.store as _store
        with patch.object(_store, "SESSIONS_DIR", self.sessions_dir):
            session2 = HydroSession.load("s-promo-001")
        self.assertTrue(session2.claims["c-001"].get("promoted"))
        self.assertIn("registry_id", session2.claims["c-001"])

    def test_promote_requires_researcher_approved(self):
        r = promote_claim_to_registry("s-promo-001", "c-001", researcher_approved=False)
        self.assertIn("error", r)

    def test_promote_gate_rejects_missing_evidence(self):
        import ai_hydro.session.store as _store
        with patch.object(_store, "SESSIONS_DIR", self.sessions_dir):
            s = HydroSession.load("s-promo-001")
            # Build a valid claim dict but with empty evidence_spans
            cd = {**_promoted_claim_dict("c-002"), "evidence_spans": [], "evidence": []}
            s.claims["c-002"] = cd
            s.save()
        r = promote_claim_to_registry("s-promo-001", "c-002", researcher_approved=True)
        self.assertIn("error", r)

    def test_promote_gate_rejects_wrong_status(self):
        import ai_hydro.session.store as _store
        with patch.object(_store, "SESSIONS_DIR", self.sessions_dir):
            s = HydroSession.load("s-promo-001")
            s.claims["c-003"] = {**_promoted_claim_dict("c-003"), "status": "proposed"}
            s.save()
        r = promote_claim_to_registry("s-promo-001", "c-003", researcher_approved=True)
        self.assertIn("error", r)


# ---------------------------------------------------------------------------
# TestCheckRegistryStaleness
# ---------------------------------------------------------------------------

class TestCheckRegistryStaleness(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmp / "sessions"
        self.sessions_dir.mkdir()
        self.reg_dir = self.tmp / "registry"
        self.p_sessions = patch("ai_hydro.session.store.SESSIONS_DIR", self.sessions_dir)
        self.p_reg_dir = patch.object(reg, "REGISTRY_DIR", self.reg_dir)
        self.p_reg_file = patch.object(reg, "CLAIMS_FILE", self.reg_dir / "claims.jsonl")
        self.p_sessions.start()
        self.p_reg_dir.start()
        self.p_reg_file.start()

    def tearDown(self):
        self.p_sessions.stop()
        self.p_reg_dir.stop()
        self.p_reg_file.stop()

    def _promote_claim(self, session_id, claim_id, data=None):
        import ai_hydro.session.store as _store
        with patch.object(_store, "SESSIONS_DIR", self.sessions_dir):
            s = HydroSession(session_id=session_id)
            s._storage_dir = self.sessions_dir
            s.claims[claim_id] = _promoted_claim_dict(claim_id)
            if data:
                s.set("streamflow", data)
            s.save()
        return promote_claim_to_registry(session_id, claim_id, researcher_approved=True)

    def test_no_promoted_claims_returns_zero(self):
        import ai_hydro.session.store as _store
        with patch.object(_store, "SESSIONS_DIR", self.sessions_dir):
            s = HydroSession(session_id="s-check-empty")
            s._storage_dir = self.sessions_dir
            s.save()
        r = check_registry_staleness("s-check-empty")
        self.assertEqual(r["n_checked"], 0)
        self.assertEqual(r["n_stale"], 0)

    def test_fresh_evidence_not_stale(self):
        data = {"data": {"q_cms": [1.0, 2.0], "gauge_id": "01013500"}}
        self._promote_claim("s-check-001", "c-001", data=data)
        r = check_registry_staleness("s-check-001")
        self.assertEqual(r["n_stale"], 0)
        self.assertIn("c-001", r["fresh_claims"])

    def test_changed_evidence_detected_stale(self):
        from ai_hydro.session.store import _hash_obj
        import ai_hydro.session.store as _store

        data_v1 = {"data": {"q_cms": [1.0, 2.0], "gauge_id": "01013500"}}
        data_v2 = {"data": {"q_cms": [1.0, 2.0, 999.0], "gauge_id": "01013500"}}

        # Promote with v1
        self._promote_claim("s-check-002", "c-001", data=data_v1)

        # Update session slot to v2 (simulates new data fetch)
        with patch.object(_store, "SESSIONS_DIR", self.sessions_dir):
            s = HydroSession.load("s-check-002")
            s.set("streamflow", data_v2)
            s.save()

        r = check_registry_staleness("s-check-002")
        self.assertEqual(r["n_stale"], 1)
        self.assertEqual(r["stale_claims"][0]["claim_id"], "c-001")

    def test_stale_updates_session_claim_status(self):
        from ai_hydro.session.store import _hash_obj
        import ai_hydro.session.store as _store

        data_v1 = {"data": {"q_cms": [1.0, 2.0]}}
        data_v2 = {"data": {"q_cms": [1.0, 2.0, 3.0]}}

        self._promote_claim("s-check-003", "c-001", data=data_v1)

        with patch.object(_store, "SESSIONS_DIR", self.sessions_dir):
            s = HydroSession.load("s-check-003")
            s.set("streamflow", data_v2)
            s.save()

        check_registry_staleness("s-check-003")

        with patch.object(_store, "SESSIONS_DIR", self.sessions_dir):
            s2 = HydroSession.load("s-check-003")
        self.assertEqual(s2.claims["c-001"].get("status"), "stale")


# ---------------------------------------------------------------------------
# TestListRegistryClaims
# ---------------------------------------------------------------------------

class TestListRegistryClaims(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmp / "sessions"
        self.sessions_dir.mkdir()
        self.reg_dir = self.tmp / "registry"
        self.p_sessions = patch("ai_hydro.session.store.SESSIONS_DIR", self.sessions_dir)
        self.p_reg_dir = patch.object(reg, "REGISTRY_DIR", self.reg_dir)
        self.p_reg_file = patch.object(reg, "CLAIMS_FILE", self.reg_dir / "claims.jsonl")
        self.p_sessions.start()
        self.p_reg_dir.start()
        self.p_reg_file.start()

        # Pre-populate two entries
        reg.append({"registry_id": "reg.s1.20260101.aaa", "claim_id": "c-001", "session_id": "s-list-001",
                     "status": "promoted", "evidence_versions": {}, "staleness": None})
        reg.append({"registry_id": "reg.s2.20260101.bbb", "claim_id": "c-002", "session_id": "s-list-002",
                     "status": "stale", "evidence_versions": {}, "staleness": {}})

    def tearDown(self):
        self.p_sessions.stop()
        self.p_reg_dir.stop()
        self.p_reg_file.stop()

    def test_list_all_returns_both(self):
        r = list_registry_claims()
        self.assertEqual(r["n_entries"], 2)

    def test_list_by_session(self):
        r = list_registry_claims(session_id="s-list-001")
        self.assertEqual(r["n_entries"], 1)
        self.assertEqual(r["entries"][0]["claim_id"], "c-001")

    def test_list_by_status_stale(self):
        r = list_registry_claims(status="stale")
        self.assertEqual(r["n_entries"], 1)
        self.assertEqual(r["entries"][0]["claim_id"], "c-002")

    def test_list_n_stale_count(self):
        r = list_registry_claims()
        self.assertEqual(r["n_stale"], 1)


if __name__ == "__main__":
    unittest.main()
