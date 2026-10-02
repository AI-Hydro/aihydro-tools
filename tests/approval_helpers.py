"""Test-only stand-in for a researcher running ``aihydro-approve`` (ADR-002a).

Goes through the same store API and the same revision computation as the CLI
(claim fields plus retained-evidence fingerprints). The session must already be
saved and ``SESSIONS_DIR`` must point at the test's temp dir. Records land in
the per-test isolated ``AIHYDRO_HOME`` (see ``conftest.py``).
"""
from __future__ import annotations

import os

# Approvals fail closed (ADR-002b): v1 test fixtures need the explicit development opt-out.
# (tests/conftest.py should set this per test too; this keeps importers working meanwhile.)
os.environ["AIHYDRO_REQUIRE_SIGNED"] = "0"


def approve(session_id: str, claim_id: str, approver: str = "test-researcher") -> dict:
    """Record a human approval for the claim's CURRENT revision (claim + evidence)."""
    from aihydro_core.records import Actor

    from ai_hydro.approval.records import session_claim_revision
    from ai_hydro.approval.writer import write_approval
    from ai_hydro.session.store import HydroSession

    *_, rev = session_claim_revision(HydroSession.load(session_id), claim_id)
    return write_approval(session_id, claim_id, rev,
                          Actor(kind="human", id=approver), "test fixture approval")
