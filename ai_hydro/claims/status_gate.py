"""Evidence rules on a claim's status, shared by every path that sets or reads one.

Rules, beside the uncertainty rule in ``ai_hydro.mcp.tools_ledger``:

* **Evidence required (``evidence_gate``).** ``supported`` and ``weakly_supported`` need at least one
  evidence span, and every span must be real. What is checked, per ``source_type``:

  - ``run``: ``source_id`` is non-empty; the run exists in the session's run log; it is not hollow (no
    ``error``, not a ``minimal`` middleware row, non-empty ``key_outputs``); and when ``metric_ref`` is
    given, that metric is among the run's outputs (or its uncertainty entries).
  - ``paper`` / ``dataset``: ``source_id`` is non-empty. There is no local registry to resolve them
    against here, so nothing more is checked at status time (promotion checks further).

* **Forcing-dependent comparison (``forcing_scope_gate`` / ``comparison_gate``), structural.** A
  ``supported``/``weakly_supported`` claim is blocked when it is a *comparison against a reference*
  and the interval it rests on excludes the forcing product:

  - the claim is a comparison claim: it names the reference it compares against in ``comparison_ref``
    (a run that merely carries a ``reference_comparison`` block does not make a plain claim one), and
  - that metric's CI declares ``scope.excludes`` containing ``forcing_product``
    (documented shape ``{"varies": [...], "assumes": [...], "excludes": [...]}``).

  When the ``reference_comparison`` entry for the cited metric says ``can_claim_difference: false`` and the
  claim is a comparison claim (``comparison_ref`` set), that is honoured directly (``comparison_gate``);
  a plain claim citing the same metric ("mean flow was 2.0 m3/s") is not affected. Nothing here is keyed on a tool or metric name.
  **The gate is inert until the producers (aihydro-watershed 0.2.0 / Wave 3 PR-4b) emit the scope and
  ``reference_comparison`` outputs**; absent or free-text scope metadata never triggers it.

* **Prose hint (advisory only).** A claim whose wording asserts a difference from a reference while its
  cited CI excludes the forcing product gets a non-blocking warning (``forcing_prose_warnings``). The
  wording heuristic is never a gate by itself.

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
    r"inconsistent|diverg\w+|(?:more|less)\s+than|above|below)\b"
    r"|\b(?:do(?:es)?\s*n(?:o|')t|did\s*n(?:o|')t|cannot|can't|fails?\s+to)\s+(?:agree|match|equal|reproduce|coincide)\b"
    r"|\b(?:do(?:es)?|did)\s+not\s+(?:agree|match|equal|reproduce|coincide)\b",
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


def _read(reader: Callable[[str, str], dict | None] | None, session_id: str, run_id: str) -> dict | None:
    try:
        row = (reader or _default_run_reader)(session_id, run_id)
    except Exception:
        return None
    return row if isinstance(row, dict) else None


def _names(obj: Any, out: set[str], depth: int = 0) -> set[str]:
    """Every key (any depth, capped) of a nested output dict, normalised to its leaf name."""
    if isinstance(obj, dict) and depth < 4:
        for k, v in obj.items():
            out.add(str(k))
            _names(v, out, depth + 1)
    return out


def span_problems(session_id: str, span: Any,
                  run_reader: Callable[[str, str], dict | None] | None = None) -> list[str]:
    """Why one evidence span is not real evidence; empty when it is (see the module docstring)."""
    if not isinstance(span, dict):
        return ["span is not an object"]
    sid = str(span.get("source_id") or "").strip()
    if not sid:
        return ["empty source_id"]
    if span.get("source_type", "run") != "run":
        return []
    row = _read(run_reader, session_id, sid)
    if row is None:
        return [f"run '{sid}' does not exist in this session's run log"]
    if row.get("error") or row.get("minimal") or not row.get("key_outputs"):
        return [f"run '{sid}' is hollow (error, middleware-only or no key_outputs)"]
    ref = span.get("metric_ref")
    if ref:
        leaf = _metric_leaf(ref)
        have = _names(row.get("key_outputs"), set())
        unc = (row.get("evidence") or {}).get("uncertainty") if isinstance(row.get("evidence"), dict) else None
        if isinstance(unc, dict):
            have |= set(map(str, unc))
        if leaf not in have:
            return [f"run '{sid}' has no metric '{leaf}' (it has: {', '.join(sorted(have)[:8]) or 'none'})"]
    return []


def _find_key(obj: Any, key: str, depth: int = 0) -> Any:
    if isinstance(obj, dict) and depth < 4:
        if key in obj:
            return obj[key]
        for v in obj.values():
            hit = _find_key(v, key, depth + 1)
            if hit is not None:
                return hit
    return None


def reference_comparison(row: dict | None) -> Any:
    """The run's ``reference_comparison`` output, wherever the producer recorded it, else ``None``."""
    if not isinstance(row, dict):
        return None
    for holder in (row.get("key_outputs"), (row.get("evidence") or {}).get("data")
                   if isinstance(row.get("evidence"), dict) else None, row.get("evidence")):
        hit = _find_key(holder, "reference_comparison")
        if hit is not None:
            return hit
    return None


