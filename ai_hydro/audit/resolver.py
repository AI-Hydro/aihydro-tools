"""
Audit resolver: bind prose markers to evidence and verify values.

Algorithm:
  1. Tokenise the prose and classify every number (grammar.classify_numerics):
     identifiers, codes, dates, years, meta-counts are exempt; sample sizes and
     quantities must be cited; method parameters are advisory.
  2. For each citable number: find the nearest following [run:...] / [lit:...]
     marker in the SAME sentence / bullet / table cell.
  3. [run:id#path]  -> load run-log entry, resolve the path (with aliasing:
                       data.X == key_outputs.X == evidence.data.X), compare the
                       value, then the unit if the run declares one.
  4. [claim:id]     -> load claim, check status in allowed set.
  5. [lit:tag]      -> pass (hash-format tags are checked against the passage index).
  6. No marker      -> uncited_number (hard, or advisory for method parameters).
  7. Emit AuditReport; violations are sorted hard-first.

The resolver is pure lookup — no inference, no LLM calls.
It never raises; all errors are captured as violations.
"""
from __future__ import annotations

import difflib
import logging
import re
from collections import Counter
from typing import Any

from .evidence import (
    available_fields,
    canonical_path,
    collect_known_ids,
    declared_unit,
    derived_from,
    is_hollow,
    iter_leaves,
    resolve_json_path,
    resolve_marker_path,
)
from .grammar import (
    ClassifiedNumeric,
    MarkerKind,
    NumericSpan,
    ParsedMarker,
    _context_window,
    _is_whitelisted_standalone,  # noqa: F401  (re-exported for older callers)
    classify_numerics,
    extract_markers,
    extract_numerics,  # noqa: F401
    segment_of,
    segments,
)
from .models import AuditReport, AuditViolation
from .units import units_conflict

log = logging.getLogger("ai_hydro.audit.resolver")

# Claim statuses that are acceptable for a cited-as-fact assertion.
_ALLOWED_CLAIM_STATUSES = {"tested", "supported", "weakly_supported"}

# How far (chars) ahead of a numeric we search for its binding marker.
_MARKER_LOOKAHEAD = 150

# Rounding tolerance: prose value within this many ULPs of last digit is a pass.
# E.g. prose "0.82" allows stored 0.815 ≤ v ≤ 0.824999…
_ROUNDING_TOLERANCE_EXTRA = 0.5  # half-unit-of-last-place cushion


_resolve_json_path = resolve_json_path   # kept for older importers


def _values_match(prose_raw: str, stored: Any) -> bool:
    """
    Return True if prose_raw (the numeric literal as found in text) is
    consistent with stored value within rounding tolerance.

    Handles: plain floats, percentages (prose "42%" vs stored 0.42 OR 42),
    comma-formatted integers, scientific notation.
    """
    # Clean the prose value
    clean = prose_raw.replace("\u2212", "-").replace(",", "").replace(" ", "").rstrip("%")
    # Remove unicode/ASCII scientific notation suffix for simpler compare
    clean = re.sub(r"[×x].*", "", clean).strip()
    try:
        prose_float = float(clean)
    except ValueError:
        return False

    # Stored value must be numeric
    if isinstance(stored, bool):
        return False
    try:
        stored_float = float(stored)
    except (TypeError, ValueError):
        return False

    # Compute half-ULP tolerance based on prose precision
    mantissa = re.split(r"[eE]", clean)[0]
    if "." in mantissa:
        decimals = len(mantissa.split(".")[1])
        ulp = 10 ** (-decimals)
    else:
        ulp = 1.0
    tolerance = ulp * (0.5 + _ROUNDING_TOLERANCE_EXTRA)

    # If prose had %, try both interpretations: stored as fraction or as percent.
    # The fraction reading is scaled by 100 together with the tolerance.
    if prose_raw.rstrip().endswith("%"):
        return (
            abs(prose_float - stored_float) <= tolerance
            or abs(prose_float / 100.0 - stored_float) <= tolerance / 100.0
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
    segs: list[tuple[int, int]] | None = None,
    prose: str | None = None,
) -> ParsedMarker | None:
    """
    Return the nearest [run:...] or [lit:...] marker that appears *after* the
    numeric literal and inside the same sentence / bullet / table cell.

    [claim:...] markers bind sentences, not individual numbers, so they are
    excluded here.  When no segmentation is supplied (older callers) the legacy
    _MARKER_LOOKAHEAD window applies.
    """
    if segs is not None:
        _, seg_hi = segment_of(segs, numeric.start)
    else:
        seg_hi = numeric.end + _MARKER_LOOKAHEAD
    for marker in markers:
        if marker.kind == MarkerKind.CLAIM:
            continue
        if marker.start >= numeric.end and marker.start < seg_hi:
            return marker
        if marker.start >= seg_hi:
            break
    return None


# Leaf names that carry a method parameter (bootstrap/CI settings) in a run.
_METHOD_LEAVES = {
    "ci_level", "confidence", "confidence_level", "alpha", "n_boot", "n_bootstrap",
    "n_resamples", "bootstrap_n", "block_size", "seed", "n_iter", "iterations", "passes",
    "n_passes", "n_replicates",
}


