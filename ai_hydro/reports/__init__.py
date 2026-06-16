"""
Research output reports for AI-Hydro sessions.

Public API:
    build_defensibility_report(session, session_id, today) → (markdown, summary)
"""
from ai_hydro.reports.defensibility import build_defensibility_report

__all__ = ["build_defensibility_report"]
