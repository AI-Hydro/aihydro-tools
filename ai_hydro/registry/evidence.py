"""Resolve registry evidence by exact identity and validate numerical support.

No fuzzy source names, fabricated run hashes, or substitutions from current
slots for historical runs. These checks establish retained-record integrity;
they do not establish causal validity, scope identity, or scientific truth.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


class EvidenceError(ValueError):
    def __init__(self, code: str, source_id: str, message: str):
        self.code, self.source_id = code, source_id
        super().__init__(message)

    def to_dict(self) -> dict:
        return {
            "error": True, "code": self.code, "source_id": self.source_id,
            "message": str(self),
            "recovery": "Retain the originating result with its exact metric, uncertainty and checks; "
                        "correct the evidence reference, then request promotion again. "
                        "The session claim remains available for investigation.",
            "next_tools": ["update_claim_status"],
        }


def resolve_source(session: Any, span: dict) -> dict:
    kind, sid = span.get("source_type"), span.get("source_id", "")
    if kind == "run":
        record = (session.get("_run_log") or {}).get(sid)
        if not isinstance(record, dict) or not record:
            raise EvidenceError("EVIDENCE_UNRESOLVED", sid, f"Run '{sid}' is not retained in this session.")
        if record.get("session_id", session.session_id) != session.session_id or record.get("run_id", sid) != sid:
            raise EvidenceError("EVIDENCE_IDENTITY_MISMATCH", sid, f"Run '{sid}' has conflicting session/run identity.")
        return record
    if kind == "dataset":
        # An artifact manifest records a digest but does not retain its payload.
        # Only an exact session product name can currently be re-resolved here.
        record = session.get(sid) if sid and not sid.startswith("_") else None
        if not isinstance(record, dict) or not record:
            raise EvidenceError("EVIDENCE_UNRESOLVED", sid, f"Dataset '{sid}' has no retained exact-name result. "
                                "Manifest metadata or a similar source name is insufficient.")
        return record
    if kind == "paper":
        from ai_hydro.knowledge.embeddings import _passage_hash, resolve_passage_hash
        passage_id = span.get("passage_hash") or sid
        record = resolve_passage_hash(passage_id)
        if not record or not record.get("text") or _passage_hash(record["text"]) != passage_id:
            raise EvidenceError("EVIDENCE_UNRESOLVED", sid, f"Paper '{sid}' has no intact indexed passage.")
        if sid not in (passage_id, record.get("doc_name")):
            raise EvidenceError("EVIDENCE_IDENTITY_MISMATCH", sid, "Paper source_id must match the passage hash or indexed doc_name.")
        if span.get("page") is not None:
            raise EvidenceError("EVIDENCE_UNRESOLVED", sid, "The passage index has no page mapping; this page reference cannot be verified.")
        return record
    raise EvidenceError("EVIDENCE_UNRESOLVED", sid, "Unsupported evidence source type.")


# Evidence fingerprint versions. A stored fingerprint is always re-computed in
# ITS OWN version, so changing the rule never makes old bindings drift.
#   sha256-v2  whole retained record (a run row includes its ``record`` seal and
#              any ``record_status*`` keys, so sealing a row later changes it)
#   sha256-v3  same, except a *run row* is hashed over its body only: the seal
#              (``record``) and the seal status (``record_status*``) are metadata
#              about the body, so attaching either later is fingerprint-neutral.
#              Datasets and passages are hashed whole, exactly as in v2.
# ``capsule/standalone_replay.py`` mirrors the excluded-key rule in stdlib; a
# test pins that the two agree.
FINGERPRINT_V2 = "sha256-v2"
FINGERPRINT_V3 = "sha256-v3"
FINGERPRINT_CURRENT = FINGERPRINT_V3
RUN_ROW_SEAL_KEY = "record"
RUN_ROW_STATUS_PREFIX = "record_status"


def is_run_row_metadata_key(key: Any) -> bool:
    """True for run-log row keys a v3 fingerprint excludes."""
    return key == RUN_ROW_SEAL_KEY or (isinstance(key, str) and key.startswith(RUN_ROW_STATUS_PREFIX))


def run_row_body(row: dict) -> dict:
    return {k: v for k, v in row.items() if not is_run_row_metadata_key(k)}


def fingerprint(record: dict, version: str = FINGERPRINT_V2) -> str:
    """``<version>:<sha256>`` of ``record``. Default stays v2 (whole record) for
    callers that are not evidence bindings; evidence uses ``evidence_fingerprint``."""
    # Existing run logs may contain unrelated NaNs. Their stable JSON spelling
    # is hashed, but referenced metrics and intervals must be finite below.
    if version == FINGERPRINT_V3:
        pass
    elif version != FINGERPRINT_V2:
        raise ValueError(f"unknown fingerprint version {version!r}")
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{version}:" + hashlib.sha256(payload.encode()).hexdigest()


def fingerprint_version(stored: Any) -> str | None:
    """The version prefix of a stored fingerprint, or None when not a known one."""
    for version in (FINGERPRINT_V2, FINGERPRINT_V3):
        if isinstance(stored, str) and stored.startswith(version + ":"):
            return version
    return None


def evidence_fingerprint(record: dict, kind: str | None = None, *, like: Any = None) -> str:
    """Fingerprint of a retained source for an evidence binding.

    ``like`` is a stored fingerprint for this source: the result is computed in
    its version (v2 or v3). Without a usable ``like`` (new binding) it is v3.
    """
    version = fingerprint_version(like) or FINGERPRINT_CURRENT
    if version == FINGERPRINT_V3 and kind == "run" and isinstance(record, dict):
        record = run_row_body(record)
    return fingerprint(record, version)


def _lookup(record: Any, path: str) -> Any:
    # Direct keys may legitimately contain dots. Otherwise accept dict paths
    # only: indexing a summary count or array is not numerical evidence.
    if not isinstance(record, dict):
        return None
    if path in record:
        return record[path]
    current = record
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _finite(value: Any) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def validate_span(record: dict, span: dict, *, require_uncertainty: bool) -> None:
    sid = span["source_id"]
    evidence = record.get("evidence")
    if evidence is not None and (not isinstance(evidence, dict) or evidence.get("schema_version") != 1):
        raise EvidenceError("EVIDENCE_UNRESOLVED", sid, "Unsupported run evidence schema; re-run the originating tool.")
    payload = evidence if evidence is not None else record
    data = payload.get("data", payload.get("key_outputs", payload))
    flags = payload.get("quality_flags") or (data.get("_quality_flags") if isinstance(data, dict) else None) or []
    if (payload.get("error") or record.get("error") or
            str(payload.get("status", "")).lower() in {"fail", "failed", "error", "invalid"} or
            any(isinstance(f, dict) and str(f.get("status", "")).lower() in {"fail", "failed", "error", "invalid"} for f in flags)):
        raise EvidenceError("EVIDENCE_CHECK_FAILED", sid, f"Evidence '{sid}' records a failed analysis or validation check.")

    metric = span.get("metric_ref")
    if not metric:
        return
    # 'kge' and 'key_outputs.kge' both refer to the original output, never
    # arbitrary run metadata such as timestamp or the number of list elements.
    for prefix in ("key_outputs.", "data."):
        if metric.startswith(prefix):
            metric = metric[len(prefix):]
            break
    value = _lookup(data, metric)
    if not _finite(value):
        raise EvidenceError("EVIDENCE_METRIC_UNAVAILABLE", sid, f"Metric '{span['metric_ref']}' is missing or is not a finite scalar in '{sid}'.")
    if not require_uncertainty:
        return
    uncertainties = payload.get("uncertainty") or (data.get("_uncertainty") if isinstance(data, dict) else None)
    uncertainty = _lookup(uncertainties, metric)
    if not isinstance(uncertainty, dict):
        raise EvidenceError("EVIDENCE_UNCERTAINTY_UNAVAILABLE", sid, f"No persisted uncertainty for metric '{metric}' in '{sid}'.")
    if (not all(_finite(uncertainty.get(k)) for k in ("value", "ci_low", "ci_high", "ci_level", "n")) or
            uncertainty["ci_low"] > uncertainty["ci_high"] or
            not 0 < uncertainty["ci_level"] < 1 or uncertainty["n"] < 2 or
            uncertainty["n"] != int(uncertainty["n"]) or
            str(uncertainty.get("status", "")).lower() in {"fail", "failed", "error", "invalid", "unavailable"} or
            not isinstance(uncertainty.get("method"), str) or
            uncertainty["method"].strip().lower() in {"", "none", "unavailable", "unknown"}):
        raise EvidenceError("EVIDENCE_UNCERTAINTY_INVALID", sid, f"Uncertainty for '{metric}' lacks finite ordered bounds, a method, sample count or confidence level.")
    if not math.isclose(value, uncertainty["value"], rel_tol=1e-12, abs_tol=1e-12):
        raise EvidenceError("EVIDENCE_UNCERTAINTY_MISMATCH", sid, f"Uncertainty estimate for '{metric}' does not match its persisted point estimate.")


def verified_versions(session: Any, spans: list[dict], *, require_uncertainty: bool = False,
                      like: dict | None = None) -> dict[str, str]:
    versions, kinds = {}, {}
    for span in spans:
        sid = span["source_id"]
        if sid in kinds and kinds[sid] != span["source_type"]:
            raise EvidenceError("EVIDENCE_IDENTITY_MISMATCH", sid, "Different evidence types share a source_id; use distinct identifiers.")
        kinds[sid] = span["source_type"]
        record = resolve_source(session, span)
        validate_span(record, span, require_uncertainty=require_uncertainty)
        version = evidence_fingerprint(record, span["source_type"], like=(like or {}).get(sid))
        if sid in versions and versions[sid] != version:
            raise EvidenceError("EVIDENCE_IDENTITY_MISMATCH", sid, "One source_id resolves to different retained records.")
        versions[sid] = version
    return versions
