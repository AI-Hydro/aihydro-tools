"""
Audit resolver: bind prose markers to evidence and verify values.

Algorithm (from Rules/CITATION_GRAMMAR.md):
  1. Extract all numeric literals and markers from prose.
  2. For each numeric: find the nearest following marker within a window.
  3. [run:id#path]  → load run-log entry, resolve JSON-path, compare.
  4. [claim:id]     → load claim, check status in allowed set.
  5. [lit:tag]      → pass (no verification).
  6. No marker + not whitelisted → violation: uncited_number.
  7. Emit AuditReport.

The resolver is pure lookup — no inference, no LLM calls.
It never raises; all errors are captured as violations.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any

from .grammar import (
    MarkerKind,
    NumericSpan,
    ParsedMarker,
    _context_window,
    _is_whitelisted_standalone,
    extract_markers,
    extract_numerics,
)
from .models import AuditReport, AuditViolation

log = logging.getLogger("ai_hydro.audit.resolver")

# Claim statuses that are acceptable for a cited-as-fact assertion.
_ALLOWED_CLAIM_STATUSES = {"tested", "supported", "weakly_supported"}

# How far (chars) ahead of a numeric we search for its binding marker.
_MARKER_LOOKAHEAD = 150

# Rounding tolerance: prose value within this many ULPs of last digit is a pass.
# E.g. prose "0.82" allows stored 0.815 ≤ v ≤ 0.824999…
_ROUNDING_TOLERANCE_EXTRA = 0.5  # half-unit-of-last-place cushion


def _resolve_json_path(obj: Any, path: str) -> tuple[bool, Any]:
    """
    Walk a dotted JSON path into obj.
    Returns (found: bool, value: Any).
    Works on dicts and lists (integer path segments index lists).
    """
    parts = path.split(".")
    cur = obj
    for part in parts:
        if cur is None:
            return False, None
        if isinstance(cur, dict):
            if part not in cur:
                return False, None
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return False, None
        else:
            return False, None
    return True, cur


def _values_match(prose_raw: str, stored: Any) -> bool:
    """
    Return True if prose_raw (the numeric literal as found in text) is
    consistent with stored value within rounding tolerance.

    Handles: plain floats, percentages (prose "42%" vs stored 0.42 OR 42),
    comma-formatted integers, scientific notation.
    """
    # Clean the prose value
    clean = prose_raw.replace(",", "").replace(" ", "").rstrip("%")
    # Remove unicode/ASCII scientific notation suffix for simpler compare
    clean = re.sub(r"[×x].*", "", clean).strip()
    try:
        prose_float = float(clean)
    except ValueError:
        return False

    # Stored value must be numeric
    try:
        stored_float = float(stored)
    except (TypeError, ValueError):
        return False

    # Compute half-ULP tolerance based on prose precision
    if "." in clean:
        decimals = len(clean.split(".")[1])
        ulp = 10 ** (-decimals)
    else:
        ulp = 1.0
    tolerance = ulp * (0.5 + _ROUNDING_TOLERANCE_EXTRA)

    # If prose had %, try both interpretations: stored as fraction or as percent
    if prose_raw.rstrip().endswith("%"):
        return (
            abs(prose_float - stored_float) <= tolerance
            or abs(prose_float / 100.0 - stored_float) <= tolerance
        )

    return abs(prose_float - stored_float) <= tolerance


def _load_run_log(session_id: str) -> dict:
    """Load the session run-log dict. Returns {} on any error."""
    try:
        from ai_hydro.session.store import HydroSession
        session = HydroSession.load(session_id)
        return dict(session.get("_run_log") or {})
    except Exception as exc:
        log.warning("Could not load run-log for session %s: %s", session_id, exc)
        return {}


def _load_claims(session_id: str) -> dict:
    """Load the session claims dict. Returns {} on any error."""
    try:
        from ai_hydro.session.store import HydroSession
        session = HydroSession.load(session_id)
        return dict(session.claims or {})
    except Exception as exc:
        log.warning("Could not load claims for session %s: %s", session_id, exc)
        return {}


def _find_binding_marker(
    numeric: NumericSpan,
    markers: list[ParsedMarker],
) -> ParsedMarker | None:
    """
    Return the nearest [run:...] or [lit:...] marker that appears
    *after* the numeric literal within _MARKER_LOOKAHEAD chars.
    [claim:...] markers bind sentences, not individual numbers, so they
    are excluded here.
    """
    for marker in markers:
        if marker.kind == MarkerKind.CLAIM:
            continue
        if marker.start >= numeric.end and (marker.start - numeric.end) <= _MARKER_LOOKAHEAD:
            return marker
    return None


def resolve_prose(prose: str, session_id: str) -> AuditReport:
    """
    Full audit: extract numerics + markers, resolve each, emit AuditReport.
    """
    violations: list[AuditViolation] = []

    try:
        markers = extract_markers(prose)
        numerics = extract_numerics(prose)

        run_log = _load_run_log(session_id)
        claims  = _load_claims(session_id)

        # Pre-check whether the passage index exists (used for lit advisory logic)
        _passage_index_exists: bool | None = None

        cited_count = 0
        total_count = len(numerics)
        lit_span_count = 0
        lit_resolved_count = 0
        lit_advisories: list[AuditViolation] = []

        # ── 1. Numeric-level checks ─────────────────────────────────────────
        for num in numerics:
            context = _context_window(prose, num.start, num.end)

            # Check whitelist first (years, Figure/Table refs)
            if _is_whitelisted_standalone(context, num.raw):
                cited_count += 1
                continue

            binding = _find_binding_marker(num, markers)

            if binding is None:
                violations.append(AuditViolation(
                    kind="uncited_number",
                    text_excerpt=context,
                    marker_raw=None,
                    prose_value=num.raw,
                    stored_value=None,
                    message=f"Numeric literal {num.raw!r} has no [run:...] or [lit:...] marker.",
                    fix_hint=(
                        f"Add [run:<run_id>#<json.path>] immediately after {num.raw!r} "
                        f"referencing the tool run that produced this value, "
                        f"or [lit:<tag>] if it is a literature/design value."
                    ),
                ))
                continue

            if binding.kind == MarkerKind.LIT:
                lit_span_count += 1
                tag = binding.lit_tag if hasattr(binding, "lit_tag") else binding.raw.strip("[]").split(":", 1)[-1]
                # Try to resolve hash-format tags against the passage index
                try:
                    from ai_hydro.knowledge.embeddings import is_hash_format, resolve_passage_hash, PASSAGE_INDEX_PATH
                    if is_hash_format(tag):
                        if _passage_index_exists is None:
                            _passage_index_exists = PASSAGE_INDEX_PATH.exists()
                        if _passage_index_exists:
                            rec = resolve_passage_hash(tag)
                            if rec is not None:
                                lit_resolved_count += 1
                            else:
                                lit_advisories.append(AuditViolation(
                                    kind="lit_unresolvable",
                                    text_excerpt=context,
                                    marker_raw=binding.raw,
                                    prose_value=num.raw,
                                    stored_value=None,
                                    message=(
                                        f"[lit:{tag}] looks like a passage hash but was not found "
                                        f"in the local passage index. The number is cited but "
                                        f"cannot be verified against the indexed passages."
                                    ),
                                    fix_hint=(
                                        f"Re-run index_passages() to refresh the index, or use "
                                        f"search_passages_tool() to find the correct passage hash "
                                        f"for this citation. Non-hash [lit:author+year] tags pass without check."
                                    ),
                                ))
                        # else: index absent — treat as legacy lit tag, no advisory
                except Exception:
                    pass  # embeddings module unavailable; treat as legacy lit tag
                cited_count += 1
                continue

            # binding.kind == RUN
            run_id   = binding.run_id
            json_path = binding.json_path

            if run_id not in run_log:
                violations.append(AuditViolation(
                    kind="run_id_not_found",
                    text_excerpt=context,
                    marker_raw=binding.raw,
                    prose_value=num.raw,
                    stored_value=None,
                    message=f"run_id {run_id!r} not found in session run-log.",
                    fix_hint=(
                        f"Use list_available_tools() then get_session_summary() to "
                        f"review the session run-log and correct the run_id. "
                        f"Available run_ids: {list(run_log.keys())[:5]}"
                    ),
                ))
                continue

            run_entry = run_log[run_id]
            found, stored = _resolve_json_path(run_entry, json_path)

            if not found:
                violations.append(AuditViolation(
                    kind="json_path_not_found",
                    text_excerpt=context,
                    marker_raw=binding.raw,
                    prose_value=num.raw,
                    stored_value=None,
                    message=(
                        f"JSON path {json_path!r} not found in run-log entry {run_id!r}. "
                        f"Available top-level keys: {list(run_entry.get('key_outputs', {}).keys())}"
                    ),
                    fix_hint=(
                        f"Check the key_outputs of run {run_id!r} and correct the path. "
                        f"Example corrected marker: [run:{run_id}#key_outputs.<correct_key>]"
                    ),
                ))
                continue

            if not _values_match(num.raw, stored):
                violations.append(AuditViolation(
                    kind="value_mismatch",
                    text_excerpt=context,
                    marker_raw=binding.raw,
                    prose_value=num.raw,
                    stored_value=stored,
                    message=(
                        f"Prose says {num.raw!r} but run-log {run_id!r} "
                        f"at path {json_path!r} stores {stored!r}."
                    ),
                    fix_hint=(
                        f"Correct the prose value to match the stored value ({stored!r}), "
                        f"or update the json_path in the marker if you meant a different field."
                    ),
                ))
                continue

            cited_count += 1

        # ── 2. Claim-level checks ───────────────────────────────────────────
        claim_markers = [m for m in markers if m.kind == MarkerKind.CLAIM]
        claim_pass = 0

        for cm in claim_markers:
            cid = cm.claim_id
            context = _context_window(prose, max(0, cm.start - 120), cm.end)

            if cid not in claims:
                violations.append(AuditViolation(
                    kind="claim_not_found",
                    text_excerpt=context,
                    marker_raw=cm.raw,
                    prose_value=None,
                    stored_value=None,
                    message=f"Claim id {cid!r} not found in session claims ledger.",
                    fix_hint=(
                        f"Use add_claim() to register this conclusion first, then cite it. "
                        f"Available claim ids: {list(claims.keys())[:5]}"
                    ),
                ))
                continue

            claim = claims[cid]
            status = claim.get("status", "")
            if status not in _ALLOWED_CLAIM_STATUSES:
                violations.append(AuditViolation(
                    kind="claim_bad_status",
                    text_excerpt=context,
                    marker_raw=cm.raw,
                    prose_value=None,
                    stored_value=status,
                    message=(
                        f"Claim {cid!r} has status {status!r}, which is not in "
                        f"the allowed set {sorted(_ALLOWED_CLAIM_STATUSES)}. "
                        f"Citing a proposed or contradicted claim as fact is a violation."
                    ),
                    fix_hint=(
                        f"Either update the claim's status via update_claim_status() "
                        f"after testing, or rephrase the sentence to acknowledge "
                        f"the claim is still {status!r} (e.g. 'preliminary evidence suggests')."
                    ),
                ))
                continue

            claim_pass += 1

        # ── 3. Assemble report ──────────────────────────────────────────────
        numeric_coverage = (cited_count / total_count) if total_count > 0 else 1.0

        return AuditReport(
            passed=len(violations) == 0,
            violations=violations,
            numeric_coverage=round(numeric_coverage, 4),
            total_numeric_count=total_count,
            cited_numeric_count=cited_count,
            claim_count=len(claim_markers),
            claim_pass_count=claim_pass,
            lit_span_count=lit_span_count,
            lit_resolved_count=lit_resolved_count,
            lit_advisories=lit_advisories,
        )

    except Exception as exc:
        log.error("resolve_prose failed unexpectedly: %s", exc, exc_info=True)
        return AuditReport(
            passed=False,
            violations=[AuditViolation(
                kind="uncited_number",
                text_excerpt="",
                marker_raw=None,
                prose_value=None,
                stored_value=None,
                message=f"Auditor internal error: {exc}",
                fix_hint="Report this error; the auditor itself has a bug.",
            )],
            numeric_coverage=0.0,
            total_numeric_count=0,
            cited_numeric_count=0,
            claim_count=0,
            claim_pass_count=0,
        )
