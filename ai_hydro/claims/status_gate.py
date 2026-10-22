"""Evidence rules on a claim's status, shared by every path that sets or reads one.

Two rules live here beside the uncertainty rule in ``ai_hydro.mcp.tools_ledger``:

* **Evidence required.** ``supported`` and ``weakly_supported`` need at least one evidence span.
* **Forcing-dependent comparison.** When the confidence interval behind a cited metric declares
  ``scope.excludes`` containing ``forcing_product``, the interval says nothing about the
  precipitation/forcing product behind the number. A claim asserting that the metric *differs
  from a reference* cannot reach ``supported``/``weakly_supported`` on that interval alone.

The scope shape is the documented machine-readable one
``{"varies": [...], "assumes": [...], "excludes": [...]}``. The rule is keyed on that metadata
only, never on a tool or metric name. A scope that is absent, or a free-text string from an older
release, never triggers it (nothing is known, so nothing is claimed).

Stored claims are never rewritten: ``stored_claim_flags`` reports the ones that break a rule so
``start_session`` can show them.
"""
from __future__ import annotations

import re
from typing import Any, Callable

#: Statuses that assert the evidence carries the claim.
EVIDENCE_STATUSES = ("supported", "weakly_supported")

FORCING_EXCLUSION = "forcing_product"

#: Words by which prose asserts a difference / bias / disagreement ...
_DIFFERENCE = re.compile(
    r"\b(?:differs?|differed|differing|different|differences?|deviat\w+|disagree\w*|discrepan\w+|"
    r"higher|lower|greater|larger|smaller|exceed\w*|overestimat\w+|underestimat\w+|bias(?:ed|es)?|"
    r"inconsistent|diverg\w+|(?:more|less)\s+than|above|below)\b",
    re.IGNORECASE,
)
#: ... measured against a reference of some kind.
_REFERENCE = re.compile(
    r"\b(?:reference|benchmark|published|catalog(?:ue)?|baseline|literature|dataset|official|"
    r"compared?\s+(?:to|with)|relative\s+to|versus|vs\.?|against|CAMELS)\b",
    re.IGNORECASE,
)
#: A negation just before the term makes it a disclosure of agreement, not an asserted difference.
_NEGATED = re.compile(r"\b(?:no|not|without|never|neither|nor|cannot|can't|isn't|doesn't|does\s+not)\b[^.;\n]{0,40}$",
                      re.IGNORECASE)


def asserts_reference_difference(text: str) -> bool:
    """Heuristic, cheap and deliberately conservative: does the prose assert a difference from a reference?"""
    if not text or not _REFERENCE.search(text):
        return False
    for m in _DIFFERENCE.finditer(text):
        clause_start = max(text.rfind(c, 0, m.start()) for c in ".;\n") + 1
        if _NEGATED.search(text[clause_start:m.start()]):
            continue
        return True
    return False


def _metric_leaf(ref: str) -> str:
    parts = [p for p in re.split(r"[.#/]", str(ref)) if p]
    leaf = parts[-1] if parts else str(ref)
    return re.sub(r"\[[^\]]*\]$", "", leaf)


def _norm(token: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(token).lower()).strip("_")


def scope_excludes(ci: Any) -> set[str]:
    """Normalised ``scope.excludes`` of one CI entry; empty when the scope is absent or not the dict shape."""
    scope = ci.get("scope") if isinstance(ci, dict) else None
    excl = scope.get("excludes") if isinstance(scope, dict) else None
    return {_norm(e) for e in excl} if isinstance(excl, (list, tuple, set)) else set()


def _default_run_reader(session_id: str, run_id: str) -> dict | None:
    from ai_hydro.session.store import _run_log_read_one
    return _run_log_read_one(session_id, run_id)


def forcing_scoped_metrics(session_id: str, claim_dict: dict,
                           run_reader: Callable[[str, str], dict | None] | None = None) -> list[dict]:
    """Cited run metrics whose CI excludes the forcing product: ``[{metric_ref, run_id}]``."""
    reader = run_reader or _default_run_reader
    found: list[dict] = []
    for span in claim_dict.get("evidence_spans") or []:
        if not (isinstance(span, dict) and span.get("source_type", "run") == "run"
                and span.get("metric_ref") and span.get("source_id")):
            continue
        row = reader(session_id, span["source_id"])
        unc = ((row or {}).get("evidence") or {}).get("uncertainty") if isinstance(row, dict) else None
        ci = unc.get(_metric_leaf(span["metric_ref"])) if isinstance(unc, dict) else None
        if FORCING_EXCLUSION in scope_excludes(ci):
            found.append({"metric_ref": span["metric_ref"], "run_id": span["source_id"]})
    return found


