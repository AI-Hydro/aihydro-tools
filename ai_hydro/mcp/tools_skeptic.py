"""
Skeptic tool — adversarial second pass over research interpretations.

Runs four deterministic checks (stale citations, scope overreach,
unvalidated high-risk assumptions, registry conflicts) and returns a
SkepticReport.  All findings are advisory; the audit gate in
write_research_interpretation is the only hard gate.
"""
from __future__ import annotations

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict


@mcp.tool()
def run_skeptic(
    session_id: str,
    interpretation_text: str | None = None,
) -> dict:
    """
    Adversarial second pass over a research interpretation.

    Runs four deterministic checks:
      1. Stale/retracted claim markers cited in the text
      2. Gauge IDs mentioned outside any registered claim scope
      3. High-risk unvalidated assumptions alongside supported claims
      4. Registry conflicts (session status degraded below promoted entry)

    Returns a SkepticReport with verdict in {clean, advisory, flagged}.
    All findings are advisory — the audit gate is the only hard refusal.

    interpretation_text: prose to check. If omitted, uses session.interpretation.
    """
    try:
        from ai_hydro.session.store import HydroSession
        from ai_hydro.skeptic import run_all_checks

        session = HydroSession.load(session_id)
        text = (interpretation_text or session.interpretation or "").strip()
        report = run_all_checks(session, text)
        result = report.model_dump()
        result["_note"] = (
            f"Skeptic: verdict={report.verdict}, "
            f"{len(report.issues)} issue(s) across "
            f"{report.n_claims_checked} claims / "
            f"{report.n_assumptions_checked} assumptions."
        )
        if report.issues:
            result["advisory"] = report.teaching_advisory()
        return result
    except Exception as exc:
        return _tool_error_to_dict(exc)
