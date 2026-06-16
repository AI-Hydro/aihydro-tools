"""
Capsule manifest: deterministic SHA-256 inventory of all capsule files.

build_manifest() is called by export_session after all files are written.
verify_manifest() is called by tests and replay.py.

Neither function modifies files; both are pure I/O.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST_FILE = "capsule_manifest.json"

# Files never included in the manifest (generated at export time, not data)
_SKIP_NAMES: frozenset[str] = frozenset({MANIFEST_FILE, "replay.py"})

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
          "capsule_dir": str,
          "n_files": int,
          "files": [{"path": str, "sha256": str, "size": int}, ...]
        }
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
    return {
        "capsule_dir": str(capsule_dir),
        "n_files": len(entries),
        "files": entries,
    }


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
    Cross-check key_outputs in run_log.json against values in session.json.

    This is a lightweight consistency check, not a full re-execution: it
    confirms that session.json still contains scalar values within *tolerance*
    of what was recorded in the run log at export time.

    Only numeric (int/float) values are checked; strings, None, and private
    keys (starting with "_") are skipped.

    Returns (all_pass, results) where each result dict has keys:
        run_id, key, expected, actual, deviation_pct, status.
    """
    rl_path = capsule_dir / "run_log.json"
    sj_path = capsule_dir / "session.json"
    if not rl_path.exists() or not sj_path.exists():
        return True, []

    run_log: dict = json.loads(rl_path.read_text(encoding="utf-8"))
    session: dict = json.loads(sj_path.read_text(encoding="utf-8"))
    slots: dict = session.get("slots", {})

    results: list[dict] = []
    all_pass = True

    # Map tool_name → canonical slot key (best-effort; partial heuristic)
    _TOOL_TO_SLOT: dict[str, str] = {
        "extract_hydrological_signatures": "signatures",
        "extract_geomorphic_parameters": "geomorphic",
        "compute_twi": "twi",
        "separate_baseflow": "baseflow",
        "train_hydro_model": "model",
        "get_model_results": "model",
        "delineate_watershed": "watershed",
        "delineate_watershed_from_point": "watershed",
        "fetch_streamflow_data": "streamflow",
    }

    for run_id, entry in run_log.items():
        tool_name = entry.get("tool_name", "")
        key_outputs: dict = entry.get("key_outputs", {})
        slot_key = _TOOL_TO_SLOT.get(tool_name)
        slot_data: dict = slots.get(slot_key, {}).get("data", {}) if slot_key else {}

        for k, expected in key_outputs.items():
            if k.startswith("_") or not isinstance(expected, (int, float)):
                continue
            actual = slot_data.get(k)
            if actual is None or not isinstance(actual, (int, float)):
                continue
            if expected == 0:
                dev_pct = 0.0 if abs(actual) < 1e-9 else float("inf")
            else:
                dev_pct = abs(actual - expected) / abs(expected)

            ok = dev_pct <= tolerance
            results.append(
                {
                    "run_id": run_id,
                    "key": k,
                    "expected": expected,
                    "actual": actual,
                    "deviation_pct": round(dev_pct * 100, 3),
                    "status": "pass" if ok else "fail",
                }
            )
            if not ok:
                all_pass = False

    return all_pass, results
