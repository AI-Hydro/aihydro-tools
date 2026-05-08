"""
Scientific Claims and Assumptions models.

Captures what the system believes (claims) and what it assumes
during a research session.
"""
from __future__ import annotations
import logging
from typing import Literal, Any
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class ClaimScope(BaseModel):
    """The boundary within which a scientific claim is valid."""
    basins: list[str]
    period: str
    forcing: str | None = None
    metric: str | None = None        # registry ID e.g. "metric.kge"
    model_versions: dict[str, str] = {}


class EvidenceSpan(BaseModel):
    """
    A typed pointer to a specific piece of evidence backing a scientific claim.

    - Run-backed: source_type="run", source_id=run_id, metric_ref="kge"
    - Paper-backed: source_type="paper", source_id=paper_id, page=12,
                    passage_hash=sha256_of_quoted_text
    - Dataset-backed: source_type="dataset", source_id=dataset_doi_or_name
    """
    source_type: Literal["run", "paper", "dataset"]
    source_id: str
    metric_ref: str | None = None      # for run-backed: "kge", "nse", "rmse"
    page: int | None = None            # for paper-backed
    passage_hash: str | None = None    # sha256 of quoted passage (paper-backed)


class ScientificClaim(BaseModel):
    """
    A scoped scientific conclusion backed by typed evidence spans.

    Evidence is required for promotion to the global registry, but a claim
    may be created in "proposed" status without it to record a hypothesis
    before testing. Evidence accumulates via update_claim_status.
    """
    id: str
    claim: str
    claim_type: Literal["empirical_result", "methodological", "hypothesis", "negative_result"]
    status: Literal[
        "proposed", "tested", "supported", "weakly_supported",
        "contradicted", "inconclusive", "superseded", "retracted"
    ]
    confidence: Literal["high", "medium", "low", "speculative"]
    confidence_rationale: str
    scope: ClaimScope
    evidence_spans: list[EvidenceSpan] = []
    contradictions: list[str] = []
    limitations: list[str] = []       # at least one required for promotion
    citations: list[str] = []
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @model_validator(mode="before")
    @classmethod
    def _migrate_evidence(cls, data: Any) -> Any:
        """
        Migrate legacy evidence: list[dict] → evidence_spans: list[EvidenceSpan].

        Sessions saved before EvidenceSpan was introduced store evidence as
        untyped dicts under the key "evidence". This validator coerces them on
        load so existing session files are never broken.
        """
        if not isinstance(data, dict):
            return data
        if "evidence" in data and data["evidence"] and "evidence_spans" not in data:
            old = data.pop("evidence")
            spans = []
            for e in old:
                if isinstance(e, dict):
                    spans.append({
                        "source_type": e.get("source_type", "run"),
                        "source_id": e.get("run_id") or e.get("source_id") or "unknown",
                        "metric_ref": next(
                            (str(v) for k, v in e.items()
                             if k not in ("run_id", "source_type", "source_id")),
                            None,
                        ),
                    })
            data["evidence_spans"] = spans
            log.warning(
                "Migrated legacy 'evidence' list to 'evidence_spans' on claim load. "
                "Re-save this claim to persist the new format."
            )
        elif "evidence" in data and not data.get("evidence_spans"):
            data.pop("evidence", None)
        return data

    @field_validator("confidence_rationale")
    @classmethod
    def _rationale_not_trivial(cls, v: str) -> str:
        if len(v.strip()) < 20:
            raise ValueError(
                "confidence_rationale must be at least 20 characters. "
                "Write one sentence explaining why this confidence level was chosen."
            )
        return v


class Assumption(BaseModel):
    """
    A scientific assumption made during the research process.
    """
    id: str
    statement: str
    scope: str                       # run_id or session_id
    risk: Literal["low", "medium", "high"]
    risk_rationale: str
    affects: list[str] = []          # ["model_performance", "water_balance"]
    validation_possible: bool = True
    validated: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
