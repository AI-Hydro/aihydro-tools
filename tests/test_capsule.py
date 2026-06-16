"""
Unit tests for ai_hydro/capsule/ (manifest build + verify + live cross-check)
and for the export_session capsule artefacts (run_log.json, capsule_manifest.json,
replay.py).

All tests are fixture-only: no network, no real sessions in ~/.aihydro.
Session storage is redirected to tmp_path.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai_hydro.capsule.manifest import (
    MANIFEST_FILE,
    TOLERANCE_DEFAULT,
    build_manifest,
    verify_live,
    verify_manifest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    """Write {relative_path: bytes} into root."""
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _make_session(session_id: str, slots: dict, run_log: dict, monkeypatch, sessions_dir: Path):
    """Build a minimal HydroSession in tmp sessions_dir."""
    import ai_hydro.session.store as _store
    from ai_hydro.session.store import HydroSession

    monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

    session = HydroSession(session_id)
    for slot_key, slot_data in slots.items():
        session.set(slot_key, {"data": slot_data, "meta": {"tool": slot_key, "params": {}}})
    session.set("_run_log", run_log)
    session.save()
    return session


# ---------------------------------------------------------------------------
# build_manifest
# ---------------------------------------------------------------------------

class TestBuildManifest:
    def test_returns_all_files(self, tmp_path):
        _write_tree(tmp_path, {
            "session.json": b'{"a":1}',
            "run_log.json": b'{}',
            "data/flow.csv": b"date,q\n2000-01-01,1.2",
        })
        m = build_manifest(tmp_path)
        assert m["n_files"] == 3
        paths = {e["path"] for e in m["files"]}
        assert "session.json" in paths
        assert "run_log.json" in paths
        assert "data/flow.csv" in paths

    def test_skips_manifest_and_replay(self, tmp_path):
        _write_tree(tmp_path, {
            "session.json": b"{}",
            MANIFEST_FILE: b"{}",
            "replay.py": b"print('hi')",
        })
        m = build_manifest(tmp_path)
        paths = {e["path"] for e in m["files"]}
        assert MANIFEST_FILE not in paths
        assert "replay.py" not in paths
        assert "session.json" in paths

    def test_sha256_correct(self, tmp_path):
        content = b"test content for hashing"
        (tmp_path / "f.txt").write_bytes(content)
        m = build_manifest(tmp_path)
        expected = hashlib.sha256(content).hexdigest()
        assert m["files"][0]["sha256"] == expected

    def test_size_recorded(self, tmp_path):
        data = b"x" * 512
        (tmp_path / "a.bin").write_bytes(data)
        m = build_manifest(tmp_path)
        assert m["files"][0]["size"] == 512

    def test_lexicographic_ordering(self, tmp_path):
        _write_tree(tmp_path, {"c.txt": b"c", "a.txt": b"a", "b.txt": b"b"})
        m = build_manifest(tmp_path)
        paths = [e["path"] for e in m["files"]]
        assert paths == sorted(paths)

    def test_empty_dir(self, tmp_path):
        m = build_manifest(tmp_path)
        assert m["n_files"] == 0
        assert m["files"] == []

    def test_posix_paths_in_manifest(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "file.txt").write_bytes(b"data")
        m = build_manifest(tmp_path)
        assert "/" in m["files"][0]["path"]  # posix separator


# ---------------------------------------------------------------------------
# verify_manifest
# ---------------------------------------------------------------------------

class TestVerifyManifest:
    def _build_and_write_manifest(self, capsule_dir: Path) -> dict:
        m = build_manifest(capsule_dir)
        (capsule_dir / MANIFEST_FILE).write_text(json.dumps(m))
        return m

    def test_passes_on_unchanged(self, tmp_path):
        _write_tree(tmp_path, {"session.json": b'{"a":1}', "methods.md": b"# M"})
        self._build_and_write_manifest(tmp_path)
        ok, results = verify_manifest(tmp_path)
        assert ok
        assert all(r["status"] == "pass" for r in results)

    def test_fails_on_modified_file(self, tmp_path):
        _write_tree(tmp_path, {"session.json": b'{"a":1}'})
        self._build_and_write_manifest(tmp_path)
        # Tamper after manifest was written
        (tmp_path / "session.json").write_bytes(b'{"a":2}')
        ok, results = verify_manifest(tmp_path)
        assert not ok
        assert results[0]["status"] == "fail"

    def test_fails_on_missing_file(self, tmp_path):
        _write_tree(tmp_path, {"session.json": b"{}", "methods.md": b"# M"})
        self._build_and_write_manifest(tmp_path)
        (tmp_path / "methods.md").unlink()
        ok, results = verify_manifest(tmp_path)
        assert not ok
        missing = [r for r in results if r["status"] == "missing"]
        assert len(missing) == 1

    def test_fails_when_manifest_absent(self, tmp_path):
        ok, results = verify_manifest(tmp_path)
        assert not ok
        assert results[0]["path"] == MANIFEST_FILE

    def test_passes_with_subdirs(self, tmp_path):
        _write_tree(tmp_path, {
            "README.md": b"# R",
            "data/flow.csv": b"q,p",
            "figures/plot.png": b"\x89PNG",
        })
        self._build_and_write_manifest(tmp_path)
        ok, _ = verify_manifest(tmp_path)
        assert ok


# ---------------------------------------------------------------------------
# verify_live
# ---------------------------------------------------------------------------

class TestVerifyLive:
    def _capsule_with_run_log(self, tmp_path: Path, run_log: dict, session: dict) -> Path:
        (tmp_path / "run_log.json").write_text(json.dumps(run_log))
        (tmp_path / "session.json").write_text(json.dumps(session))
        return tmp_path

    def test_passes_exact_match(self, tmp_path):
        run_log = {
            "sigs.20260610.sess.ab12": {
                "tool_name": "extract_hydrological_signatures",
                "key_outputs": {"q_mean": 1.25, "flow_variability": 2.10},
            }
        }
        session = {"slots": {"signatures": {"data": {"q_mean": 1.25, "flow_variability": 2.10}}}}
        self._capsule_with_run_log(tmp_path, run_log, session)
        ok, results = verify_live(tmp_path)
        assert ok
        assert all(r["status"] == "pass" for r in results)

    def test_passes_within_tolerance(self, tmp_path):
        run_log = {
            "sigs.20260610.sess.ab12": {
                "tool_name": "extract_hydrological_signatures",
                "key_outputs": {"q_mean": 1.00},
            }
        }
        session = {"slots": {"signatures": {"data": {"q_mean": 1.005}}}}  # 0.5% deviation
        self._capsule_with_run_log(tmp_path, run_log, session)
        ok, results = verify_live(tmp_path, tolerance=0.01)
        assert ok

    def test_fails_exceeds_tolerance(self, tmp_path):
        run_log = {
            "sigs.20260610.sess.ab12": {
                "tool_name": "extract_hydrological_signatures",
                "key_outputs": {"q_mean": 1.00},
            }
        }
        session = {"slots": {"signatures": {"data": {"q_mean": 1.05}}}}  # 5% deviation
        self._capsule_with_run_log(tmp_path, run_log, session)
        ok, results = verify_live(tmp_path, tolerance=0.01)
        assert not ok
        assert results[0]["status"] == "fail"

    def test_skips_private_keys(self, tmp_path):
        run_log = {
            "sigs.20260610.sess.ab12": {
                "tool_name": "extract_hydrological_signatures",
                "key_outputs": {"_quality_flags": [{"validator": "x", "status": "pass"}]},
            }
        }
        session = {"slots": {"signatures": {"data": {}}}}
        self._capsule_with_run_log(tmp_path, run_log, session)
        ok, results = verify_live(tmp_path)
        assert ok
        assert results == []

    def test_skips_non_numeric(self, tmp_path):
        run_log = {
            "sigs.20260610.sess.ab12": {
                "tool_name": "extract_hydrological_signatures",
                "key_outputs": {"label": "humid"},
            }
        }
        session = {"slots": {"signatures": {"data": {"label": "humid"}}}}
        self._capsule_with_run_log(tmp_path, run_log, session)
        ok, results = verify_live(tmp_path)
        assert ok
        assert results == []

    def test_returns_true_when_files_missing(self, tmp_path):
        ok, results = verify_live(tmp_path)
        assert ok
        assert results == []

    def test_tolerance_default_is_one_percent(self):
        assert TOLERANCE_DEFAULT == 0.01


# ---------------------------------------------------------------------------
# export_session integration: artefacts written correctly
# ---------------------------------------------------------------------------

class TestExportSessionCapsule:
    def test_capsule_artefacts_present(self, tmp_path, monkeypatch):
        import ai_hydro.mcp.tools_session as _ts
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        capsule_root = tmp_path / "capsules"
        capsule_root.mkdir()

        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-capsule-01"
        session = HydroSession(sid)
        session.set("_run_log", {
            "sigs.20260610.test.ab12": {
                "run_id": "sigs.20260610.test.ab12",
                "tool_name": "extract_hydrological_signatures",
                "session_id": sid,
                "timestamp": "2026-06-10T00:00:00+00:00",
                "key_outputs": {"q_mean": 1.25},
            }
        })
        session.set("signatures", {
            "data": {"q_mean": 1.25, "q_median": 0.90},
            "meta": {"tool": "extract_hydrological_signatures", "params": {}, "computed_at": "2026-06-10"},
        })
        session.save()

        capsule_path = str(capsule_root / f"capsule_{sid}")
        result = _ts.export_session(session_id=sid, capsule_path=capsule_path)

        assert "error" not in result, result
        cap = Path(result["capsule_dir"])

        assert (cap / "run_log.json").exists(), "run_log.json missing"
        assert (cap / MANIFEST_FILE).exists(), "capsule_manifest.json missing"
        assert (cap / "replay.py").exists(), "replay.py missing"
        assert (cap / "session.json").exists(), "session.json missing"
        assert (cap / "environment.yml").exists(), "environment.yml missing"

    def test_run_log_json_valid(self, tmp_path, monkeypatch):
        import ai_hydro.mcp.tools_session as _ts
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        capsule_root = tmp_path / "capsules"
        capsule_root.mkdir()
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-capsule-02"
        session = HydroSession(sid)
        run_log = {
            "q.20260610.test.cd34": {
                "run_id": "q.20260610.test.cd34",
                "tool_name": "fetch_streamflow_data",
                "session_id": sid,
                "timestamp": "2026-06-10T01:00:00+00:00",
                "key_outputs": {"n_days": 3650, "mean_q_cfs": 120.5},
            }
        }
        session.set("_run_log", run_log)
        session.save()

        capsule_path = str(capsule_root / f"capsule_{sid}")
        result = _ts.export_session(session_id=sid, capsule_path=capsule_path)
        assert "error" not in result

        cap = Path(result["capsule_dir"])
        stored = json.loads((cap / "run_log.json").read_text())
        assert "q.20260610.test.cd34" in stored
        assert stored["q.20260610.test.cd34"]["key_outputs"]["n_days"] == 3650

    def test_manifest_covers_session_json(self, tmp_path, monkeypatch):
        import ai_hydro.mcp.tools_session as _ts
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        capsule_root = tmp_path / "capsules"
        capsule_root.mkdir()
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-capsule-03"
        session = HydroSession(sid)
        session.save()

        capsule_path = str(capsule_root / f"capsule_{sid}")
        result = _ts.export_session(session_id=sid, capsule_path=capsule_path)
        assert "error" not in result

        cap = Path(result["capsule_dir"])
        manifest = json.loads((cap / MANIFEST_FILE).read_text())
        paths = {e["path"] for e in manifest["files"]}
        assert "session.json" in paths
        assert result["n_manifest_entries"] == manifest["n_files"]

    def test_manifest_verifies_clean(self, tmp_path, monkeypatch):
        import ai_hydro.mcp.tools_session as _ts
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        capsule_root = tmp_path / "capsules"
        capsule_root.mkdir()
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-capsule-04"
        session = HydroSession(sid)
        session.save()

        capsule_path = str(capsule_root / f"capsule_{sid}")
        result = _ts.export_session(session_id=sid, capsule_path=capsule_path)
        assert "error" not in result

        ok, results = verify_manifest(Path(result["capsule_dir"]))
        assert ok, [r for r in results if r["status"] != "pass"]

    def test_manifest_fails_after_tampering(self, tmp_path, monkeypatch):
        import ai_hydro.mcp.tools_session as _ts
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        capsule_root = tmp_path / "capsules"
        capsule_root.mkdir()
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-capsule-05"
        session = HydroSession(sid)
        session.save()

        capsule_path = str(capsule_root / f"capsule_{sid}")
        result = _ts.export_session(session_id=sid, capsule_path=capsule_path)
        cap = Path(result["capsule_dir"])

        # Tamper with session.json after the manifest was written
        (cap / "session.json").write_bytes(b'{"tampered":true}')

        ok, results = verify_manifest(cap)
        assert not ok
        failed = [r for r in results if r["status"] == "fail"]
        assert any("session.json" in r["path"] for r in failed)


# ---------------------------------------------------------------------------
# replay.py subprocess: exit codes
# ---------------------------------------------------------------------------

class TestReplayScript:
    def _build_capsule(self, tmp_path: Path, monkeypatch) -> Path:
        import ai_hydro.mcp.tools_session as _ts
        import ai_hydro.session.store as _store
        from ai_hydro.session.store import HydroSession

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        capsule_root = tmp_path / "capsules"
        capsule_root.mkdir()
        monkeypatch.setattr(_store, "SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions_dir)

        sid = "test-replay-01"
        session = HydroSession(sid)
        session.set("_run_log", {
            "sigs.20260610.test.ef56": {
                "run_id": "sigs.20260610.test.ef56",
                "tool_name": "extract_hydrological_signatures",
                "session_id": sid,
                "timestamp": "2026-06-10T00:00:00+00:00",
                "key_outputs": {"q_mean": 1.25},
            }
        })
        session.save()

        capsule_path = str(capsule_root / f"capsule_{sid}")
        result = _ts.export_session(session_id=sid, capsule_path=capsule_path)
        assert "error" not in result
        return Path(result["capsule_dir"])

    def test_exit_0_clean_capsule(self, tmp_path, monkeypatch):
        cap = self._build_capsule(tmp_path, monkeypatch)
        proc = subprocess.run(
            [sys.executable, str(cap / "replay.py")],
            capture_output=True,
        )
        assert proc.returncode == 0, proc.stdout.decode() + proc.stderr.decode()

    def test_exit_1_after_tamper(self, tmp_path, monkeypatch):
        cap = self._build_capsule(tmp_path, monkeypatch)
        # Tamper with a file
        (cap / "session.json").write_bytes(b'{"tampered":true}')
        proc = subprocess.run(
            [sys.executable, str(cap / "replay.py")],
            capture_output=True,
        )
        assert proc.returncode == 1

    def test_stdout_shows_pass_lines(self, tmp_path, monkeypatch):
        cap = self._build_capsule(tmp_path, monkeypatch)
        proc = subprocess.run(
            [sys.executable, str(cap / "replay.py")],
            capture_output=True, text=True,
        )
        assert "PASS" in proc.stdout
        assert "Hash check:" in proc.stdout
