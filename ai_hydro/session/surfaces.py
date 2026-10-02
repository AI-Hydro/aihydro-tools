"""Versioned, read-only snapshots for research panels and headless consumers.

Storage details stay in Python. Reading does not migrate sessions, synthesize
run history, create a database, or recompute scientific results.
"""
from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from typing import Any

from ai_hydro.session import store

RESOURCE_TEMPLATE = "aihydro://research/snapshot/{reference}"


class SnapshotError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    def to_dict(self) -> dict:
        return {"error": True, "code": self.code, "message": str(self)}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError("RESEARCH_SNAPSHOT_UNREADABLE", f"Cannot read {path}: {exc}") from exc


def _resolve_path(reference: str, home: Path) -> Path:
    value = reference.strip()
    if not value:
        raise SnapshotError("SESSION_NOT_FOUND", "A session ID or explicit session/capsule path is required.")
    target = Path(value).expanduser()
    if target.is_absolute() or len(target.parts) > 1:
        candidates = [target, target / "session.json"]
    else:
        candidates = [home / "sessions" / f"{store._safe_filename_component(value)}.json",
                      home / "exports" / f"capsule_{value}" / "session.json",
                      home / "capsules" / value / "session.json"]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise SnapshotError("SESSION_NOT_FOUND", f"No persisted session or capsule found for '{reference}'.")


def _slot(raw: dict, name: str) -> Any:
    value = raw.get(name)
    if not isinstance(value, dict):
        return value
    if raw.get("_hydro_slots_v2") or "__legacy__" in value:
        active = raw.get("active_feature_id")
        result = None
        for feature in ([active] if active and active != "__legacy__" else []) + ["__legacy__"]:
            by_key = value.get(feature, {})
            if isinstance(by_key, dict):
                result = store._latest_result_in_feature(by_key)
            if result is not None:
                break
        value = result
    if isinstance(value, dict) and "data" in value:
        value = value["data"]
    return value


def _legacy_runs(value: Any) -> dict:
    # Known legacy wrappers only. A current analysis product is never a log.
    if isinstance(value, dict) and "__legacy__" in value:
        value = value["__legacy__"].get("", {})
    if isinstance(value, dict) and "data" in value and set(value) <= {"data", "meta"}:
        value = value["data"]
    if value is None:
        return {}
    if isinstance(value, list):
        result = {}
        for entry in value:
            if not isinstance(entry, dict) or not isinstance(entry.get("run_id"), str) or not entry["run_id"]:
                raise SnapshotError("RUN_LOG_INVALID", "A legacy run-list entry has no persisted run_id.")
            if entry["run_id"] in result:
                raise SnapshotError("RUN_LOG_INVALID", "Duplicate run IDs in legacy run list.")
            result[entry["run_id"]] = entry
        return result
    if not isinstance(value, dict):
        raise SnapshotError("RUN_LOG_INVALID", "Persisted run log is not a dictionary or identified run list.")
    return value


def _sqlite_runs(path: Path) -> dict:
    # URI mode=ro never creates a missing DB; no HydroSession.load migration.
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute("SELECT run_id, entry_json FROM runs ORDER BY timestamp, run_id").fetchall()
        finally:
            conn.close()
        records = {}
        for run_id, payload in rows:
            records[run_id] = json.loads(payload)
        return records
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise SnapshotError("RUN_LOG_UNREADABLE", f"Cannot read persisted run log {path}: {exc}") from exc


def _normalize_runs(records: dict, session_id: str) -> list[dict]:
    entries = []
    for rid, record in records.items():
        if not isinstance(rid, str) or not rid or not isinstance(record, dict):
            raise SnapshotError("RUN_LOG_INVALID", "Persisted run IDs and records must be nonempty strings and objects.")
        if record.get("run_id", rid) != rid or record.get("session_id", session_id) != session_id:
            raise SnapshotError("RUN_IDENTITY_MISMATCH", f"Run '{rid}' conflicts with its persisted key or session identity.")
        meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
        outputs = record.get("key_outputs", record.get("data"))
        if outputs is None:
            metadata_keys = {"run_id", "session_id", "tool_name", "tool", "timestamp", "created_at",
                             "meta", "inputs", "evidence", "slot", "diff_status", "diff_notes"}
            outputs = {k: v for k, v in record.items() if k not in metadata_keys}
        if not isinstance(outputs, dict):
            raise SnapshotError("RUN_LOG_INVALID", f"Run '{rid}' has invalid key outputs.")
        entries.append({**record, "run_id": rid, "session_id": session_id,
                        "tool_name": str(record.get("tool_name") or record.get("tool") or meta.get("tool") or "unknown"),
                        "timestamp": str(record.get("timestamp") or record.get("created_at") or meta.get("computed_at") or ""),
                        "key_outputs": outputs})
    return sorted(entries, key=lambda entry: (entry["timestamp"], entry["run_id"]))


