"""
Skeptic — adversarial second pass over research interpretations.

Checks:
  1. Stale/retracted claims cited in the interpretation text
  2. Scope overreach — gauge IDs mentioned outside any claim's registered scope
  3. High-risk unvalidated assumptions that conflict with supported claims
  4. Registry conflicts — session claim status has degraded below its promoted entry

All findings are advisory; only the auditor is a hard gate.
"""
from ai_hydro.skeptic.checks import run_all_checks
from ai_hydro.skeptic.models import SkepticIssue, SkepticReport

__all__ = ["run_all_checks", "SkepticIssue", "SkepticReport"]