def _claim_text(claim_dict: dict) -> str:
    return f"{claim_dict.get('claim', '')}\n{claim_dict.get('confidence_rationale', '')}"


def _refusal(code: str, claim_id: str, claim_dict: dict, status: str, rule: str,
             explanation: str, how_to_fix: str, **extra: Any) -> dict:
    return {
        "error": code,
        "claim_id": claim_id,
        "claim_type": claim_dict.get("claim_type", ""),
        "requested_status": status,
        **extra,
        "teaching_error": {"rule": rule, "explanation": explanation, "how_to_fix": how_to_fix},
    }


def evidence_gate(claim_id: str, claim_dict: dict, status: str) -> dict | None:
    """Refusal when ``status`` is supported/weakly_supported and the claim has no evidence span."""
    if status not in EVIDENCE_STATUSES or claim_dict.get("evidence_spans"):
        return None
    return _refusal(
        "evidence_gate", claim_id, claim_dict, status, "supported_claims_require_evidence",
        f"A claim cannot be '{status}' with no evidence span: nothing in the session shows what "
        "supports it.",
        "1. Record the claim as 'proposed' or 'tested' (or keep it so).\n"
        "2. Attach at least one evidence span (add_claim evidence_spans=[{run_id, metric_ref}], or a "
        "literature passage via source_type='lit') pointing at the run that produced the number.\n"
        "3. Then set the status again.",
    )


def forcing_scope_gate(session_id: str, claim_id: str, claim_dict: dict, status: str,
                       run_reader: Callable[[str, str], dict | None] | None = None) -> dict | None:
    """Refusal when a reference-difference claim rests on a CI that excludes the forcing product."""
    if status not in EVIDENCE_STATUSES or not asserts_reference_difference(_claim_text(claim_dict)):
        return None
    metrics = forcing_scoped_metrics(session_id, claim_dict, run_reader)
    if not metrics:
        return None
    names = ", ".join(sorted({m["metric_ref"] for m in metrics}))
    return _refusal(
        "forcing_scope_gate", claim_id, claim_dict, status, "difference_from_reference_needs_forcing_in_scope",
        f"The confidence interval for {names} declares scope.excludes 'forcing_product': it covers "
        "sampling variability only, not the precipitation/forcing product or its error. A claim that "
        f"the metric differs from a reference cannot be '{status}' on that interval alone; the gap may "
        "be forcing, period or definition rather than a property of the basin.",
        "1. Record the claim as 'tested' or 'inconclusive' and state that the interval excludes the "
        "forcing product; or\n"
        "2. Reword it as a statement about the sampled period only (no difference from the reference); or\n"
        "3. Add evidence whose uncertainty scope includes the forcing product (e.g. a forcing-product "
        "spread), cite it in evidence_spans, then set the status again.",
        metrics=metrics,
    )


def status_gate(session_id: str, claim_id: str, claim_dict: dict, status: str,
                run_reader: Callable[[str, str], dict | None] | None = None) -> dict | None:
    """The evidence rules for setting ``status`` (the uncertainty rule is applied by the caller first)."""
    return (evidence_gate(claim_id, claim_dict, status)
            or forcing_scope_gate(session_id, claim_id, claim_dict, status, run_reader))


def stored_claim_flags(session: Any, run_reader: Callable[[str, str], dict | None] | None = None) -> list[dict]:
    """Stored claims that break a status rule. Never rewrites them; the caller only reports."""
    flags: list[dict] = []
    for cid, claim in (getattr(session, "claims", None) or {}).items():
        if not isinstance(claim, dict):
            continue
        status = claim.get("status", "")
        refusal = status_gate(session.session_id, cid, claim, status, run_reader)
        if refusal is not None:
            flags.append({
                "claim_id": cid, "status": status, "code": refusal["error"],
                "rule": refusal["teaching_error"]["rule"],
                "how_to_fix": refusal["teaching_error"]["how_to_fix"],
            })
    return flags
