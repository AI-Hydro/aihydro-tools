"""
Capsule manifest: deterministic SHA-256 inventory of all capsule files.

build_manifest() is called by export_session after all files are written.
verify_manifest() is called by tests and replay.py.

Neither function modifies files; both are pure I/O.

Replay vocabulary: a capsule supports ``archive_integrity`` (files and record
digests re-verify). It never claims ``recomputed``; see standalone_replay.py.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST_FILE = "capsule_manifest.json"

# Files never included in the manifest (generated at export time, not data)
#
# bundle.json, ro-crate-metadata.json and manifest-sha256.txt are written after
# the manifest (they describe it) and so can never be listed in it.
_SKIP_NAMES: frozenset[str] = frozenset({MANIFEST_FILE, "replay.py", "bundle.json",
                                         "ro-crate-metadata.json", "manifest-sha256.txt"})

# The strongest replay level a capsule archive supports (aihydro_core.records
# ReplayStatus). Hashes and record digests can be re-verified; no computation
# is re-executed.
REPLAY_STATUS = "archive_integrity"

# Default tolerance for --live numerical comparison (1 % relative)
TOLERANCE_DEFAULT: float = 0.01


def build_manifest(capsule_dir: Path) -> dict:
    """
    Walk capsule_dir recursively, SHA-256-hash every file, return manifest dict.

    Skips replay.py and capsule_manifest.json (generated at export time).
    File paths are relative to capsule_dir; ordering is lexicographic so the
    manifest is deterministic on every platform.

    Return shape::

        {
          "n_files": int,
          "files": [{"path": str, "sha256": str, "size": int}, ...],
          "replay_status": "archive_integrity",
          "recomputation": "not_performed",
          "run_records": {...}          # present when run_log.json exists
        }

    ``replay_status`` is the strongest level the archive itself can support:
    file hashes and record digests can be re-verified, nothing is recomputed.
    ``replay.py --live`` may report ``cross_check`` for a given run, but an
    export never claims more than ``archive_integrity``.
    """
    entries: list[dict] = []
    for p in sorted(capsule_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.name in _SKIP_NAMES:
            continue
        raw = p.read_bytes()
        entries.append(
            {
                "path": str(p.relative_to(capsule_dir).as_posix()),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        )
    manifest = {
        "n_files": len(entries),
        "files": entries,
        "replay_status": REPLAY_STATUS,
        "recomputation": "not_performed",
    }
    run_records = _run_record_summary(capsule_dir)
    if run_records is not None:
        manifest["run_records"] = run_records
    return manifest


def _run_record_summary(capsule_dir: Path) -> dict | None:
    """Counts of v2 records in the capsule's run log, plus the environment.

    ``environment`` is the fingerprint of the *exporting* process. A record's
    ``env_digest`` can be matched to it, so ``matches_exporting_environment``
    says which records were produced under the interpreter that wrote this
    capsule; the rest only carry a digest.
    """
    rl_path = capsule_dir / "run_log.json"
    if not rl_path.exists():
        return None
    try:
        run_log = json.loads(rl_path.read_text(encoding="utf-8"))
        records = [e["record"] for e in run_log.values()
                   if isinstance(e, dict) and isinstance(e.get("record"), dict)]
        summary: dict = {
            "schema": "aihydro.run/2",
            "run_log_rows": len(run_log),
            "v2_records": len(records),
            "legacy_unrecorded": len(run_log) - len(records),
            # rows the recorder could not seal and says so (a subset of the above)
            "unsealable": sum(1 for e in run_log.values()
                              if isinstance(e, dict) and not isinstance(e.get("record"), dict)
                              and e.get("record_status") == "unsealable"),
            "records_with_record_error": sum(1 for r in records if r.get("record_error")),
        }
        try:
            from ai_hydro.session.run_records import process_environment

            fingerprint, env_digest = process_environment()
            summary["environment"] = {"env_digest": env_digest, "fingerprint": fingerprint}
            summary["matches_exporting_environment"] = sum(
                1 for r in records if r.get("env_digest") == env_digest
            )
        except Exception:
            summary["environment"] = None
        return summary
    except Exception:
        return None


def verify_manifest(capsule_dir: Path) -> tuple[bool, list[dict]]:
    """
    Compare actual file hashes against the manifest written during export.

    Returns (all_pass, results) where each result dict has keys:
        path, status ("pass" | "fail" | "missing"), expected, actual.

    Returns (False, [{"path": MANIFEST_FILE, "status": "missing"}]) when the
    manifest file itself does not exist.
    """
    manifest_path = capsule_dir / MANIFEST_FILE
    if not manifest_path.exists():
        return False, [
            {
                "path": MANIFEST_FILE,
                "status": "missing",
                "expected": "",
                "actual": "",
            }
        ]

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results: list[dict] = []
    all_pass = True

    for entry in manifest["files"]:
        fpath = capsule_dir / entry["path"]
        if not fpath.exists():
            results.append(
                {
                    "path": entry["path"],
                    "status": "missing",
                    "expected": entry["sha256"],
                    "actual": "",
                }
            )
            all_pass = False
        else:
            actual = hashlib.sha256(fpath.read_bytes()).hexdigest()
            ok = actual == entry["sha256"]
            results.append(
                {
                    "path": entry["path"],
                    "status": "pass" if ok else "fail",
                    "expected": entry["sha256"],
                    "actual": actual,
                }
            )
            if not ok:
                all_pass = False

    return all_pass, results


def verify_live(
    capsule_dir: Path,
    *,
    tolerance: float = TOLERANCE_DEFAULT,
) -> tuple[bool, list[dict]]:
    """
    Cross-check numeric key_outputs in run_log.json against values retained in
    session.json. Delegates to the standalone verifier written into every
    capsule as replay.py, so library and script cannot disagree.

    This is a consistency check between two stores, not a re-execution
    (replay level ``cross_check`` at most). Only numeric scalars are compared;
    private keys (starting with "_"), strings and None are skipped.

    Returns (all_pass, results). **An empty ``results`` list means nothing was
    compared**, and ``all_pass`` is then True only in the vacuous sense; the
    generated replay.py reports this case explicitly and exits 2 under
    ``--live``. Each result has: run_id, key, expected, actual,
    deviation_pct, matched_by, status.
    """
    from ai_hydro.capsule.standalone_replay import live_cross_check

    comparisons, _skipped = live_cross_check(capsule_dir, tolerance)
    return all(c["status"] == "pass" for c in comparisons), comparisons