def _suggest_marker(raw: str, category: str, run_log: dict) -> tuple[str | None, int]:
    """
    A marker the agent could paste for ``raw``, when exactly one stored field
    (collapsed over mirrors and duplicate runs) carries that value.
    Returns (marker or None, number of distinct fields matched).
    """
    hits: dict[tuple[str, str], tuple[str, str]] = {}   # (run, leaf) -> (timestamp, path)
    for rid, entry in run_log.items():
        if not isinstance(entry, dict):
            continue
        for path, val in iter_leaves(entry):
            leaf = path.rsplit(".", 1)[-1]
            if category == "method_param" and leaf not in _METHOD_LEAVES:
                continue
            if _values_match(raw, val):
                hits.setdefault((rid, leaf), (str(entry.get("timestamp") or ""), path))
    leaves = {leaf for (_, leaf) in hits}
    if not hits or len(leaves) != 1:
        return None, len(leaves)
    (rid, _leaf), (_ts, path) = max(hits.items(), key=lambda kv: kv[1][0])
    return f"[run:{rid}#{path}]", 1


def _closest_fields(path: str, entry: dict) -> list[str]:
    leaf = path.rsplit(".", 1)[-1]
    return difflib.get_close_matches(leaf, available_fields(entry), n=3, cutoff=0.6)


def _site_ids(session_id: str) -> set[str]:
    """Ids of the audited study itself (site_id, canonical id), best effort."""
    ids = {session_id} if session_id else set()
    try:
        from ai_hydro.session.store import HydroSession
        sess = HydroSession.load(session_id)
        for v in (getattr(sess, "site_id", ""), getattr(sess, "canonical_id", "")):
            if v:
                ids.add(str(v))
    except Exception:
        pass
    return ids


def _violation(kind: str, context: str, marker: ParsedMarker | None, num: ClassifiedNumeric | None,
               message: str, fix_hint: str, *, severity: str = "hard", stored: Any = None,
               suggested: str | None = None, resolved: str | None = None) -> AuditViolation:
    return AuditViolation(
        kind=kind,                      # type: ignore[arg-type]
        text_excerpt=context,
        marker_raw=marker.raw if marker else None,
        prose_value=num.span.raw if num else None,
        stored_value=stored,
        message=message,
        fix_hint=fix_hint,
        severity=severity,              # type: ignore[arg-type]
        category=(num.category if num else None),
        suggested_marker=suggested,
        resolved_path=resolved,
    )


