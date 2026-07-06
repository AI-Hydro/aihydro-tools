"""
Session path-safety tests (Wave 0.2 of the ecosystem remediation plan).

Covers:
1. Traversal containment — hostile session_id/shard_id resolve inside
   SESSIONS_DIR (or raise), never outside.
2. Collision avoidance — distinct legitimate session_ids never map to the
   same file (the case-preserving sanitizer, not the lowercasing slugify).
3. Existing-file discoverability — sessions written before this fix (raw,
   un-sanitized filenames) remain loadable via the legacy-path fallback.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


class TestPathTraversalContainment:
    def test_hostile_session_id_stays_inside_sessions_dir(self, tmp_path):
        from ai_hydro.session.store import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
            path = HydroSession._path("../../etc/passwd")
            resolved = path.resolve()
            assert os.path.commonpath([str(resolved), str(tmp_path.resolve())]) == str(
                tmp_path.resolve()
            )

    def test_hostile_shard_id_stays_inside_sessions_dir(self, tmp_path):
        from ai_hydro.session.store import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
            path = HydroSession._path("normal-session", shard_id="../../../evil")
            resolved = path.resolve()
            assert os.path.commonpath([str(resolved), str(tmp_path.resolve())]) == str(
                tmp_path.resolve()
            )

    def test_hostile_session_id_round_trips_load_save(self, tmp_path):
        """A hostile session_id must not error, and must not touch anything
        outside SESSIONS_DIR — round-trip load/save stays contained."""
        from ai_hydro.session.store import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("../../../../tmp/pwned")
            s.notes = ["hello"]
            s.save()
            # Nothing was written outside tmp_path
            for f in tmp_path.rglob("*.json"):
                assert str(f.resolve()).startswith(str(tmp_path.resolve()))
            # And it's still loadable through the same (hostile) session_id
            s2 = HydroSession.load("../../../../tmp/pwned")
            assert s2.notes == ["hello"]


class TestNoFilenameCollision:
    def test_case_differing_ids_do_not_collide(self, tmp_path):
        from ai_hydro.session.store import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
            p1 = HydroSession._path("Foo_Bar")
            p2 = HydroSession._path("foo-bar")
            assert p1 != p2

    def test_case_differing_sessions_persist_independently(self, tmp_path):
        from ai_hydro.session.store import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s1 = HydroSession("Study_Alpha")
            s1.notes = ["alpha"]
            s1.save()
            s2 = HydroSession("study-alpha")
            s2.notes = ["beta"]
            s2.save()

            reloaded1 = HydroSession.load("Study_Alpha")
            reloaded2 = HydroSession.load("study-alpha")
            assert reloaded1.notes == ["alpha"]
            assert reloaded2.notes == ["beta"]


class TestExistingFileDiscoverability:
    def test_legacy_raw_named_file_is_still_loadable(self, tmp_path):
        """A session file written under the pre-fix raw-interpolation scheme
        (i.e. exactly f"{session_id}.json") must still be discoverable by
        HydroSession.load() after the sanitizer ships, for any session_id
        the sanitizer leaves unchanged (the common case: digits, letters,
        hyphens, underscores — gauge IDs, slugs, UUIDs)."""
        from ai_hydro.session.store import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
            # Simulate a pre-fix write: raw session_id interpolated directly.
            legacy_id = "01109000"
            raw_path = tmp_path / f"{legacy_id}.json"
            raw_path.write_text(json.dumps({
                "session_id": legacy_id,
                "notes": ["pre-existing session"],
            }))
            loaded = HydroSession.load(legacy_id)
            assert loaded.notes == ["pre-existing session"]

    def test_legacy_fallback_does_not_escape_sessions_dir(self, tmp_path):
        """The legacy-path fallback must remain containment-checked — it
        must not become a new traversal vector on the read path."""
        from ai_hydro.session.store import HydroSession
        outside = tmp_path.parent / "outside-secret.json"
        outside.write_text(json.dumps({"session_id": "x", "notes": ["secret"]}))
        try:
            with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
                legacy = HydroSession._legacy_raw_path("../outside-secret")
                assert legacy is None
        finally:
            outside.unlink(missing_ok=True)
