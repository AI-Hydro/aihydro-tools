"""
AI-Hydro Answer Auditor MCP tools.

Tier 1 — scientific output gating:
  audit_interpretation   verify synthesis prose against the session evidence ledger

These tools are hot (full schema always in context) because they are called
in every research session that produces a write_research_interpretation result.
"""
from __future__ import annotations

import logging

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _resolve_session, _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp.audit")


COMPLETION_OUTCOMES = ("done", "partial", "infeasible")
_MAX_UNMET = 10
_MAX_UNMET_CHARS = 200


def record_completion_outcome(outcome: str | None, unmet: list[str] | None) -> dict | None:
    """Validate the task's completion outcome and seal it in this call's run record (``record.extra``).

    ``None`` when no outcome was given (older callers). An invalid value is a plain error, not a guess.
    """
    if outcome is None:
        if unmet:
            return {"error": True, "code": "INVALID_PARAMETER",
                    "message": "unmet was given without outcome.",
                    "recovery": f"Pass outcome as one of {list(COMPLETION_OUTCOMES)}."}
        return None
    if outcome not in COMPLETION_OUTCOMES:
        return {"error": True, "code": "INVALID_PARAMETER",
                "message": f"outcome must be one of {list(COMPLETION_OUTCOMES)}, got {outcome!r}.",
                "recovery": "done: all of it. partial: some of it. infeasible: it could not be done."}
    full = [str(u).strip() for u in (unmet or []) if str(u).strip()]
    items = [u[:_MAX_UNMET_CHARS] for u in full][:_MAX_UNMET]
    truncated = len(full) > _MAX_UNMET or any(len(u) > _MAX_UNMET_CHARS for u in full[:_MAX_UNMET])
    if outcome == "done" and items:
        return {"error": True, "code": "INVALID_PARAMETER",
                "message": "outcome 'done' cannot list unmet items.",
                "recovery": "Use outcome 'partial' (or 'infeasible') when something was not done."}
    from ai_hydro.session import run_records

    run_records.declare_lineage(
        completion_outcome=outcome, **({"completion_unmet": items} if items else {}),
        **({"completion_unmet_truncated": True, "completion_unmet_total": len(full)} if truncated else {}))
    return {"outcome": outcome, **({"unmet": items} if items else {}),
            **({"unmet_truncated": True, "unmet_total": len(full)} if truncated else {})}


@mcp.tool()
def audit_interpretation(
    session_id: str | None = None,
    prose: str = "",
    outcome: str | None = None,
    unmet: list[str] | None = None,
) -> dict:
    """
    Verify that every quantity in synthesis prose is bound to a run-log
    entry or a declared literature marker ([lit:...]).

    Ids, codes (HUC/EPSG), dates, years and plain counts ("17 signatures") are
    not quantities. Decimals, numbers with units and sample sizes ("7,305 daily
    observations") need a marker in the same sentence/bullet/cell; method
    parameters ("bootstrap 90% CIs") are advisory.

    Checks each [run:<id>#<path>] marker:
      - run_id exists in session run-log
      - JSON-path resolves (canonical key_outputs.<field>; data.<field> and
        evidence.data.<field> are aliases); the run has outputs (hollow_evidence)
      - prose value matches stored value within rounding tolerance
      - prose unit matches the run's declared unit, if any (unit_mismatch)

    Checks each [claim:<id>] marker:
      - claim exists in session ledger
      - claim status ∈ {tested, supported, weakly_supported}

    Advisory only: a cited run whose record shows no path to observed data is listed once
    in lineage_advisories (not in violations or advisory_count). A run that declares no parent is not called fabricated; see
    lineage_unknown_count.

    outcome: "done", "partial" or "infeasible" (could not be done); unmet lists what was not done.
    Sealed in this call's run record; it does not change the audit.

    Returns {passed, violations, numeric_coverage, ...}.

    Tier 1 — enforcement runs post_run() (quality_flags, _run_id) automatically.
    """
    try:
        # An audit judges one study's prose: never guess the study ("most recent on disk").
        session_id = _resolve_session(
            session_id, None, allow_auto_create=False, allow_recent_fallback=False, must_exist=True
        )

        if not prose or not prose.strip():
            return {
                "error": "prose is empty — nothing to audit.",
                "_note": "Pass the synthesis text as the 'prose' parameter.",
            }

        from ai_hydro.audit import audit_prose
        done = record_completion_outcome(outcome, unmet)
        if done is not None and "error" in done:
            return done
        report = audit_prose(prose, session_id)

        violations_raw = [v.model_dump() for v in report.violations]

        result = {
            "session_id": session_id,
            "passed": report.passed,
            "numeric_coverage": report.numeric_coverage,
            "total_numeric_count": report.total_numeric_count,
            "cited_numeric_count": report.cited_numeric_count,
            "claim_count": report.claim_count,
            "claim_pass_count": report.claim_pass_count,
            "violations": violations_raw,        # hard first; each has a severity
            "hard_count": report.hard_count,
            "advisory_count": report.advisory_count,
            "exempt_by_reason": report.exempt_by_reason,
            "lineage_advisories": [v.model_dump() for v in report.lineage_advisories],
            "lineage_checked_count": report.lineage_checked_count,
            "lineage_flagged_count": report.lineage_flagged_count,
            "lineage_unknown_count": report.lineage_unknown_count,
        }
        if done:
            result["completion_outcome"] = done

        if not report.passed:
            result["_note"] = (
                f"Audit FAILED ({report.hard_count} hard violation(s), "
                f"{report.advisory_count} advisory). "
                "Fix all violations, then re-call write_research_interpretation. "
                "See 'violations' list for exact fix instructions."
            )
            result["teaching_error"] = report.teaching_error()
        else:
            result["_note"] = (
                f"Audit passed. {report.cited_numeric_count}/{report.total_numeric_count} "
                "quantities cited"
                + (f" ({report.advisory_count} advisory)" if report.advisory_count else "")
                + ". Proceed with write_research_interpretation."
            )

        return result

    except Exception as e:
        log.error("audit_interpretation failed: %s", e)
        return _tool_error_to_dict(e)
