from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class SkepticIssue(BaseModel):
    issue_type: Literal[
        "stale_claim_cited",
        "retracted_claim_cited",
        "scope_overreach",
        "unvalidated_high_risk_assumption",
        "registry_conflict",
    ]
    severity: Literal["advisory", "warning", "error"]
    description: str
    claim_id: str | None = None
    recommendation: str


class SkepticReport(BaseModel):
    passed: bool
    verdict: Literal["clean", "advisory", "flagged"]
    issues: list[SkepticIssue] = []
    n_claims_checked: int = 0
    n_assumptions_checked: int = 0
    session_id: str
    checked_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def advisory_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "advisory")

    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    def teaching_advisory(self) -> str:
        lines = [
            f"Skeptic found {len(self.issues)} issue(s) in the interpretation:\n"
        ]
        for i, issue in enumerate(self.issues, 1):
            lines.append(
                f"  [{i}] {issue.severity.upper()} ({issue.issue_type}): "
                f"{issue.description}\n"
                f"       → {issue.recommendation}"
            )
        return "\n".join(lines)
