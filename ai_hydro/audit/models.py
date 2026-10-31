"""
Audit result models for AI-Hydro Answer Auditor.

Mirror the ValidatorResult shape (ai_hydro/validators/models.py) so
downstream consumers — enforcement, export, UI chips — can handle
audit results and validator results with the same code paths.
"""
from __future__ import annotations

from typing import Literal, Any
from pydantic import BaseModel


class AuditViolation(BaseModel):
    """A single failed audit check."""
    kind: Literal[
        "uncited_number",        # numeric literal with no marker
        "run_id_not_found",      # [run:X#...] but X not in session run-log
        "value_mismatch",        # prose number ≠ run-log value (within rounding)
        "json_path_not_found",   # JSON-path not resolvable in run-log entry
        "claim_not_found",       # [claim:X] but X not in session claims
        "claim_bad_status",      # claim exists but status not in allowed set
        "malformed_marker",      # marker present but syntax is wrong
        "lit_unresolvable",      # [lit:<hash>] hash not found in passage index
        "hollow_evidence",       # marker resolves to a run with no recorded outputs
        "unit_mismatch",         # prose unit differs from the unit the run declares
        "no_observed_lineage",   # advisory: the cited run's record shows no path to observed data
    ]
    text_excerpt: str            # the prose snippet containing the violation
    marker_raw: str | None       # the raw marker string, if any
    prose_value: str | None      # the numeric literal as found in text
    stored_value: Any | None     # what the run-log actually says (for mismatches)
    message: str                 # human-readable explanation (also shown to LLM)
    fix_hint: str                # instruction to the LLM on how to correct it
    # Added in the token-typed grammar (all optional: older consumers ignore them).
    severity: Literal["hard", "advisory"] = "hard"   # hard blocks; advisory only informs
    category: str | None = None  # quantity | sample_size | method_parameter | None
    suggested_marker: str | None = None  # a unique value-match the agent can paste
    resolved_path: str | None = None     # storage path an aliased marker resolved to


class AuditReport(BaseModel):
    """
    Full audit result for one call to write_research_interpretation.

    passed = True only when there is no HARD violation (advisories never block).
    violations are sorted hard-first; every violation carries a severity.
    numeric_coverage = (cited_count / total_count) or 1.0 if total_count == 0.

    lit_span_count     — number of [lit:...] markers found in prose.
    lit_resolved_count — subset whose tag matched a passage in the index.
    lit_advisories     — non-blocking notices for unresolvable hash lits
                         (the number IS cited; only verification fails).
    """
    passed: bool
    violations: list[AuditViolation] = []
    numeric_coverage: float          # 0.0 – 1.0
    total_numeric_count: int
    cited_numeric_count: int
    claim_count: int
    claim_pass_count: int
    lit_span_count: int = 0
    lit_resolved_count: int = 0
    lit_advisories: list[AuditViolation] = []
    hard_count: int = 0
    advisory_count: int = 0
    # Tokens the grammar deliberately did not treat as quantities, by reason
    # (identifier, code, date, year, meta_count, ...). Transparency, not findings.
    exempt_count: int = 0
    exempt_by_reason: dict[str, int] = {}
    # Lineage check (advisory; never changes `passed`). Distinct cited runs examined, how many were
    # flagged `no_observed_lineage`, and how many could not be judged (unsealed or unresolved rows,
    # or a run whose record simply declares no parent).
    # One advisory per flagged run id, kept apart from `violations` so `advisory_count` stays what it was.
    lineage_advisories: list[AuditViolation] = []
    lineage_checked_count: int = 0
    lineage_flagged_count: int = 0
    lineage_unknown_count: int = 0

    def teaching_error(self) -> str:
        """
        Produce the teaching-error string injected into write_research_interpretation
        refusals — lists every violation with its fix_hint.
        """
        hard = [v for v in self.violations if v.severity == "hard"]
        lines = [
            f"Audit failed: {len(hard)} hard violation(s) found"
            f" ({len(self.violations) - len(hard)} advisory).",
            f"Numeric coverage: {self.cited_numeric_count}/{self.total_numeric_count}.",
            "",
            "Fix each item below, then re-call write_research_interpretation:",
        ]
        for i, v in enumerate(self.violations, 1):
            lines.append(f"\n[{i}] {v.kind.upper()} ({v.severity})")
            lines.append(f"    Excerpt : {v.text_excerpt!r}")
            if v.marker_raw:
                lines.append(f"    Marker  : {v.marker_raw}")
            if v.prose_value:
                lines.append(f"    Value   : {v.prose_value}")
            if v.stored_value is not None:
                lines.append(f"    Stored  : {v.stored_value}")
            lines.append(f"    Problem : {v.message}")
            if v.suggested_marker:
                lines.append(f"    Suggest : {v.suggested_marker}")
            lines.append(f"    Fix     : {v.fix_hint}")
        return "\n".join(lines)
