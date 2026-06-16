"""
Science Protocol binding for aihydro-tools.

Re-exports the domain-free Protocols from aihydro-core and documents which
aihydro-tools classes satisfy each one via structural subtyping. This module
is the seam: core knows the shape; tools provides the hydrology implementation.

ClaimStore        <- HydroSession (ai_hydro.session.store)
Auditor           <- resolve_prose (ai_hydro.audit.resolver)
UncertaintyProvider <- bootstrap_ci / block_bootstrap_ci (ai_hydro.analysis.uncertainty)

No import cycles: this module only re-exports from aihydro_core and does NOT
import from ai_hydro.session, ai_hydro.audit, or ai_hydro.analysis — those
are the implementations, not the interface definitions.
"""
from __future__ import annotations

from aihydro_core.science import (
    # Claim
    ClaimStatus,
    EvidenceSpan,
    Claim,
    ClaimStore,
    # Audit
    ViolationKind,
    AuditViolationRecord,
    AuditReportRecord,
    Auditor,
    # Uncertainty
    UncertaintyMethod,
    UncertaintyEstimate,
    UncertaintyProvider,
)

__all__ = [
    "ClaimStatus", "EvidenceSpan", "Claim", "ClaimStore",
    "ViolationKind", "AuditViolationRecord", "AuditReportRecord", "Auditor",
    "UncertaintyMethod", "UncertaintyEstimate", "UncertaintyProvider",
]
