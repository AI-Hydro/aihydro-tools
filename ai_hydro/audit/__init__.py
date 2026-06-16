"""
AI-Hydro Answer Auditor.

Verifies that every quantitative literal in LLM-authored synthesis prose is
bound to a specific run-log entry or a declared whitelist marker.

Three public modules:
  grammar   — marker regex parser (extract markers from prose)
  resolver  — look up markers against session run-log and claims ledger
  models    — AuditReport, AuditViolation (pydantic)

Entry point:
  from ai_hydro.audit import audit_prose
"""
from .grammar import extract_markers, MarkerKind  # noqa: F401
from .resolver import resolve_prose               # noqa: F401
from .models import AuditReport, AuditViolation   # noqa: F401


def audit_prose(prose: str, session_id: str) -> "AuditReport":
    """
    Run a full audit of synthesis prose against the session evidence ledger.

    Returns AuditReport.  Never raises — errors are captured as violations.
    """
    return resolve_prose(prose, session_id)
