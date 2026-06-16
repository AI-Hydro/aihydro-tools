"""
Deterministic skeptic checks — no NLI, no external calls.

Each check takes a session (HydroSession) and optional interpretation text,
and returns a list[SkepticIssue].
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ai_hydro.skeptic.models import SkepticIssue, SkepticReport

if TYPE_CHECKING:
    from ai_hydro.session.store import HydroSession

# Regex for USGS 8-digit gauge IDs (e.g. 01013500, 09380000)
_GAUGE_RE = re.compile(r"\b(\d{8})\b")

# Claim marker pattern: [claim:some-id]
_CLAIM_MARKER_RE = re.compile(r"\[claim:([^\]]+)\]")


# ---------------------------------------------------------------------------
# Check 1 — Stale / retracted claims cited in the interpretation text
# ---------------------------------------------------------------------------

def check_stale_claims_cited(
    session: "HydroSession",
    text: str,
) -> list[SkepticIssue]:
    issues: list[SkepticIssue] = []
    cited_ids = {m.group(1) for m in _CLAIM_MARKER_RE.finditer(text)}
    for claim_id in cited_ids:
        claim_dict = session.claims.get(claim_id)
        if not claim_dict:
            continue
        status = claim_dict.get("status", "")
        if status == "stale":
            issues.append(SkepticIssue(
                issue_type="stale_claim_cited",
                severity="warning",
                description=(
                    f"Claim '{claim_id}' is cited in the interpretation but "
                    f"has status 'stale' — its source evidence has changed since "
                    f"the claim was last reviewed."
                ),
                claim_id=claim_id,
                recommendation=(
                    "Re-run check_registry_staleness and update or retract the "
                    f"claim before citing it. Remove [claim:{claim_id}] until resolved."
                ),
            ))
        elif status == "retracted":
            issues.append(SkepticIssue(
                issue_type="retracted_claim_cited",
                severity="error",
                description=(
                    f"Claim '{claim_id}' is cited in the interpretation but has "
                    f"status 'retracted' and must not appear in published outputs."
                ),
                claim_id=claim_id,
                recommendation=(
                    f"Remove [claim:{claim_id}] from the interpretation. "
                    "A retracted claim cannot support any published conclusion."
                ),
            ))
    return issues


# ---------------------------------------------------------------------------
# Check 2 — Scope overreach: gauge IDs in text not covered by any claim scope
# ---------------------------------------------------------------------------

def check_scope_overreach(
    session: "HydroSession",
    text: str,
) -> list[SkepticIssue]:
    issues: list[SkepticIssue] = []
    if not text:
        return issues

    # Collect all gauge IDs covered by at least one claim scope
    covered: set[str] = set()
    for claim_dict in session.claims.values():
        scope = claim_dict.get("scope", {})
        for basin in scope.get("basins", []):
            covered.add(str(basin).strip())

    # Skip the check entirely if no claims have basin scopes
    if not covered:
        return issues

    # Gauge IDs mentioned in the interpretation text
    mentioned = {m.group(1) for m in _GAUGE_RE.finditer(text)}
    overreach = mentioned - covered

    for gauge_id in sorted(overreach):
        issues.append(SkepticIssue(
            issue_type="scope_overreach",
            severity="warning",
            description=(
                f"Gauge '{gauge_id}' is mentioned in the interpretation but is "
                f"not covered by any registered claim scope. Statements about "
                f"this gauge are not backed by a claim in this session."
            ),
            recommendation=(
                f"Either add a claim with scope.basins containing '{gauge_id}', "
                "or remove the reference from the interpretation."
            ),
        ))
    return issues


# ---------------------------------------------------------------------------
# Check 3 — High-risk unvalidated assumptions vs supported claims
# ---------------------------------------------------------------------------

def check_assumption_violations(session: "HydroSession") -> list[SkepticIssue]:
    issues: list[SkepticIssue] = []
    supported_claims = [
        c for c in session.claims.values()
        if c.get("status") in ("supported", "weakly_supported")
    ]
    if not supported_claims:
        return issues

    for assumption in session.assumptions.values():
        if assumption.get("risk") != "high":
            continue
        if assumption.get("validated"):
            continue
        # A high-risk unvalidated assumption exists alongside supported claims
        issues.append(SkepticIssue(
            issue_type="unvalidated_high_risk_assumption",
            severity="advisory",
            description=(
                f"Assumption '{assumption.get('id', '?')}' — "
                f"\"{assumption.get('statement', '')}\" — is rated high-risk "
                f"and has not been validated, yet the session contains "
                f"{len(supported_claims)} supported claim(s)."
            ),
            recommendation=(
                "Validate or explicitly acknowledge this assumption in the "
                "interpretation's limitations paragraph before publication. "
                "Consider calling add_claim(claim_type='assumption') to surface it."
            ),
        ))
    return issues


# ---------------------------------------------------------------------------
# Check 4 — Registry conflict: session status degraded below promoted entry
# ---------------------------------------------------------------------------

def check_registry_conflicts(session: "HydroSession") -> list[SkepticIssue]:
    issues: list[SkepticIssue] = []
    for claim_dict in session.claims.values():
        if not claim_dict.get("registry_id"):
            continue
        session_status = claim_dict.get("status", "")
        # "promoted" in registry but now stale/retracted/contradicted in session
        if session_status in ("stale", "retracted", "contradicted"):
            claim_id = claim_dict.get("id", claim_dict.get("claim_id", "?"))
            issues.append(SkepticIssue(
                issue_type="registry_conflict",
                severity="warning",
                description=(
                    f"Claim '{claim_id}' was promoted to the global registry "
                    f"(registry_id: {claim_dict['registry_id']}) but its current "
                    f"session status is '{session_status}'. The registry entry "
                    f"and this session are now inconsistent."
                ),
                claim_id=claim_id,
                recommendation=(
                    "Run check_registry_staleness to propagate the status change "
                    "to the registry, or retract the registry entry explicitly."
                ),
            ))
    return issues


# ---------------------------------------------------------------------------
# Aggregate runner
# ---------------------------------------------------------------------------

def run_all_checks(
    session: "HydroSession",
    text: str,
) -> SkepticReport:
    issues: list[SkepticIssue] = []
    issues.extend(check_stale_claims_cited(session, text))
    issues.extend(check_scope_overreach(session, text))
    issues.extend(check_assumption_violations(session))
    issues.extend(check_registry_conflicts(session))

    has_error = any(i.severity == "error" for i in issues)
    has_warning = any(i.severity == "warning" for i in issues)

    if has_error:
        verdict = "flagged"
    elif has_warning or issues:
        verdict = "advisory"
    else:
        verdict = "clean"

    return SkepticReport(
        passed=(verdict == "clean"),
        verdict=verdict,
        issues=issues,
        n_claims_checked=len(session.claims),
        n_assumptions_checked=len(session.assumptions),
        session_id=session.session_id,
    )
