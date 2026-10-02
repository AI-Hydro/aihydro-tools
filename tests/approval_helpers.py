"""Test-only stand-in for a researcher running ``aihydro-approve`` (ADR-002a).

Goes through the same store API as the CLI. The session must already be saved
and ``SESSIONS_DIR`` must point at the test's temp dir. Records land in the
per-test isolated ``AIHYDRO_HOME`` (see ``conftest.py``).
"""
from __future__ import annotations


def approve(session_id: str, claim_id: str, approver: str = "test-researcher") -> dict:
    """Record a human approval for the claim's CURRENT revision."""
    from aihydro_core.records import Actor

    from ai_hydro.approval.records import claim_revision_digest
    from ai_hydro.approval.writer import write_approval
    from ai_hydro.session.store import HydroSession

    claim = HydroSession.load(session_id).claims[claim_id]
    return write_approval(session_id, claim_id, claim_revision_digest(claim),
                          Actor(kind="human", id=approver), "test fixture approval")
