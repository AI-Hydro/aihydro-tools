"""Test-only stand-in for a researcher running ``aihydro-approve`` (ADR-002a).

Goes through the same store API and the same revision computation as the CLI
(claim fields plus retained-evidence fingerprints). The session must already be
saved and ``SESSIONS_DIR`` must point at the test's temp dir. Records land in
the per-test isolated ``AIHYDRO_HOME`` (see ``conftest.py``).
"""
from __future__ import annotations


def seed_run(session_id: str, run_id: str = "r1", outputs: dict | None = None, **row) -> None:
    """Store a real (non-hollow) run-log row, as a finished tool run leaves: what evidence spans must cite.
    ``SESSIONS_DIR`` must already point at the test's temp dir."""
    from ai_hydro.session.store import HydroSession

    s = HydroSession.load(session_id)
    s.set("_run_log", {run_id: {"run_id": run_id, "session_id": session_id, "tool_name": "fixture",
                                "timestamp": "2026-10-01T00:00:00+00:00",
                                "key_outputs": outputs or {"kge": 0.8, "nse": 0.8}, **row}})
    s.save()


def basin_ref_full(label: str = "synthetic") -> dict:
    """The full BasinRef dict behind ``basin_ref(label)``: what a delineation retains."""
    from aihydro_core.records.place import BasinAnchor, BasinRef
    return BasinRef(anchor=BasinAnchor("network_element", "test-network", "1", label),
                    method="test_fixture").to_dict()


def retained(label: str = "synthetic") -> dict:
    """``{id: full ref}`` for a claim dict's ``basin_ref_records`` (promotion re-verifies it)."""
    full = basin_ref_full(label)
    return {full["id"]: full}


def basin_ref(label: str = "synthetic") -> dict:
    """A well-formed ``ClaimScope.basin_refs`` entry (slice 3): promotion of a basin-scoped
    claim now requires one. Test setup only; the id is derived, not minted by a delineation."""
    from aihydro_core.records.place import BasinAnchor, basin_id_from_anchor
    return {"id": basin_id_from_anchor(BasinAnchor("network_element", "test-network", "1", label)),
            "label": label}


def approve(session_id: str, claim_id: str, approver: str = "test-researcher") -> dict:
    """Record a human approval for the claim's CURRENT revision (claim + evidence)."""
    from aihydro_core.records import Actor

    from ai_hydro.approval.records import session_claim_revision
    from ai_hydro.approval.writer import write_approval
    from ai_hydro.session.store import HydroSession

    *_, rev = session_claim_revision(HydroSession.load(session_id), claim_id)
    return write_approval(session_id, claim_id, rev,
                          Actor(kind="human", id=approver), "test fixture approval")