def resolve_prose(prose: str, session_id: str) -> AuditReport:
    """
    Full audit: tokenise + classify numerics, bind markers, resolve each, emit AuditReport.
    """
    violations: list[AuditViolation] = []

    try:
        markers = extract_markers(prose)

        run_log = _load_run_log(session_id)
        claims  = _load_claims(session_id)

        known_ids = collect_known_ids(session_id, run_log, claims) | _site_ids(session_id)
        classified = classify_numerics(prose, known_ids)
        segs = segments(prose)

        # Pre-check whether the passage index exists (used for lit advisory logic)
        _passage_index_exists: bool | None = None

        cited_count = 0
        total_count = 0
        exempt: Counter = Counter()
        lit_span_count = 0
        lit_resolved_count = 0
        lit_advisories: list[AuditViolation] = []

        # ── 1. Numeric-level checks ─────────────────────────────────────────
        for cn in classified:
            if cn.category == "exempt":
                exempt[cn.reason] += 1
                continue

            num = cn.span
            total_count += 1
            context = _context_window(prose, num.start, num.end)
            binding = _find_binding_marker(num, markers, segs, prose)
            advisory = cn.category == "method_param"

            if binding is None:
                suggested, _n = _suggest_marker(num.raw, cn.category, run_log)
                label = {
                    "sample_size": "sample size / record length",
                    "method_param": "method parameter",
                }.get(cn.category, "quantity")
                violations.append(_violation(
                    "uncited_number", context, None, cn,
                    f"The {label} {num.raw!r} has no [run:...] or [lit:...] marker "
                    f"in its sentence / bullet / table cell.",
                    (f"Add {suggested} immediately after {num.raw!r}."
                     if suggested else
                     f"Add [run:<run_id>#key_outputs.<field>] immediately after {num.raw!r} "
                     f"referencing the tool run that produced this value, "
                     f"or [lit:<tag>] if it is a literature/design value.")
                    + (" (Advisory: a method parameter does not block the answer.)" if advisory else ""),
                    severity="advisory" if advisory else "hard",
                    suggested=suggested,
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
                                lit_advisories.append(_violation(
                                    "lit_unresolvable", context, binding, cn,
                                    f"[lit:{tag}] looks like a passage hash but was not found "
                                    f"in the local passage index. The number is cited but "
                                    f"cannot be verified against the indexed passages.",
                                    f"Re-run index_passages() to refresh the index, or use "
                                    f"search_passages_tool() to find the correct passage hash "
                                    f"for this citation. Non-hash [lit:author+year] tags pass without check.",
                                    severity="advisory",
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
                violations.append(_violation(
                    "run_id_not_found", context, binding, cn,
                    f"run_id {run_id!r} not found in session run-log.",
                    f"Use list_available_tools() then get_session_summary() to "
                    f"review the session run-log and correct the run_id. "
                    f"Available run_ids: {list(run_log.keys())[:5]}",
                ))
                continue

            run_entry = run_log[run_id]
            found, stored, rpath = resolve_marker_path(run_entry, json_path)

            if not found:
                if is_hollow(run_entry):
                    origin = derived_from(run_entry)
                    hint = (
                        f"Run {run_id!r} recorded no outputs (a minimal / reused entry), so it cannot "
                        f"back {num.raw!r}. "
                    )
                    suggested = None
                    for orig in origin:
                        if orig in run_log:
                            ok, _v, op = resolve_marker_path(run_log[orig], json_path)
                            if ok:
                                suggested = f"[run:{orig}#{canonical_path(op)}]"
                                break
                    if suggested:
                        hint += f"It is derived from {origin}; cite the original run: {suggested}."
                    elif origin:
                        hint += f"It is derived from {origin}; cite the original run that holds the outputs."
                    else:
                        hint += ("Re-run the tool so it records its outputs, or cite the run that "
                                 "actually produced the value.")
                    violations.append(_violation(
                        "hollow_evidence", context, binding, cn,
                        f"Marker {binding.raw} points at run {run_id!r}, which has empty key_outputs "
                        f"(minimal entry); the value {num.raw!r} cannot be verified.",
                        hint, suggested=suggested,
                    ))
                    continue
                keys = available_fields(run_entry)
                close = _closest_fields(json_path, run_entry)
                violations.append(_violation(
                    "json_path_not_found", context, binding, cn,
                    f"JSON path {json_path!r} not found in run-log entry {run_id!r}. "
                    f"Available fields: {keys[:25]}",
                    (f"Check the key_outputs of run {run_id!r} and correct the path "
                     f"(canonical form: key_outputs.<field>; data.<field> and evidence.data.<field> "
                     f"are accepted aliases). "
                     + (f"Closest fields: {close}. " if close else "")
                     + f"Example corrected marker: [run:{run_id}#key_outputs."
                     + (close[0] if close else "<correct_key>") + "]"),
                ))
                continue

            if not _values_match(num.raw, stored):
                violations.append(_violation(
                    "value_mismatch", context, binding, cn,
                    f"Prose says {num.raw!r} but run-log {run_id!r} "
                    f"at path {json_path!r} stores {stored!r}.",
                    f"Correct the prose value to match the stored value ({stored!r}), "
                    f"or update the json_path in the marker if you meant a different field.",
                    stored=stored, resolved=rpath,
                ))
                continue

            declared = declared_unit(run_entry, rpath)
            if units_conflict(cn.unit, declared):
                violations.append(_violation(
                    "unit_mismatch", context, binding, cn,
                    f"Prose gives {num.raw!r} in {cn.unit!r} but run {run_id!r} declares "
                    f"{declared!r} for {canonical_path(rpath)!r}.",
                    f"State the value in the run's unit ({declared}) — or convert it explicitly, "
                    f"say so in the sentence, and cite the converted value's source.",
                    stored=stored, resolved=rpath,
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
                violations.append(_violation(
                    "claim_not_found", context, cm, None,
                    f"Claim id {cid!r} not found in session claims ledger.",
                    f"Use add_claim() to register this conclusion first, then cite it. "
                    f"Available claim ids: {list(claims.keys())[:5]}",
                ))
                continue

            claim = claims[cid]
            status = claim.get("status", "")
            if status not in _ALLOWED_CLAIM_STATUSES:
                violations.append(_violation(
                    "claim_bad_status", context, cm, None,
                    f"Claim {cid!r} has status {status!r}, which is not in "
                    f"the allowed set {sorted(_ALLOWED_CLAIM_STATUSES)}. "
                    f"Citing a proposed or contradicted claim as fact is a violation.",
                    f"Either update the claim's status via update_claim_status() "
                    f"after testing, or rephrase the sentence to acknowledge "
                    f"the claim is still {status!r} (e.g. 'preliminary evidence suggests').",
                    stored=status,
                ))
                continue

            claim_pass += 1

        # ── 3. Assemble report ──────────────────────────────────────────────
        violations.sort(key=lambda v: 0 if v.severity == "hard" else 1)   # stable: hard first
        hard = sum(1 for v in violations if v.severity == "hard")
        numeric_coverage = (cited_count / total_count) if total_count > 0 else 1.0

        return AuditReport(
            passed=hard == 0,
            violations=violations,
            numeric_coverage=round(numeric_coverage, 4),
            total_numeric_count=total_count,
            cited_numeric_count=cited_count,
            claim_count=len(claim_markers),
            claim_pass_count=claim_pass,
            lit_span_count=lit_span_count,
            lit_resolved_count=lit_resolved_count,
            lit_advisories=lit_advisories,
            hard_count=hard,
            advisory_count=len(violations) - hard,
            exempt_count=sum(exempt.values()),
            exempt_by_reason=dict(exempt),
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
            hard_count=1,
        )