def comparison_entry(rc: Any, leaf: str) -> dict | None:
    """The per-metric entry of a ``reference_comparison``: ``{leaf: {...}}``, ``{"table"|"signatures"|
    "metrics": {leaf: {...}}}`` or a list of ``{"metric"|"signature"|"name": leaf, ...}``."""
    if isinstance(rc, dict):
        if isinstance(rc.get(leaf), dict):
            return rc[leaf]
        for k in ("table", "signatures", "metrics", "rows"):
            hit = comparison_entry(rc.get(k), leaf)
            if hit is not None:
                return hit
    elif isinstance(rc, list):
        for item in rc:
            if isinstance(item, dict) and leaf in (item.get("metric"), item.get("signature"), item.get("name")):
                return item
    return None


def _is_comparison_claim(claim_dict: dict) -> bool:
    """A comparison claim names the reference it compares against (``comparison_ref``)."""
    return bool(str(claim_dict.get("comparison_ref") or "").strip())


def _ci(row: dict | None, leaf: str) -> Any:
    unc = ((row or {}).get("evidence") or {}).get("uncertainty") if isinstance((row or {}).get("evidence"), dict) else None
    return unc.get(leaf) if isinstance(unc, dict) else None


def forcing_scoped_metrics(session_id: str, claim_dict: dict,
                           run_reader: Callable[[str, str], dict | None] | None = None,
                           *, structural_only: bool = True) -> list[dict]:
    """Cited run metrics whose CI excludes the forcing product: ``[{metric_ref, run_id, comparison}]``.

    ``comparison`` is True only when the claim itself is a comparison claim (``comparison_ref``): a run that
    carries a ``reference_comparison`` block does not make a plain claim citing it a comparison.
    With ``structural_only`` only comparison claims' metrics are returned; without it every forcing-scoped metric."""
    found: list[dict] = []
    claim_cmp = _is_comparison_claim(claim_dict)
    for span in claim_dict.get("evidence_spans") or []:
        if not (isinstance(span, dict) and span.get("source_type", "run") == "run"
                and span.get("metric_ref") and span.get("source_id")):
            continue
        row = _read(run_reader, session_id, span["source_id"])
        leaf = _metric_leaf(span["metric_ref"])
        if FORCING_EXCLUSION not in scope_excludes(_ci(row, leaf)):
            continue
        comparison = claim_cmp
        if comparison or not structural_only:
            found.append({"metric_ref": span["metric_ref"], "run_id": span["source_id"], "comparison": comparison})
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


def evidence_gate(session_id: str, claim_id: str, claim_dict: dict, status: str,
                  run_reader: Callable[[str, str], dict | None] | None = None) -> dict | None:
    """Refusal when ``status`` is supported/weakly_supported and the evidence is absent or not real."""
    if status not in EVIDENCE_STATUSES:
        return None
    spans = claim_dict.get("evidence_spans") or []
    problems = [{"index": i, "problem": p} for i, sp in enumerate(spans)
                for p in span_problems(session_id, sp, run_reader)]
    if spans and not problems:
        return None
    fix = (
        "1. Keep the claim 'proposed' or 'tested' meanwhile.\n"
        "2. Run the analysis that produces the number and cite that run: add_claim "
        "evidence_spans=[{\"source_type\": \"run\", \"source_id\": \"<run_id from the tool response>\", "
        "\"metric_ref\": \"<metric the run reports>\"}]. Paper or dataset evidence uses source_type "
        "'paper' or 'dataset' with a non-empty source_id.\n"
        "3. Then set the status again."
    )
    if not spans:
        return _refusal("evidence_gate", claim_id, claim_dict, status, "supported_claims_require_evidence",
                        f"A claim cannot be '{status}' with no evidence span: nothing in the session shows "
                        "what supports it.", fix)
    return _refusal("evidence_gate", claim_id, claim_dict, status, "supported_claims_require_real_evidence",
                    f"A claim cannot be '{status}' on evidence that does not resolve: "
                    + "; ".join(f"span {p['index']}: {p['problem']}" for p in problems) + ".",
                    fix, span_problems=problems)


