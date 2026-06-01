"""
C1 contract tests for HydroSession — three-level slot model and Store Protocol.

This test file verifies the invariants introduced in C1 of the aihydro-core
build.  It covers:

1.  Store Protocol conformance — HydroSession satisfies isinstance(s, Store)
2.  Old session file migration — single-value slots are migrated losslessly
    to the three-level v2 format on first load
3.  Multi-feature non-collision — the trigger scenario (TWI on ann1, then
    ann2, no clear_session needed)
4.  Backward-compatible set()/get() and property accessors
5.  record_result() with and without an explicit feature_id
6.  commit() delegates to save()
7.  synopsis_for_llm() — flat for single-feature, nested for multi-feature
8.  Feature registry (put_feature / get_feature / list_features)
9.  computed() / pending() with the new slot model
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session(tmp_path: Path, session_id: str = "test"):
    """Load (or create) a HydroSession isolated to tmp_path."""
    from ai_hydro.session import HydroSession
    return HydroSession.load(session_id)


def _make_result(value: float, tool: str = "compute_twi") -> dict:
    return {
        "data": {"mean_twi": value, "resolution": 30},
        "meta": {"tool": tool, "computed_at": "2026-06-01T10:00:00+00:00"},
    }


# ---------------------------------------------------------------------------
# 1. Store Protocol conformance
# ---------------------------------------------------------------------------

class TestStoreProtocolConformance:
    """HydroSession must satisfy the Store Protocol at runtime."""

    def test_isinstance_store(self, tmp_path):
        from ai_hydro.session import HydroSession
        from aihydro_core.store import Store
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("proto-check")
            assert isinstance(s, Store), (
                "HydroSession must satisfy the Store Protocol (runtime_checkable)"
            )

    def test_all_store_methods_present(self, tmp_path):
        """Every method in the Store Protocol must exist on HydroSession."""
        from ai_hydro.session import HydroSession
        from aihydro_core.store import Store
        required = [
            "put_feature", "get_feature", "list_features",
            "get_active_feature_id", "set_active_feature_id",
            "put_result", "get_result", "list_results",
            "store_artifact", "add_citations", "commit",
        ]
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("method-check")
            for method in required:
                assert hasattr(s, method), f"HydroSession missing Store method: {method}"


# ---------------------------------------------------------------------------
# 2. Old session file migration
# ---------------------------------------------------------------------------

class TestOldSessionMigration:
    """Pre-C1 session files (no _hydro_slots_v2 key) migrate losslessly."""

    def _write_old_session(self, path: Path, session_id: str) -> None:
        """Write a session file in the pre-C1 single-value slot format."""
        old = {
            "session_id": session_id,
            "site_name": "Test Site",
            "site_id": "01031500",
            "site_type": "usgs_gauge",
            "workspace_dir": None,
            "working_geometry_path": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "archived": False,
            "notes": ["note 1"],
            "interpretation": "",
            "interpretation_at": None,
            "_citations": ["usgs_nwis"],
            "artifact_manifest": {},
            "claims": {},
            "assumptions": {},
            "_site_name_history": [],
            # Old slot format: direct value (no feature_id / params_key nesting)
            "watershed": {"data": {"area_km2": 1200.0, "gauge_name": "Test Gauge"}, "meta": {}},
            "streamflow": {"data": {"n_days": 3652, "q_mean_cms": 45.2}, "meta": {}},
            "signatures": None,   # Explicitly null
        }
        path.write_text(json.dumps(old, indent=2))

    def test_old_session_loads_without_error(self, tmp_path):
        from ai_hydro.session import HydroSession
        self._write_old_session(tmp_path / "old-migrate.json", "old-migrate")
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession.load("old-migrate")
            assert s.session_id == "old-migrate"

    def test_old_session_slot_values_preserved(self, tmp_path):
        from ai_hydro.session import HydroSession
        self._write_old_session(tmp_path / "old-values.json", "old-values")
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession.load("old-values")
            assert s.watershed is not None
            assert s.watershed["data"]["area_km2"] == 1200.0
            assert s.streamflow is not None
            assert s.streamflow["data"]["n_days"] == 3652

    def test_old_session_null_slot_returns_none(self, tmp_path):
        from ai_hydro.session import HydroSession
        self._write_old_session(tmp_path / "old-null.json", "old-null")
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession.load("old-null")
            assert s.signatures is None

    def test_old_session_resaves_as_v2(self, tmp_path):
        """Re-saving an old-format session must produce _hydro_slots_v2: true."""
        from ai_hydro.session import HydroSession
        self._write_old_session(tmp_path / "old-resave.json", "old-resave")
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession.load("old-resave")
            s.save()
            raw = json.loads((tmp_path / "old-resave.json").read_text())
            assert raw.get("_hydro_slots_v2") is True

    def test_old_session_v2_slot_structure_after_resave(self, tmp_path):
        """After resave, the watershed slot must use the three-level structure."""
        from ai_hydro.session import HydroSession
        self._write_old_session(tmp_path / "old-struct.json", "old-struct")
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession.load("old-struct")
            s.save()
            raw = json.loads((tmp_path / "old-struct.json").read_text())
            # Must be: watershed → __legacy__ → "" → {data, meta}
            assert "__legacy__" in raw["watershed"]
            result = raw["watershed"]["__legacy__"][""]
            assert result["data"]["area_km2"] == 1200.0

    def test_old_session_metadata_preserved(self, tmp_path):
        from ai_hydro.session import HydroSession
        self._write_old_session(tmp_path / "old-meta.json", "old-meta")
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession.load("old-meta")
            assert s.site_name == "Test Site"
            assert s.site_id == "01031500"
            assert "usgs_nwis" in s.get_citations()
            assert "note 1" in s.notes


# ---------------------------------------------------------------------------
# 3. Multi-feature non-collision — the trigger scenario
# ---------------------------------------------------------------------------

class TestMultiFeatureNonCollision:
    """The trigger scenario: TWI for ann1 and ann2 must never collide."""

    def test_two_features_same_product_independent(self, tmp_path):
        """
        Put TWI results for two different annotation features.
        Reading ann1's TWI must never return ann2's value, and vice versa.
        """
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("trigger-scenario")
            result_ann1 = _make_result(8.2)
            result_ann2 = _make_result(6.1)

            s.put_result("twi", "ann1", "params_30m", result_ann1)
            s.put_result("twi", "ann2", "params_30m", result_ann2)

            assert s.get_result("twi", "ann1", "params_30m")["data"]["mean_twi"] == 8.2
            assert s.get_result("twi", "ann2", "params_30m")["data"]["mean_twi"] == 6.1
            # Cross-check: ann1's result is not accessible under ann2
            assert s.get_result("twi", "ann1", "params_30m") is not s.get_result("twi", "ann2", "params_30m")

    def test_two_features_persist_across_save_load(self, tmp_path):
        """TWI results for two features must survive a save/load round-trip."""
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("roundtrip-features")
            s.put_result("twi", "ann1", "p30", _make_result(8.2))
            s.put_result("twi", "ann2", "p30", _make_result(6.1))
            s.save()

            s2 = HydroSession.load("roundtrip-features")
            assert s2.get_result("twi", "ann1", "p30")["data"]["mean_twi"] == 8.2
            assert s2.get_result("twi", "ann2", "p30")["data"]["mean_twi"] == 6.1

    def test_cache_miss_for_unknown_feature(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("cache-miss")
            s.put_result("twi", "ann1", "p30", _make_result(8.2))
            # ann99 has no result
            assert s.get_result("twi", "ann99", "p30") is None

    def test_cache_miss_for_unknown_params_key(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("cache-params")
            s.put_result("twi", "ann1", "p30", _make_result(8.2))
            # ann1 at 60m has no result
            assert s.get_result("twi", "ann1", "p60") is None

    def test_list_results_shows_all_features(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("list-results")
            s.put_result("twi", "ann1", "p30", _make_result(8.2))
            s.put_result("twi", "ann2", "p30", _make_result(6.1))
            s.put_result("twi", "ann1", "p60", _make_result(8.0))

            listing = s.list_results("twi")
            assert "ann1" in listing
            assert "ann2" in listing
            assert set(listing["ann1"]) == {"p30", "p60"}
            assert listing["ann2"] == ["p30"]

    def test_no_clear_session_needed_for_second_feature(self, tmp_path):
        """
        Simulate the agent workflow: compute TWI for ann1, then ann2.
        The old single-slot model required clear_session.
        With C1 this must work without clearing anything.
        """
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("no-clear-needed")

            # Step 1: compute TWI for annotation 1
            s.put_result("twi", "ann1", "params_30m", _make_result(8.2))

            # Step 2: switch active feature to ann2
            s.set_active_feature_id("ann2")

            # Step 3: compute TWI for ann2 (no clear_session!)
            s.put_result("twi", "ann2", "params_30m", _make_result(6.1))

            # Both results are independently accessible
            assert s.get_result("twi", "ann1", "params_30m")["data"]["mean_twi"] == 8.2
            assert s.get_result("twi", "ann2", "params_30m")["data"]["mean_twi"] == 6.1


# ---------------------------------------------------------------------------
# 4. Backward-compatible set() / get() and property accessors
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """Old-style set()/get() and property setters/getters must keep working."""

    def test_property_setter_stores_under_legacy(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("compat-setter")
            s.twi = _make_result(9.0)
            # Stored under __legacy__ sentinel
            assert s._slots["twi"]["__legacy__"][""] is not None

    def test_property_getter_returns_legacy_result(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("compat-getter")
            s.twi = _make_result(9.0)
            assert s.twi["data"]["mean_twi"] == 9.0

    def test_get_returns_none_for_missing_slot(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("compat-none")
            assert s.get("nonexistent") is None
            assert s.twi is None

    def test_active_feature_preferred_over_legacy(self, tmp_path):
        """
        If an active feature has a result, get() returns it over the __legacy__
        result.
        """
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("compat-active-pref")
            # Old tool wrote to __legacy__
            s.twi = _make_result(5.0)
            # New tool wrote for ann1 (real feature id)
            s.put_result("twi", "ann1", "p30", _make_result(9.0))
            s.set_active_feature_id("ann1")

            # get() should now return ann1's result
            assert s.twi["data"]["mean_twi"] == 9.0

    def test_set_none_clears_legacy_slot(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("compat-clear")
            s.twi = _make_result(9.0)
            s.twi = None
            assert s.twi is None

    def test_all_nine_common_slot_properties(self, tmp_path):
        """All 9 built-in property setters/getters must work."""
        from ai_hydro.session import HydroSession
        slots = ["watershed", "streamflow", "signatures", "geomorphic",
                 "camels", "forcing", "twi", "cn", "model"]
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("compat-all-slots")
            for slot in slots:
                val = {"data": {f"val_{slot}": 1.0}, "meta": {}}
                setattr(s, slot, val)
                got = getattr(s, slot)
                assert got is not None, f"Property '{slot}' returned None after set"
                assert got["data"][f"val_{slot}"] == 1.0


# ---------------------------------------------------------------------------
# 5. record_result() with and without feature_id
# ---------------------------------------------------------------------------

class TestRecordResult:
    """record_result() must route correctly in both old and new modes."""

    def test_record_result_no_feature_uses_legacy(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("record-legacy")
            s.record_result("twi", {"mean_twi": 7.0}, tool_name="compute_twi")
            val = s._slots["twi"]["__legacy__"][""]
            assert val is not None
            assert val["data"]["mean_twi"] == 7.0

    def test_record_result_with_feature_id(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("record-feature")
            s.record_result("twi", {"mean_twi": 8.5}, tool_name="compute_twi",
                            feature_id="ann1")
            val = s._slots["twi"]["ann1"][""]
            assert val["data"]["mean_twi"] == 8.5

    def test_record_result_active_feature_used_if_set(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("record-active")
            s.set_active_feature_id("ann2")
            s.record_result("twi", {"mean_twi": 6.8}, tool_name="compute_twi")
            val = s._slots["twi"]["ann2"][""]
            assert val["data"]["mean_twi"] == 6.8


# ---------------------------------------------------------------------------
# 6. commit() delegates to save()
# ---------------------------------------------------------------------------

class TestCommit:
    def test_commit_calls_save(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("commit-test")
            s.twi = _make_result(5.5)
            s.commit()
            # File must exist after commit
            assert (tmp_path / "commit-test.json").exists()

    def test_commit_roundtrip(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("commit-rt")
            s.twi = _make_result(5.5)
            s.commit()
            s2 = HydroSession.load("commit-rt")
            assert s2.twi["data"]["mean_twi"] == 5.5


# ---------------------------------------------------------------------------
# 7. synopsis_for_llm()
# ---------------------------------------------------------------------------

class TestSynopsisForLlm:
    def test_single_feature_is_flat(self, tmp_path):
        """Single-feature (legacy) sessions return a flat per-slot dict."""
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("synopsis-flat")
            s.twi = {
                "data": {"mean_twi": 8.2},
                "meta": {"tool": "compute_twi", "computed_at": "2026-06-01T10:00:00+00:00"},
            }
            synopsis = s.synopsis_for_llm()
            # Flat: synopsis["twi"]["mean_twi"] == 8.2
            assert "twi" in synopsis
            assert "mean_twi" in synopsis["twi"]
            assert synopsis["twi"]["mean_twi"] == 8.2

    def test_multi_feature_is_nested(self, tmp_path):
        """Multi-feature sessions return synopsis nested by feature name/id."""
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("synopsis-nested")
            s.put_result("twi", "ann1", "p30", {
                "data": {"mean_twi": 8.2},
                "meta": {"tool": "compute_twi", "computed_at": "2026-06-01T10:00:00+00:00"},
            })
            s.put_result("twi", "ann2", "p30", {
                "data": {"mean_twi": 6.1},
                "meta": {"tool": "compute_twi", "computed_at": "2026-06-01T11:00:00+00:00"},
            })
            synopsis = s.synopsis_for_llm()
            assert "twi" in synopsis
            twi = synopsis["twi"]
            # Should be nested by feature
            assert isinstance(twi, dict)
            assert len(twi) == 2


# ---------------------------------------------------------------------------
# 8. Feature registry
# ---------------------------------------------------------------------------

class TestFeatureRegistry:
    def test_put_and_get_feature(self, tmp_path):
        from ai_hydro.session import HydroSession
        from aihydro_core.primitives import Feature
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("reg-get")
            feat = Feature(
                feature_id="ann1",
                geojson={"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
                name="Annotation 1",
                source="map_annotation",
            )
            s.put_feature(feat)
            out = s.get_feature("ann1")
            assert out is not None
            assert out.feature_id == "ann1"
            assert out.name == "Annotation 1"

    def test_get_missing_feature_returns_none(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("reg-miss")
            assert s.get_feature("nonexistent") is None

    def test_list_features(self, tmp_path):
        from ai_hydro.session import HydroSession
        from aihydro_core.primitives import Feature
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("reg-list")
            s.put_feature(Feature(
                feature_id="ann1",
                geojson={"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
                name="Ann1",
            ))
            s.put_feature(Feature(
                feature_id="ann2",
                geojson={"type": "Polygon", "coordinates": [[[2, 2], [3, 2], [3, 3], [2, 2]]]},
                name="Ann2",
            ))
            ids = [f.feature_id for f in s.list_features()]
            assert "ann1" in ids
            assert "ann2" in ids

    def test_feature_registry_persists(self, tmp_path):
        from ai_hydro.session import HydroSession
        from aihydro_core.primitives import Feature
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("reg-persist")
            s.put_feature(Feature(
                feature_id="ann1",
                geojson={"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
                name="Persist Test",
            ))
            s.set_active_feature_id("ann1")
            s.save()

            s2 = HydroSession.load("reg-persist")
            f = s2.get_feature("ann1")
            assert f is not None
            assert f.name == "Persist Test"
            assert s2.get_active_feature_id() == "ann1"


# ---------------------------------------------------------------------------
# 9. computed() / pending() with three-level slots
# ---------------------------------------------------------------------------

class TestComputedPending:
    def test_computed_includes_all_feature_slots(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("comp-test")
            s.put_result("twi", "ann1", "p30", _make_result(8.2))
            s.put_result("twi", "ann2", "p30", _make_result(6.1))
            comp = s.computed()
            assert "twi" in comp

    def test_computed_excludes_all_none_products(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("comp-none")
            # Explicitly store None under a product
            s.put_result("twi", "ann1", "p30", None)
            assert "twi" not in s.computed()

    def test_pending_omits_computed_legacy_slots(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("pend-test")
            s.watershed = {"data": {"area_km2": 100}, "meta": {}}
            pending = s.pending()
            assert "watershed" not in pending
            assert "streamflow" in pending
