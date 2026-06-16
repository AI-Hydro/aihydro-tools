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


@mcp.tool()
def audit_interpretation(
    session_id: str | None = None,
    prose: str = "",
) -> dict:
    """
    Verify that every numeric literal in synthesis prose is bound to a
    run-log entry or a declared whitelist marker ([lit:...]).

    Checks each [run:<id>#<path>] marker:
      - run_id exists in session run-log
      - JSON-path resolves to the stored field
      - prose value matches stored value within rounding tolerance

    Checks each [claim:<id>] marker:
      - claim exists in session ledger
      - claim status ∈ {tested, supported, weakly_supported}

    Returns {passed, violations, numeric_coverage, ...}.

    Tier 1 — enforcement runs post_run() (quality_flags, _run_id) automatically.
    """
    try:
        session_id = _resolve_session(session_id, None, allow_auto_create=False)

        if not prose or not prose.strip():
            return {
                "error": "prose is empty — nothing to audit.",
                "_note": "Pass the synthesis text as the 'prose' parameter.",
            }

        from ai_hydro.audit import audit_prose
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
            "violations": violations_raw,
        }

        if not report.passed:
            result["_note"] = (
                f"Audit FAILED ({len(report.violations)} violation(s)). "
                "Fix all violations, then re-call write_research_interpretation. "
                "See 'violations' list for exact fix instructions."
            )
            result["teaching_error"] = report.teaching_error()
        else:
            result["_note"] = (
                f"Audit passed. {report.cited_numeric_count}/{report.total_numeric_count} "
                "numerics cited. Proceed with write_research_interpretation."
            )

        return result

    except Exception as e:
        log.error("audit_interpretation failed: %s", e)
        return _tool_error_to_dict(e)