def forcing_scope_gate(session_id: str, claim_id: str, claim_dict: dict, status: str,
                       run_reader: Callable[[str, str], dict | None] | None = None) -> dict | None:
    """Structural refusal of reference-comparison claims that the cited evidence cannot carry."""
    if status not in EVIDENCE_STATUSES:
        return None
    # A producer that says the difference cannot be claimed is believed, whatever the CI scope, but only
    # for a claim that makes a comparison: a plain statement of the value is not what the flag is about.
    for span in (claim_dict.get("evidence_spans") or []) if _is_comparison_claim(claim_dict) else []:
        if not (isinstance(span, dict) and span.get("source_type", "run") == "run"
                and span.get("metric_ref") and span.get("source_id")):
            continue
        row = _read(run_reader, session_id, span["source_id"])
        entry = comparison_entry(reference_comparison(row), _metric_leaf(span["metric_ref"]))
        if isinstance(entry, dict) and entry.get("can_claim_difference") is False:
            return _refusal(
                "comparison_gate", claim_id, claim_dict, status, "producer_says_difference_not_claimable",
                f"The reference comparison for {span['metric_ref']} in run {span['source_id']} reports "
                "can_claim_difference=false: the producer found the comparison not comparable (period, "
                "definition, area or forcing differ), so no difference from the reference may be claimed as "
                f"'{status}'.",
                "1. Record the claim as 'tested' or 'inconclusive' and quote the comparison's own caveats; or\n"
                "2. Make the quantities comparable (same period/definition) and re-run the comparison.",
                metrics=[{"metric_ref": span["metric_ref"], "run_id": span["source_id"]}])
    metrics = forcing_scoped_metrics(session_id, claim_dict, run_reader)
    if not metrics:
        return None
    names = ", ".join(sorted({m["metric_ref"] for m in metrics}))
    return _refusal(
        "forcing_scope_gate", claim_id, claim_dict, status, "difference_from_reference_needs_forcing_in_scope",
        f"This claim compares against a reference and rests on the confidence interval for {names}, which "
        "declares scope.excludes 'forcing_product': it covers sampling variability only, not the "
        "precipitation/forcing product or its error. A difference from a reference cannot be "
        f"'{status}' on that interval alone; the gap may be forcing, period or definition rather than a "
        "property of the basin.",
        "1. Record the claim as 'tested' or 'inconclusive' and state that the interval excludes the "
        "forcing product; or\n"
        "2. Reword it as a statement about the sampled period only (no difference from the reference); or\n"
        "3. Add evidence whose uncertainty scope includes the forcing product (e.g. a forcing-product "
        "spread), cite it in evidence_spans, then set the status again.",
        metrics=metrics,
    )


def forcing_prose_warnings(session_id: str, claim_dict: dict,
                           run_reader: Callable[[str, str], dict | None] | None = None) -> list[dict]:
    """Advisory only: wording asserts a difference from a reference while the cited CI excludes forcing."""
    if claim_dict.get("status") not in EVIDENCE_STATUSES or not asserts_reference_difference(_claim_text(claim_dict)):
        return []
    metrics = forcing_scoped_metrics(session_id, claim_dict, run_reader, structural_only=False)
    if not metrics:
        return []
    return [{
        "code": "FORCING_SCOPE_PROSE", "severity": "warning", "blocking": False, "metrics": metrics,
        "message": "The wording asserts a difference from a reference, but the cited interval "
                   "excludes the forcing product (sampling variability only).",
        "how_to_fix": "State that the interval excludes forcing/period/definition differences, or cite evidence "
                      "whose uncertainty scope includes the forcing product.",
    }]


def status_gate(session_id: str, claim_id: str, claim_dict: dict, status: str,
                run_reader: Callable[[str, str], dict | None] | None = None) -> dict | None:
    """The evidence rules for setting ``status`` (the uncertainty rule is applied by the caller first)."""
    return (evidence_gate(session_id, claim_id, claim_dict, status, run_reader)
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
