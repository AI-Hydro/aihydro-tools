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


def fingerprint(record: dict) -> str:
    # Existing run logs may contain unrelated NaNs. Their stable JSON spelling
    # is hashed, but referenced metrics and intervals must be finite below.
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256-v2:" + hashlib.sha256(payload.encode()).hexdigest()


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


def verified_versions(session: Any, spans: list[dict], *, require_uncertainty: bool = False) -> dict[str, str]:
    versions, kinds = {}, {}
    for span in spans:
        sid = span["source_id"]
        if sid in kinds and kinds[sid] != span["source_type"]:
            raise EvidenceError("EVIDENCE_IDENTITY_MISMATCH", sid, "Different evidence types share a source_id; use distinct identifiers.")
        kinds[sid] = span["source_type"]
        record = resolve_source(session, span)
        validate_span(record, span, require_uncertainty=require_uncertainty)
        version = fingerprint(record)
        if sid in versions and versions[sid] != version:
            raise EvidenceError("EVIDENCE_IDENTITY_MISMATCH", sid, "One source_id resolves to different retained records.")
        versions[sid] = version
    return versions
