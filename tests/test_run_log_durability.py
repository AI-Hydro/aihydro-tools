"""
Run-log durability tests (Wave 0.3 of the ecosystem remediation plan).

Covers:
1. Concurrency — N writers to the same session's run log via distinct
   run_ids must all survive (the SQLite migration's core promise: no more
   whole-session load->mutate->save lost-update race).
2. Migration — pre-SQLite _run_log data found in a session's JSON file is
   imported once, idempotently, and the session JSON no longer re-serializes
   _run_log going forward.
3. Cross-writer union — the three call sites (session.set("_run_log", ...),
   put_result()'s automatic recording, and a second concurrent set() call)
   never delete each other's rows.
"""
from __future__ import annotations

import json
import threading
from unittest.mock import patch

import pytest


class TestRunLogConcurrency:
    def test_n_writers_all_survive(self, tmp_path):
        """N threads each record a distinct run_id for the same session —
        all N must be present afterward (the old dict-slot design would lose
        some to the load->mutate->save race under real multi-process
        concurrency; this proves the row-level SQLite upsert doesn't)."""
        from ai_hydro.session.store import HydroSession

        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
            session_id = "concurrency-test"
            HydroSession(session_id).save()

            n = 20
            errors = []

            def _write(i):
                try:
                    s = HydroSession.load(session_id)
                    s.set("_run_log", {f"run-{i}": {"tool_name": "t", "timestamp": str(i), "key_outputs": {"i": i}}})
                except Exception as exc:  # pragma: no cover - surfaced via errors list
                    errors.append(exc)

            threads = [threading.Thread(target=_write, args=(i,)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"writer threads raised: {errors}"
            final = HydroSession.load(session_id)
            run_log = final.get("_run_log") or {}
            assert len(run_log) == n, f"expected {n} runs, got {len(run_log)}: {sorted(run_log)}"
            for i in range(n):
                assert run_log[f"run-{i}"]["key_outputs"]["i"] == i

    def test_put_result_and_explicit_set_do_not_clobber_each_other(self, tmp_path):
        """put_result()'s automatic run-log recording and an explicit
        session.set("_run_log", ...) call are two of the three writers this
        migration consolidates — both must survive side by side."""
        from ai_hydro.session import HydroSession

        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("mixed-writers")
            s.put_result("twi", "ann1", "p30", {
                "data": {"twi_mean": 8.2},
                "meta": {"tool": "compute_twi", "computed_at": "2024-01-01T00:00:00+00:00"},
            })
            s.set("_run_log", {"manual-run": {"tool_name": "manual", "timestamp": "z", "key_outputs": {"x": 1}}})
            s.save()

            reloaded = HydroSession.load("mixed-writers")
            run_log = reloaded.get("_run_log") or {}
            assert "manual-run" in run_log
            assert run_log["manual-run"]["key_outputs"]["x"] == 1
            # put_result's auto-generated entry (hashed run_id, prefixed by slot)
            auto_entries = [rid for rid in run_log if rid.startswith("twi.")]
            assert len(auto_entries) == 1


class TestRunLogMigration:
    def test_legacy_json_run_log_is_imported_once(self, tmp_path):
        """A session file written before this migration (flat _run_log dict
        directly in the session JSON, C1 v2 nested shape) must have its
        entries imported into the SQLite store on first load."""
        from ai_hydro.session.store import HydroSession

        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
            session_id = "legacy-runlog-session"
            legacy_json = {
                "session_id": session_id,
                "_hydro_slots_v2": True,
                "_run_log": {
                    "__legacy__": {
                        "": {
                            "old-run-1": {"tool_name": "delineate_watershed", "timestamp": "t1", "key_outputs": {"area_km2": 100}},
                        }
                    }
                },
            }
            (tmp_path / f"{session_id}.json").write_text(json.dumps(legacy_json))

            loaded = HydroSession.load(session_id)
            run_log = loaded.get("_run_log") or {}
            assert "old-run-1" in run_log
            assert run_log["old-run-1"]["key_outputs"]["area_km2"] == 100

    def test_run_log_no_longer_round_trips_through_session_json(self, tmp_path):
        """After the fix, _run_log must not be re-serialized into the
        session's own JSON file — it lives only in the SQLite store."""
        from ai_hydro.session import HydroSession

        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("no-json-runlog")
            s.set("_run_log", {"r1": {"tool_name": "x", "timestamp": "t", "key_outputs": {}}})
            s.save()

            raw = json.loads((tmp_path / "no-json-runlog.json").read_text())
            assert "_run_log" not in raw

    def test_migration_is_idempotent(self, tmp_path):
        """Loading the same legacy session twice must not duplicate or error."""
        from ai_hydro.session.store import HydroSession

        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
            session_id = "legacy-idempotent"
            legacy_json = {
                "session_id": session_id,
                "_run_log": {"r1": {"tool_name": "x", "timestamp": "t", "key_outputs": {"v": 1}}},
            }
            (tmp_path / f"{session_id}.json").write_text(json.dumps(legacy_json))

            HydroSession.load(session_id)
            second = HydroSession.load(session_id)
            run_log = second.get("_run_log") or {}
            assert len(run_log) == 1
            assert run_log["r1"]["key_outputs"]["v"] == 1


class TestRunLogArbitraryEntryShapes:
    def test_bare_metric_dict_entries_round_trip(self, tmp_path):
        """Bench/test fixtures write bare {metric: value} entries (no
        tool_name/timestamp/key_outputs wrapper) resolved directly by
        json-path in the answer auditor — these must round-trip verbatim,
        not be coerced into the production entry schema."""
        from ai_hydro.session import HydroSession

        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("bare-entry-shape")
            s.set("_run_log", {"r-bare": {"kge": 0.82}})
            s.save()

            reloaded = HydroSession.load("bare-entry-shape")
            run_log = reloaded.get("_run_log") or {}
            assert run_log["r-bare"] == {"kge": 0.82}


class TestRunLogLockContention:
    def test_no_write_is_lost_or_swallowed_under_contention(self, tmp_path, caplog):
        """Regression (2026-10-02): the journal-mode PRAGMA on every connect
        returned "database is locked" under contention, the writer swallowed
        it, and a run-log row was lost silently. Probabilistic: on the old
        writer this failed roughly 1 run in 3 on an idle machine and more
        under load; the fixed writer lost 0 of 800 writes under CPU load.
        Every row must land and no lock error may be logged."""
        import logging

        from ai_hydro.session.store import HydroSession

        caplog.set_level(logging.WARNING, logger="ai_hydro")
        for trial in range(30):
            d = tmp_path / f"t{trial}"
            d.mkdir()
            with patch("ai_hydro.session.store._SESSIONS_DIR", d):
                sid = "contention"
                HydroSession(sid).save()

                def _write(i):
                    s = HydroSession.load(sid)
                    s.set("_run_log", {f"run-{i}": {"tool_name": "t", "timestamp": str(i), "key_outputs": {"i": i}}})

                threads = [threading.Thread(target=_write, args=(i,)) for i in range(32)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                assert len(HydroSession.load(sid).get("_run_log") or {}) == 32, f"trial {trial} lost a row"
        locked = [r.getMessage() for r in caplog.records if "locked" in r.getMessage().lower()]
        assert not locked, f"lock errors were swallowed: {locked}"

    def test_writer_waits_for_a_held_lock_instead_of_dropping_the_row(self, tmp_path):
        """Guarantee (not a reproduction of the 2026-10-02 race): while
        another connection holds an exclusive lock, the writer waits and
        stores the row rather than dropping it. The original race, a lock
        error swallowed under heavy concurrent first-time WAL setup, is
        timing-dependent; the contention test above exercises it
        probabilistically."""
        import sqlite3
        import time as _time

        from ai_hydro.session import store

        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
            sid = "held-lock"
            db = store._run_log_db_path(sid)
            setup = sqlite3.connect(str(db))
            setup.execute("PRAGMA journal_mode=DELETE")
            setup.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, timestamp TEXT, entry_json TEXT)")
            setup.commit()
            setup.close()

            holder = sqlite3.connect(str(db), check_same_thread=False)
            holder.execute("BEGIN EXCLUSIVE")
            release = threading.Timer(0.5, lambda: (holder.commit(), holder.close()))
            release.start()
            started = _time.monotonic()
            status = store._run_log_record(sid, "run-held", {"tool_name": "t", "timestamp": "0"})
            release.join()

            assert status == "inserted", status
            assert _time.monotonic() - started >= 0.4  # it waited for the lock
            assert "run-held" in store._run_log_read_all(sid)
