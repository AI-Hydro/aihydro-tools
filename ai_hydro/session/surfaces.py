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


def _sqlite_runs(path: Path) -> tuple[dict, str]:
    """``(records, source)``. Never writes: ``mode=ro``, no PRAGMA, no DDL.

    A read-only directory (or WAL sidecars SQLite may not create) retries with
    ``immutable=1`` and reports source ``sqlite_immutable``.
    """
    last: Exception | None = None
    for suffix, source in (("?mode=ro", "sqlite"), ("?mode=ro&immutable=1", "sqlite_immutable")):
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + suffix, uri=True, timeout=5)
            try:
                rows = conn.execute("SELECT run_id, entry_json FROM runs ORDER BY timestamp, run_id").fetchall()
            finally:
                conn.close()
            return {run_id: json.loads(payload) for run_id, payload in rows}, source
        except sqlite3.Error as exc:
            last = exc
        except (ValueError, TypeError) as exc:      # corrupt row JSON: not an access problem
            raise SnapshotError("RUN_LOG_UNREADABLE", f"Cannot read persisted run log {path}: {exc}") from exc
    raise SnapshotError("RUN_LOG_UNREADABLE", f"Cannot read persisted run log {path}: {last}") from last


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
                             "meta", "inputs", "evidence", "slot", "diff_status", "diff_notes",
                             "record", "minimal", "error", "error_summary"}
            outputs = {k: v for k, v in record.items() if k not in metadata_keys}
        if record.get("minimal") is True:
            # Middleware-written rows carry no tool outputs; nothing may leak in.
            outputs = {}
        if not isinstance(outputs, dict):
            raise SnapshotError("RUN_LOG_INVALID", f"Run '{rid}' has invalid key outputs.")
        rec = record.get("record") if isinstance(record.get("record"), dict) else None
        entries.append({**record, "run_id": rid,
                        "minimal": record.get("minimal") is True,
                        "record_error": rec.get("record_error") if rec else None, "session_id": session_id,
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


_NO_REVISION = {"revision": None, "revision_digest": None, "history_len": 0,
                "revision_drift": None, "approval": {"state": "none"}}


class _ReadOnlyRunSession:
    """The two things ``resolve_source`` needs for run spans, from the snapshot's own run log.

    No ``HydroSession.load`` (it migrates and writes); nothing here touches disk.
    """

    def __init__(self, session_id: str, records: dict):
        self.session_id, self._records = session_id, records

    def get(self, slot: str):
        return (self._records or None) if slot == "_run_log" else None


def _live_evidence(session_id: str, claim: dict, records: dict,
                   like: dict | None = None) -> tuple[dict | None, str | None]:
    """``(evidence_versions, None)`` computed from retained runs, else ``(None, reason)``.

    Same ``fingerprint(resolve_source())`` promotion uses. Dataset and paper
    spans need a loaded session or the passage index and are not checkable here.
    """
    from ai_hydro.approval.records import evidence_fingerprints
    from ai_hydro.session.models import ScientificClaim

    spans = ScientificClaim(**dict(claim)).evidence_spans
    kinds = sorted({str(s.source_type) for s in spans} - {"run"})
    if kinds:
        return None, f"evidence span type(s) {', '.join(kinds)} cannot be checked read-only"
    return evidence_fingerprints(_ReadOnlyRunSession(session_id, records), spans, like=like), None


def _approval_state(session_id: str, claim_id: str, digest: str) -> dict:
    """Read-only approval state for exactly this revision digest.

    Fails closed (ADR-002b): an approval the verifier does not accept (no
    trust root, bad signature, unenrolled key) is ``unverifiable``, never
    ``approved``. ``consumed`` means a registry row already cites it.
    """
    from ai_hydro.approval import records as approvals
    from ai_hydro.approval.trust import check_approval

    matching = [r for r in approvals.iter_approvals()
                if r.get("session_id") == session_id and r.get("claim_id") == claim_id
                and r.get("claim_revision_digest") == digest]
    if not matching:
        return {"state": "none", "for_revision_digest": digest}
    accepted = approvals.find_approval(session_id, claim_id, digest)
    if accepted is None:
        reason = check_approval(matching[-1]).reason
        return {"state": "unverifiable", "for_revision_digest": digest, "reason": reason,
                "channel": None, "trust_root": None, "principal": None, "policy": None}
    stamp = approvals.approval_stamp(accepted)
    consumed = stamp["record_digest"] in approvals.approval_consumers()
    return {"state": "consumed" if consumed else "approved", "for_revision_digest": digest,
            "record_digest": stamp["record_digest"], "channel": stamp["channel"],
            "trust_root": stamp["trust_root"], "principal": stamp["principal"],
            "policy": stamp["policy"]}


def _gate_approval(session_id: str, claim_id: str, recorded: str, live: str | None,
                   unchecked_reason: str | None, changed: list) -> dict:
    """Approval state as the promotion gate would read it.

    ``approved`` only when an accepted approval is bound to the digest
    recomputed with LIVE evidence. An approval for the recorded revision that
    no longer matches the live claim is ``stale_evidence`` (retained evidence
    changed) or ``stale_revision`` (claim fields edited); one whose evidence
    could not be checked read-only is ``evidence_unchecked``. Neither is ever
    ``approved``.
    """
    if live is None:
        state = _approval_state(session_id, claim_id, recorded)
        if state["state"] == "approved":
            return {**state, "state": "evidence_unchecked", "reason": unchecked_reason}
        return state
    state = _approval_state(session_id, claim_id, live)
    if live != recorded and state["state"] == "none":
        old = _approval_state(session_id, claim_id, recorded)
        if old["state"] != "none":
            return {**old, "state": "stale_evidence" if "evidence_versions" in changed else "stale_revision",
                    "recorded_state": old["state"], "live_digest": live}
    return state


def _claim_surface(session_id: str, claim_id: str, claim: dict, chains: dict, capsule: bool,
                   records: dict) -> dict:
    """Additive revision/approval fields for one claim; never raises."""
    if capsule:
        return {**_NO_REVISION, "revision_error": None,
                "revision_drift": None, "revision_drift_reason": "capsule has no claim revision store",
                "approval": {"state": "unverifiable", "reason": "capsule has no claim revision store"}}
    chain = chains.get(claim_id)
    if chain is None:
        return {**_NO_REVISION, "revision_error": None, "revision_drift_reason": "no revision history"}
    if not chain.get("ok"):
        return {**_NO_REVISION, "revision_error": chain.get("error", "chain unreadable"),
                "revision_drift_reason": "revision chain failed verification",
                "approval": {"state": "unverifiable", "reason": "revision chain failed verification"}}
    rows = chain["rows"]
    last = rows[-1] if rows else None
    if last is None:
        return {**_NO_REVISION, "revision_error": None, "revision_drift_reason": "no revision history"}
    out = {"revision": last["revision"], "revision_digest": last["revision_digest"],
           "history_len": len(rows), "revision_error": None}
    live = None
    changed: list = []
    unchecked_reason = None
    try:
        from ai_hydro.approval.records import claim_revision_fields
        from ai_hydro.session.claim_revisions import revision_drift
        from aihydro_core.records import digest as seal_digest

        versions, unchecked_reason = _live_evidence(
            session_id, claim, records, last["content"].get("evidence_versions"))
        checked = versions is not None
        fields = claim_revision_fields(
            claim, versions if checked else last["content"].get("evidence_versions", {}))
        drift = revision_drift(fields, last)
        changed = drift.get("changed_fields", [])
        if checked:
            live = seal_digest(fields)
        elif drift["state"] == "in_sync":
            # Fields match but the evidence was not re-read: not a verdict.
            drift = {"state": "evidence_unchecked", "drift": None, "revision": last["revision"],
                     "revision_digest": last["revision_digest"]}
        out["revision_drift"] = {**drift, "evidence_checked": checked}
        out["revision_drift_reason"] = None if checked else unchecked_reason
    except Exception as exc:
        out["revision_drift"] = None
        out["revision_drift_reason"] = f"{type(exc).__name__}: {exc}"
        unchecked_reason = out["revision_drift_reason"]
    try:
        out["approval"] = _gate_approval(session_id, claim_id, last["revision_digest"], live,
                                         unchecked_reason, changed)
    except Exception as exc:
        out["approval"] = {"state": "unverifiable", "reason": f"{type(exc).__name__}: {exc}"}
    return out


def _surface_claims(session_id: str, claims: dict, capsule: bool, records: dict) -> tuple[dict, str]:
    """``(claims, revision_source)``."""
    chains: dict = {}
    history_error = None
    source = "capsule" if capsule else "absent"
    if not capsule:
        try:
            from ai_hydro.session import claim_revisions
            chains, source = claim_revisions.history_readonly(session_id)
        except Exception as exc:  # store unreadable: per-claim error, snapshot survives
            history_error = f"{type(exc).__name__}: {exc}"
            source = "error"
    result = {}
    for cid, claim in claims.items():
        if not isinstance(claim, dict):
            result[cid] = claim
            continue
        try:
            extra = _claim_surface(session_id, cid, claim, chains, capsule, records)
            if history_error:
                extra.update({**_NO_REVISION, "revision_error": history_error,
                              "approval": {"state": "unverifiable", "reason": history_error}})
        except Exception as exc:
            extra = {**_NO_REVISION, "revision_error": f"{type(exc).__name__}: {exc}"}
        result[cid] = {**claim, **extra}
    return result, source


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
        if capsule:
            records, log_source = _legacy_runs(_read_json(log_path)), "capsule_json"
        else:
            records, log_source = _sqlite_runs(log_path)
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
    surfaced, revision_source = _surface_claims(sid, {**legacy_claims, **claims}, capsule, records)
    snapshot = {
        "schema_version": 1, "session_id": sid, "session_path": str(path),
        "source": "capsule" if capsule else "session", "run_log_source": log_source,
        "claims": surfaced, "revision_source": revision_source, "experiments": experiments,
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