def _record_visibility(records: dict) -> tuple[dict | None, list[dict]]:
    """Record coverage counts and per-run ``record_error`` messages.

    Verification only; never writes. ``(None, [])`` when the record contract
    (aihydro-core records) is unavailable, so old installs still read.
    """
    try:
        from ai_hydro.session.run_records import coverage_summary
    except ImportError:
        return None, []
    coverage = coverage_summary(records)
    errors = [{"run_id": rid, "record_error": entry["record"]["record_error"]}
              for rid, entry in records.items()
              if isinstance(entry, dict) and isinstance(entry.get("record"), dict)
              and entry["record"].get("record_error")]
    return coverage, errors


def read_research_snapshot(reference: str, *, home: Path | None = None) -> dict:
    """Read stored claims, active experiment slot and exact recorded runs.

    A capsule's adjacent run_log.json wins over its embedded legacy log; it
    never resolves a same-ID live SQLite file. For sessions, SQLite wins even
    when empty. Corruption is an error, never an excuse for stale fallback.
    """
    home = Path(home) if home is not None else store._SESSIONS_DIR.parent
    path = _resolve_path(reference, home)
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise SnapshotError("SESSION_INVALID", "Persisted session must be a JSON object.")
    sid = str(raw.get("session_id") or raw.get("gauge_id") or path.stem)
    capsule = path.name == "session.json" and path.parent != (home / "sessions").resolve()
    log_path = path.parent / "run_log.json" if capsule else path.with_suffix(".runlog.sqlite3")
    if log_path.exists():
        records = _legacy_runs(_read_json(log_path)) if capsule else _sqlite_runs(log_path)
        log_source = "capsule_json" if capsule else "sqlite"
    elif "_run_log" in raw:
        records = _legacy_runs(raw["_run_log"])
        log_source = "legacy_json"
    else:
        records, log_source = {}, "absent"
    legacy_claims = _slot(raw, "_claims") or {}
    claims = raw.get("claims") or {}
    experiments = _slot(raw, "_experiments") or {}
    if not all(isinstance(value, dict) for value in (legacy_claims, claims, experiments)):
        raise SnapshotError("SESSION_INVALID", "Claims and experiment slots must be objects.")
    record_coverage, record_errors = _record_visibility(records)
    snapshot = {
        "schema_version": 1, "session_id": sid, "session_path": str(path),
        "source": "capsule" if capsule else "session", "run_log_source": log_source,
        "claims": {**legacy_claims, **claims}, "experiments": experiments,
        "runs": _normalize_runs(records, sid),
        # Additive (schema_version stays 1): how many rows carry a verifiable
        # aihydro.run/2 record, and which records say a digest is missing.
        "record_coverage": record_coverage, "record_errors": record_errors,
        "warnings": ["No retained run log is available; current results are not historical runs."] if log_source == "absent" else [],
    }
    return store._json_safe(snapshot)


def snapshot_resource(reference: str) -> str:
    """MCP transport: reference is UTF-8 encoded as unpadded base64url."""
    try:
        decoded = base64.b64decode(reference + "=" * (-len(reference) % 4), altchars=b"-_", validate=True).decode("utf-8")
        result = read_research_snapshot(decoded)
    except (ValueError, UnicodeError) as exc:
        result = exc.to_dict() if isinstance(exc, SnapshotError) else {"error": True, "code": "INVALID_REFERENCE", "message": "Invalid snapshot reference."}
    return json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference")
    parser.add_argument("--home", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(read_research_snapshot(args.reference, home=args.home), allow_nan=False))
    except SnapshotError as error:
        print(json.dumps(error.to_dict()))
        raise SystemExit(1)
