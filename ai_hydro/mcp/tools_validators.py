"""
Physics-based scientific validators.

Validators reduce invalid scientific composition at the workflow level.
They are agent-callable tools that produce diagnostic outputs (ValidatorResult).
"""
from __future__ import annotations

import logging
from typing import Literal

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict, resolve_existing_study
from ai_hydro.registry.paths import aihydro_home
from ai_hydro.session import HydroSession
from ai_hydro.validators.models import ValidatorResult
from ai_hydro.validators.series_binding import AMBIGUOUS, SERIES_MISMATCH

log = logging.getLogger("ai_hydro.mcp")


# Plausible long-term runoff-ratio (Q/P) range for the water-balance check.
# Engineering bound, not a literature constant: CAMELS basin runoff ratios
# (Addor et al. 2017) sit roughly in 0 to ~1.5 (values > 1 occur with snow
# undercatch, groundwater import or karst); anything below 1e-3 or above 3 is
# treated as a defective input (fill values, unit error) rather than climate.
# Ratios in (1, 3] still produce the existing > 1.0 warning.
RUNOFF_RATIO_MIN = 1e-3
RUNOFF_RATIO_MAX = 3.0


def _non_physical_scalar(value, lo: float, hi: float) -> str | None:
    """Return a reason if ``value`` is not a finite number inside [lo, hi]."""
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "it is not a number"
    if not math.isfinite(value):
        return "it is not finite"
    if value <= 0:
        return "it is not positive"
    if not (lo <= value <= hi):
        return f"it is outside the plausible range [{lo:g}, {hi:g}]"
    return None


@mcp.tool()
def check_water_balance_consistency(session_id: str) -> dict:
    """
    Check if annual runoff is less than or equal to annual precipitation.
    
    Runoff ratio > 1.0 indicates a potential error or significant 
    non-precipitation water source (snowmelt, groundwater).

    Reports ``insufficient_data`` (never ``pass``) when the runoff ratio is
    missing, non-finite, <= 0, outside [1e-3, 3.0], or was derived from
    precipitation the signatures record marks as not used.
    """
    try:
        session_id = resolve_existing_study(session_id)
        session = HydroSession.load(session_id)
        sigs = session.get("signatures")
        if not sigs:
            return ValidatorResult(
                validator="water_balance_consistency",
                status="insufficient_data",
                message="Hydrological signatures (runoff_ratio) not found in session.",
                affected_slots=["signatures"],
                recommendation="Run extract_hydrological_signatures first."
            ).model_dump()

        sig_data = sigs.get("data", {}) or {}
        rr = sig_data.get("runoff_ratio")
        if rr is None:
            _pr = sig_data.get("_precipitation")
            _why = (f" Precipitation status: {_pr.get('status')} ({_pr.get('reason')})."
                    if isinstance(_pr, dict) and _pr.get("status") is not None
                    and not precipitation_usable(_pr)[0] else "")
            return ValidatorResult(
                validator="water_balance_consistency",
                status="insufficient_data",
                message="Runoff ratio missing from signatures data." + _why,
                affected_slots=["signatures"]
            ).model_dump()

        # General rule: a validator never passes on a non-physical input.
        _bad = _non_physical_scalar(rr, RUNOFF_RATIO_MIN, RUNOFF_RATIO_MAX)
        if _bad is None:
            _pr = sig_data.get("_precipitation")
            if isinstance(_pr, dict):
                # Keyed on status AND reason codes: used_with_gaps whose only gap is the skipped leap-year
                # Dec 31 (client_calendar_assumption) is not degraded; other gap reasons are.
                _ok, _why_not = precipitation_usable(_pr) if _pr.get("status") is not None else (True, None)
            else:
                _pst = sig_data.get("precipitation_status")
                _ok, _why_not = ((True, None) if _pst in (None, *PRECIPITATION_USED_STATUSES)
                                 else (False, f"status {_pst}"))
            if not _ok:
                _bad = ("it was derived from precipitation that was not usable "
                        f"({_why_not})")
        if _bad is not None:
            return ValidatorResult(
                validator="water_balance_consistency",
                status="insufficient_data",
                message=f"Runoff ratio {rr!r} cannot be checked: {_bad}.",
                recommendation="Restore a precipitation source and re-run "
                               "extract_hydrological_signatures.",
                affected_slots=["signatures"],
                metadata={"plausible_range": [RUNOFF_RATIO_MIN, RUNOFF_RATIO_MAX]},
            ).model_dump()

        if rr > 1.0:
            severity = "medium" if rr < 1.1 else "high"
            return ValidatorResult(
                validator="water_balance_consistency",
                status="warning",
                severity=severity,
                message=f"Annual runoff ratio is {rr:.2f} (> 1.0).",
                recommendation="Investigate if snowmelt, groundwater release, or gauging errors are present.",
                affected_slots=["signatures"]
            ).model_dump()

        return ValidatorResult(
            validator="water_balance_consistency",
            status="pass",
            message=f"Water balance is consistent (Runoff Ratio: {rr:.2f}).",
            affected_slots=["signatures"]
        ).model_dump()
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def check_temporal_alignment(session_id: str, slot_a: str, slot_b: str) -> dict:
    """
    Check if two time-series slots share the same temporal range.
    """
    try:
        session_id = resolve_existing_study(session_id)
        session = HydroSession.load(session_id)
        a = session.get(slot_a)
        b = session.get(slot_b)

        if not a or not b:
            return ValidatorResult(
                validator="temporal_alignment",
                status="insufficient_data",
                message=f"One or both slots ({slot_a}, {slot_b}) are missing.",
                affected_slots=[slot_a, slot_b]
            ).model_dump()

        ma = a.get("meta", {}).get("params", {})
        mb = b.get("meta", {}).get("params", {})
        
        start_a, end_a = ma.get("start_date"), ma.get("end_date")
        start_b, end_b = mb.get("start_date"), mb.get("end_date")

        if not all([start_a, end_a, start_b, end_b]):
            return ValidatorResult(
                validator="temporal_alignment",
                status="warning",
                severity="low",
                message="Missing temporal metadata (start/end dates). Cannot verify alignment.",
                affected_slots=[slot_a, slot_b]
            ).model_dump()

        # Never pass on unparseable or inverted dates (equal garbage is not alignment).
        import pandas as _pd
        try:
            _ta, _tb = (_pd.Timestamp(start_a), _pd.Timestamp(end_a)), (_pd.Timestamp(start_b), _pd.Timestamp(end_b))
            _bad_dates = (None if _ta[0] <= _ta[1] and _tb[0] <= _tb[1]
                          else "an end date precedes its start date")
        except Exception:
            _bad_dates = "a start/end date is not a valid date"
        if _bad_dates:
            return ValidatorResult(
                validator="temporal_alignment",
                status="insufficient_data",
                message=f"Temporal alignment cannot be verified: {_bad_dates}.",
                affected_slots=[slot_a, slot_b]
            ).model_dump()

        if start_a == start_b and end_a == end_b:
            return ValidatorResult(
                validator="temporal_alignment",
                status="pass",
                message=f"Slots aligned: {start_a} to {end_a}.",
                affected_slots=[slot_a, slot_b]
            ).model_dump()

        return ValidatorResult(
            validator="temporal_alignment",
            status="fail",
            severity="medium",
            message=f"Temporal mismatch: {slot_a} ({start_a} to {end_a}) vs {slot_b} ({start_b} to {end_b}).",
            recommendation="Re-fetch or truncate data to ensure consistent temporal overlap.",
            affected_slots=[slot_a, slot_b]
        ).model_dump()

    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def check_unit_consistency(session_id: str, slot: str, expected_units: str) -> dict:
    """
    Check if a session slot uses the expected scientific units.
    """
    try:
        session_id = resolve_existing_study(session_id)
        session = HydroSession.load(session_id)
        s = session.get(slot)
        if not s:
            return ValidatorResult(
                validator="unit_consistency",
                status="insufficient_data",
                message=f"Slot '{slot}' not found.",
                affected_slots=[slot]
            ).model_dump()

        # Check 'data.units' or 'meta.units'
        actual_units = s.get("data", {}).get("units") or s.get("meta", {}).get("units")
        
        if not actual_units:
            return ValidatorResult(
                validator="unit_consistency",
                status="warning",
                severity="low",
                message=f"Units not explicitly declared for slot '{slot}'.",
                affected_slots=[slot]
            ).model_dump()

        if actual_units == expected_units:
            return ValidatorResult(
                validator="unit_consistency",
                status="pass",
                message=f"Units consistent: {actual_units}.",
                affected_slots=[slot]
            ).model_dump()

        return ValidatorResult(
            validator="unit_consistency",
            status="fail",
            severity="high",
            message=f"Unit mismatch for '{slot}': Found '{actual_units}', expected '{expected_units}'.",
            recommendation=f"Apply unit conversion to {expected_units} before proceeding.",
            affected_slots=[slot]
        ).model_dump()

    except Exception as exc:
        return _tool_error_to_dict(exc)


# --------------------------------------------------------------------------- #
# Series-bound streamflow validators (Science Wave 3, PR-4b)
#
# record_length, usgs_qualification_codes, stationarity and regulated_basin take
# ``source="signatures"|"streamflow"`` and an optional ``series_digest``. They resolve the
# series they judge by digest (``ai_hydro.validators.series_binding``) and never fall back to a
# different series: any failed check is ``insufficient_data`` with ``series_mismatch``.
# --------------------------------------------------------------------------- #

#: Reason codes under which a precipitation gap does NOT degrade a consumer. A skipped leap-year Dec 31
#: (aihydro-data ``client_calendar_assumption``) alone is a disclosure, not a defect.
BENIGN_PRECIPITATION_GAP_REASONS = ("client_calendar_assumption",)
#: ``_precipitation.status`` values under which the precipitation WAS used to compute runoff_ratio / stream_elas.
PRECIPITATION_USED_STATUSES = ("used", "used_with_gaps")


def precipitation_usable(prec: object) -> tuple[bool, str | None]:
    """``(usable, why_not)`` for a ``_precipitation`` record, keyed on status and REASON CODES.

    ``used`` is usable. ``used_with_gaps`` is usable too, but only when no gap carries a reason other than
    ``BENIGN_PRECIPITATION_GAP_REASONS`` and no water year was excluded as incomplete: a record whose only gap is
    ``client_calendar_assumption`` is not degraded. An unreadable status is not usable.
    """
    if not isinstance(prec, dict):
        return False, "no precipitation record"
    status = prec.get("status")
    if status == "used":
        return True, None
    if status != "used_with_gaps":
        return False, f"status {status}" + (f": {prec.get('reason')}" if prec.get("reason") else "")
    reasons = {r.get("reason") for r in (prec.get("missing_ranges") or []) if isinstance(r, dict)}
    bad = sorted(str(r) for r in reasons - set(BENIGN_PRECIPITATION_GAP_REASONS))
    if prec.get("water_years_excluded"):
        return False, "water years excluded as incomplete"
    if bad or not reasons or prec.get("missing_ranges_truncated"):
        return False, "gaps with reason " + (", ".join(bad) if bad else "unknown")
    return True, None


def _series_problem_result(validator: str, b, affected: list[str]) -> dict:
    """The one shape every series-bound validator returns when its series cannot be resolved or verified."""
    mismatch = b.problem == SERIES_MISMATCH
    md = {"quality_flag": b.problem, "series_mismatch": mismatch, "source": b.source}
    if b.problem == AMBIGUOUS:
        md["teaching_error"] = {
            "rule": "validator_needs_to_know_which_signatures_result",
            "explanation": "More than one signatures result is stored, and a validator never guesses which series to judge.",
            "how_to_fix": "Call again with run_id=<the signatures run id> (or feature=<feature id>, or "
                          "series_digest=<the result's streamflow_digest>)."}
    return ValidatorResult(
        validator=validator,
        status="insufficient_data",
        message=b.message or "The series could not be resolved.",
        recommendation=("Re-run the producing tool, or call this validator with the source that holds the series "
                        "you mean (source='signatures' for the series the signatures consumed, "
                        "source='streamflow' for the session slot)."),
        affected_slots=affected,
        metadata=md,
    ).model_dump()


def _bound(session, source: str, series_digest: str | None, run_id: str | None = None,
           feature: str | None = None):
    from ai_hydro.validators.series_binding import bind_series
    return bind_series(session, source, series_digest, run_id, feature)


def _record_length_verdict(n_days, affected: list[str], series_md: dict | None = None,
                           what: str = "streamflow slot") -> dict:
    """Tiered record-length verdict for ``n_days`` valid days (the thresholds are the tool docstring's)."""
    extra = {"series": series_md} if series_md else {}
    _bad_n = (n_days is not None and (
        isinstance(n_days, bool) or not isinstance(n_days, (int, float))
        or n_days != n_days or n_days in (float("inf"), float("-inf")) or n_days < 0))
    if n_days is None or _bad_n:
        return ValidatorResult(
            validator="record_length",
            status="insufficient_data",
            message=f"Record length could not be determined from the {what}"
                    + (f" (n_days={n_days!r} is not a non-negative finite number)." if _bad_n else "."),
            affected_slots=affected,
            metadata=extra,
        ).model_dump()

    years_approx = n_days / 365.25

    if n_days < 3650:
        return ValidatorResult(
            validator="record_length",
            status="fail",
            severity="high",
            message=(
                f"Record is only {n_days} days ({years_approx:.1f} years). "
                "Flood-frequency analysis requires ≥ 10 years of data."
            ),
            recommendation=(
                "Extend the record length or restrict the analysis to "
                "low-return-period statistics (< 10-year events). "
                "Do not use this record for design-flood estimation."
            ),
            affected_slots=affected,
            metadata={"n_days": n_days, "quality_flag": "record_too_short", **extra},
        ).model_dump()

    if n_days < 7300:
        return ValidatorResult(
            validator="record_length",
            status="warning",
            severity="medium",
            message=(
                f"Record is {n_days} days ({years_approx:.1f} years) — "
                "adequate but below the 20-year recommended minimum for "
                "robust flood-frequency and trend analyses."
            ),
            recommendation=(
                "Use 90% CIs on all frequency estimates and note the "
                "limited record length as a limitation in any claim. "
                "If a longer record is available, extend the fetch range."
            ),
            affected_slots=affected,
            metadata={"n_days": n_days, "quality_flag": "record_length_low", **extra},
        ).model_dump()

    return ValidatorResult(
        validator="record_length",
        status="pass",
        message=(
            f"Record length is {n_days} days ({years_approx:.1f} years) — "
            "meets the ≥ 20-year threshold for reliable flood-frequency analysis."
        ),
        affected_slots=affected,
        metadata={"n_days": n_days, **extra},
    ).model_dump()


def _record_length_for_series(b) -> dict:
    return _record_length_verdict(b.n_valid, [b.slot], b.describe(), what=f"{b.source} series")


@mcp.tool()
def check_record_length(
    session_id: str,
    source: Literal["signatures", "streamflow"] = "streamflow",
    series_digest: str | None = None,
    run_id: str | None = None,
    feature: str | None = None,
) -> dict:
    """
    Guard against analyses on too-short records.

    Counts the days of the series being judged and applies tiered thresholds
    from hydrology literature (Vogel & Fennessey 1994; Burn & Elnur 2002):
      - FAIL    : n < 3650 days (< 10 years) — flood frequency unreliable
      - WARNING : n < 7300 days (< 20 years) — adequate but at low end
      - PASS    : n ≥ 7300 days (20+ years)

    source : "streamflow" (default) judges the session streamflow slot; "signatures" judges the
        series extract_hydrological_signatures consumed (retained and digest-bound), which need not be
        the slot's. series_digest, when given, is the digest the series must have; a different series is
        never validated: the result is insufficient_data with series_mismatch. With several stored signatures
        results, run_id (the result's run id) or feature chooses one; without either, or series_digest, the call
        is ambiguous and says so instead of guessing.

    Fires automatically after fetch_streamflow_data (source "streamflow") and after
    extract_hydrological_signatures (source "signatures").
    """
    try:
        session_id = resolve_existing_study(session_id)
        session = HydroSession.load(session_id)
        if source != "streamflow" or series_digest or run_id or feature:
            b = _bound(session, source, series_digest, run_id, feature)
            if not b.resolved:
                return _series_problem_result("record_length", b, [b.slot])
            return _record_length_for_series(b)
        sf = session.get("streamflow")
        if not sf:
            return ValidatorResult(
                validator="record_length",
                status="insufficient_data",
                message="No streamflow slot found. Fetch streamflow data first.",
                affected_slots=["streamflow"],
                recommendation="Call fetch_streamflow_data or data_fetch(variable='streamflow', ...).",
            ).model_dump()

        data = sf.get("data", {})
        n_days: int | None = data.get("n_days")

        # Fall back: use q_cms_n (stripped count) or count short q_cms array
        if n_days is None:
            n_days = data.get("q_cms_n")  # stored when array was stripped on save
            if n_days is None:
                q_vals = data.get("q_cms", [])
                if q_vals:
                    n_days = sum(1 for v in q_vals if v is not None)
        return _record_length_verdict(n_days, ["streamflow"])
    except Exception as exc:
        return _tool_error_to_dict(exc)


def _qualification_verdict(
    qual_codes, provisional_flag, approval_counts, qualifier_counts, qualifiers_other, prov_fraction,
    affected: list[str], series_md: dict | None = None,
) -> dict:
    """The review-status verdict for USGS approval / qualifier information, wherever it was read from."""
    extra = {"series": series_md} if series_md else {}
    has_counts = isinstance(approval_counts, dict) and bool(approval_counts) or (
        isinstance(qualifier_counts, dict) and bool(qualifier_counts))

    # Never certify "approved" from codes we cannot read: a non-list value, or a
    # non-string entry, is not evidence of review status. A string token outside the
    # USGS approved ('A'), provisional ('P') and estimated ('e') vocabulary IS readable
    # (ice, equipment malfunction, ...): it is reported with its raw value as a warning.
    _tokens: list[str] = []
    _unreadable = False
    if qual_codes is not None:
        if not isinstance(qual_codes, list):
            _unreadable = True
        else:
            for c in qual_codes:
                if not isinstance(c, str):
                    _unreadable = True
                else:
                    _tokens.extend(c.replace(",", " ").split())
    # Counts keyed by the API's words or by the short codes.
    def _count(d: object, *keys: str) -> float:
        if not isinstance(d, dict):
            return 0
        tot = 0
        for k in keys:
            v = d.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                tot += v
        return tot

    n_prov_counts = _count(approval_counts, "Provisional", "P")
    n_est_counts = _count(qualifier_counts, "ESTIMATED", "e")
    other: dict[str, int] = {}
    for t in _tokens:
        if t not in ("A", "P", "e"):
            other[t] = other.get(t, 0) + 1
    if isinstance(qualifiers_other, dict):
        for k, v in qualifiers_other.items():
            if k in ("A", "P", "e"):
                continue
            other[k] = max(other.get(k, 0), v) if isinstance(v, (int, float)) else other.get(k, 0)
    if isinstance(qualifier_counts, dict):
        for k, v in qualifier_counts.items():
            if k not in ("ESTIMATED", "e", "A", "P") and isinstance(v, (int, float)):
                other[k] = max(other.get(k, 0), v)

    has_provisional = (
        bool(provisional_flag) or "P" in _tokens or n_prov_counts > 0
        or (isinstance(prov_fraction, (int, float)) and prov_fraction > 0)
    )
    has_estimated = "e" in _tokens or n_est_counts > 0

    if _unreadable and not (has_provisional or has_estimated or other):
        return ValidatorResult(
            validator="usgs_qualification_codes",
            status="insufficient_data",
            message=f"Qualification codes {qual_codes!r} are not in a readable form "
                    "(expected a list of code strings: A / P / e or raw USGS qualifier tokens); "
                    "review status unknown.",
            affected_slots=affected,
            metadata={"quality_flag": "qualification_codes_unreadable", **extra},
        ).model_dump()
    if qual_codes is not None and not _tokens and not has_counts and not provisional_flag:
        return ValidatorResult(
            validator="usgs_qualification_codes",
            status="insufficient_data",
            message="Qualification codes are empty; review status unknown.",
            affected_slots=affected,
            metadata={"quality_flag": "qualification_codes_absent", **extra},
        ).model_dump()

    counts_md = {"approval_counts": approval_counts, "qualifier_counts": qualifier_counts,
                 "qualifiers_other": other, **extra}
    if has_provisional:
        frac = (f" ({prov_fraction:.1%} of values)"
                if isinstance(prov_fraction, (int, float)) and prov_fraction > 0 else "")
        return ValidatorResult(
            validator="usgs_qualification_codes",
            status="warning",
            severity="medium",
            message=(
                f"Streamflow record contains provisional ('P') data{frac} that has not "
                "yet been reviewed and approved by USGS. Values may be revised."
                + (f" Other qualifiers present: {other}." if other else "")
            ),
            recommendation=(
                "Add a limitations entry to any claim using this data: "
                "'Data is provisional and subject to revision by USGS.' "
                "Re-check the USGS Water Data portal before publication."
            ),
            affected_slots=affected,
            metadata={"quality_flag": "provisional_data", "codes": qual_codes
                      if not isinstance(qual_codes, list) or len(qual_codes) <= 50 else None,
                      **counts_md},
        ).model_dump()

    if other:
        return ValidatorResult(
            validator="usgs_qualification_codes",
            status="warning",
            severity="medium",
            message=(
                "Streamflow record carries USGS qualifiers outside approved / provisional / "
                f"estimated: {other} (raw values, value counts). Such values (ice, equipment "
                "malfunction, interpolation, ...) are not ordinary approved observations."
            ),
            recommendation=(
                "State the qualifier counts as a limitation in any claim, and check the "
                "flagged periods in the USGS Water Data portal before using them."
            ),
            affected_slots=affected,
            metadata={"quality_flag": "other_qualifiers", **counts_md},
        ).model_dump()

    if has_estimated:
        return ValidatorResult(
            validator="usgs_qualification_codes",
            status="warning",
            severity="low",
            message=(
                "Streamflow record contains estimated ('e') values — "
                "gaps were filled using regression or nearby gauges."
            ),
            recommendation=(
                "Note in any claim that some values are estimated. "
                "Estimated records are particularly common during flood peaks."
            ),
            affected_slots=affected,
            metadata={"quality_flag": "estimated_data", **counts_md,
                      "codes": qual_codes if not isinstance(qual_codes, list) or len(qual_codes) <= 50 else None},
        ).model_dump()

    return ValidatorResult(
        validator="usgs_qualification_codes",
        status="pass",
        message="Streamflow data is approved / reviewed (no provisional, estimated or other qualifier codes).",
        affected_slots=affected,
        metadata=dict(extra),
    ).model_dump()


def _qualification_for_series(b) -> dict:
    """Verdict from the per-day approval / qualifier codes of a bound series (valid days only)."""
    affected = [b.slot]
    md = b.describe()
    if b.qualifier_codes is None and b.approval_codes is None:
        return ValidatorResult(
            validator="usgs_qualification_codes",
            status="insufficient_data",
            message=(f"The {b.source} series has no USGS approval or qualifier codes attached: "
                     + (b.codes_reason or "the retained series records none")
                     + ". Review status unknown."),
            affected_slots=affected,
            recommendation="Verify the data review status directly from the USGS Water Data portal "
                           "(waterdata.usgs.gov) for the gauge, and list it as a limitation.",
            metadata={"quality_flag": "qualification_codes_absent", "series": md},
        ).model_dump()
    valid = [_is_finite_number(v) for v in b.q]

    def _tok(codes, i):
        if not codes or i >= len(codes) or codes[i] in (None, ""):
            return []
        return [t for t in str(codes[i]).replace(",", " ").split() if t]

    approval: dict[str, int] = {}
    qualifier: dict[str, int] = {}
    n_with_approval = 0
    for i, ok in enumerate(valid):
        if not ok:
            continue
        a = _tok(b.approval_codes, i)
        if a:
            n_with_approval += 1
        for t in a:
            approval[t] = approval.get(t, 0) + 1
        for t in _tok(b.qualifier_codes, i):
            qualifier[t] = qualifier.get(t, 0) + 1
    prov_fraction = (approval.get("P", 0) / n_with_approval) if n_with_approval else None
    other = {k: v for k, v in qualifier.items() if k not in ("e", "A", "P")}
    return _qualification_verdict(
        None, None, approval, qualifier, other, prov_fraction, affected, md)


def _is_finite_number(v) -> bool:
    import math
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


@mcp.tool()
def check_usgs_qualification_codes(
    session_id: str,
    source: Literal["signatures", "streamflow"] = "streamflow",
    series_digest: str | None = None,
    run_id: str | None = None,
    feature: str | None = None,
) -> dict:
    """
    Flag provisional or estimated USGS streamflow data.

    source="streamflow" (default): reads qualification_codes, the quality summary (approval /
    qualifier counts, ``qualifiers_other``, ``provisional_fraction``) from the streamflow slot
    metadata. source="signatures": reads the per-day approval and qualifier codes of the series
    extract_hydrological_signatures consumed (retained and digest-bound), counting valid days only.
    A different series than the one named (series_digest) is never validated: the result is
    insufficient_data with series_mismatch.

    USGS uses 'P' for provisional (not yet reviewed) and 'e' for estimated
    records. Provisional data may be revised; using it in published claims
    without noting this is a scientific integrity risk.

    Returns 'insufficient_data' when no qualification codes are stored —
    the fetch pipeline does not yet propagate these codes for all sources.
    In that case, treat the data as unreviewed and note it as a limitation.
    """
    try:
        session_id = resolve_existing_study(session_id)
        session = HydroSession.load(session_id)
        if source != "streamflow" or series_digest or run_id or feature:
            b = _bound(session, source, series_digest, run_id, feature)
            if not b.resolved:
                return _series_problem_result("usgs_qualification_codes", b, [b.slot])
            if source == "signatures" or b.qualifier_codes is not None or b.approval_codes is not None:
                return _qualification_for_series(b)
        sf = session.get("streamflow")
        if not sf:
            return ValidatorResult(
                validator="usgs_qualification_codes",
                status="insufficient_data",
                message="No streamflow slot found. Fetch streamflow data first.",
                affected_slots=["streamflow"],
                recommendation="Call fetch_streamflow_data or data_fetch(variable='streamflow', ...).",
            ).model_dump()

        data = sf.get("data", {})
        meta = sf.get("meta", {})

        # Check meta first, then data-level keys
        qual_codes: list | None = (
            meta.get("qualification_codes")
            or data.get("qualification_codes")
        )
        rle = meta.get("qualification_codes_rle")
        if qual_codes is None and isinstance(rle, list) and rle and all(
                isinstance(r, (list, tuple)) and len(r) == 2 and isinstance(r[0], str) for r in rle):
            qual_codes = [r[0] for r in rle]          # run-length-encoded: the distinct runs are enough
        provisional_flag: bool | None = (
            meta.get("provisional")
            or data.get("provisional")
        )
        # Quality summary written by the slot writer (approval / qualifier counts as the
        # USGS Water Data API states them). Flat copies are accepted too.
        qsum = meta.get("quality_summary") if isinstance(meta.get("quality_summary"), dict) else {}
        approval_counts = qsum.get("approval_counts") or meta.get("approval_counts") or {}
        qualifier_counts = qsum.get("qualifier_counts") or meta.get("qualifier_counts") or {}
        qualifiers_other = qsum.get("qualifiers_other") or meta.get("qualifiers_other") or {}
        prov_fraction = qsum.get("provisional_fraction", meta.get("provisional_fraction"))
        has_counts = isinstance(approval_counts, dict) and bool(approval_counts) or (
            isinstance(qualifier_counts, dict) and bool(qualifier_counts))

        if qual_codes is None and provisional_flag is None and not has_counts:
            # Not propagated by fetch pipeline yet
            source_name = meta.get("source", "")
            note = (
                " NWIS streams may include provisional records not flagged here."
                if "nwis" in source_name.lower() or "usgs" in source_name.lower()
                else ""
            )
            return ValidatorResult(
                validator="usgs_qualification_codes",
                status="insufficient_data",
                message=(
                    "Qualification codes are not present in the streamflow metadata."
                    + note
                ),
                affected_slots=["streamflow"],
                recommendation=(
                    "Verify the data review status directly from the USGS Water Data portal "
                    "(waterdata.usgs.gov) for the gauge. Note in any claim whether the "
                    "data has been reviewed/approved by USGS."
                ),
                metadata={"quality_flag": "qualification_codes_absent"},
            ).model_dump()

        return _qualification_verdict(
            qual_codes, provisional_flag, approval_counts, qualifier_counts, qualifiers_other,
            prov_fraction, ["streamflow"])
    except Exception as exc:
        return _tool_error_to_dict(exc)


def _norm_gauge(g) -> str | None:
    s = str(g or "").strip()
    return s.zfill(8) if s.isdigit() else (s or None)


def _basin_gauge_mismatch(session, expect_gauge: str | None) -> str | None:
    """Why the session's delineated basin is not the one ``expect_gauge`` names, else ``None``."""
    want = _norm_gauge(expect_gauge)
    if not want or want == "global_watershed":
        return None
    for slot_name in ("delineation", "watershed"):
        slot = session.get(slot_name)
        have = _norm_gauge(((slot or {}).get("data") or {}).get("gauge_id"))
        if have:
            return None if have == want else (
                f"the session's delineated basin is gauge {have}, but the series describes gauge {want}")
    return None


def _regulated_verdict(session, expect_gauge: str | None = None, series_md: dict | None = None) -> dict:
    extra = {"series": series_md} if series_md else {}
    why = _basin_gauge_mismatch(session, expect_gauge) if series_md else None
    if why:
        return ValidatorResult(
            validator="regulated_basin",
            status="insufficient_data",
            message=f"Regulation cannot be judged for this series: {why}; not validating a different basin.",
            affected_slots=["delineation"],
            recommendation="Run the regulation check on the basin the series belongs to (delineate it first).",
            metadata={"quality_flag": SERIES_MISMATCH, "series_mismatch": True, **extra},
        ).model_dump()

    # --- Step 1: check explicit metadata flag in delineation or signatures slot
    for slot_name in ("delineation", "watershed", "signatures"):
        slot = session.get(slot_name)
        if not slot:
            continue
        data = slot.get("data", {})
        meta = slot.get("meta", {})
        regulated = data["regulated"] if "regulated" in data else meta.get("regulated")
        if regulated is not None:
            if regulated:
                return ValidatorResult(
                    validator="regulated_basin",
                    status="warning",
                    severity="high",
                    message=(
                        f"Basin is flagged as regulated (upstream dams or diversions "
                        f"detected via '{slot_name}' metadata)."
                    ),
                    recommendation=(
                        "Natural-flow statistics (BFI, flood frequency, recession constants) "
                        "may not reflect catchment hydrology. Add a limitations entry: "
                        "'Basin contains upstream regulation; natural-flow statistics "
                        "are approximate.' Consider using regulated-flow modelling."
                    ),
                    affected_slots=[slot_name],
                    metadata={"quality_flag": "regulated_basin", **extra},
                ).model_dump()
            else:
                return ValidatorResult(
                    validator="regulated_basin",
                    status="pass",
                    message=f"Basin flagged as unregulated in '{slot_name}' metadata.",
                    affected_slots=[slot_name],
                    metadata=dict(extra),
                ).model_dump()

    # --- Step 2: NID GeoPackage spatial check
    nid_path = aihydro_home() / "cache" / "full_nid_inventory.gpkg"
    if nid_path.exists():
        try:
            import geopandas as _gpd  # optional dep

            # Try to get watershed geometry from delineation slot
            geom_wkt: str | None = None
            for slot_name in ("delineation", "watershed"):
                slot = session.get(slot_name)
                if slot:
                    geom_wkt = slot.get("data", {}).get("geometry_wkt")
                    if geom_wkt:
                        break

            if geom_wkt:
                from shapely import wkt as _wkt
                basin_geom = _wkt.loads(geom_wkt)
                nid = _gpd.read_file(nid_path, mask=basin_geom)
                n_dams = len(nid)
                if n_dams > 0:
                    return ValidatorResult(
                        validator="regulated_basin",
                        status="warning",
                        severity="high",
                        message=(
                            f"NID inventory found {n_dams} dam(s) within the watershed boundary."
                        ),
                        recommendation=(
                            "Natural-flow statistics may be affected by upstream regulation. "
                            "Add a limitations entry and consider regulated-flow corrections."
                        ),
                        affected_slots=["delineation"],
                        metadata={
                            "quality_flag": "regulated_basin",
                            "n_dams_in_watershed": n_dams,
                            **extra,
                        },
                    ).model_dump()
                return ValidatorResult(
                    validator="regulated_basin",
                    status="pass",
                    message=f"NID inventory found no dams within the watershed boundary.",
                    affected_slots=["delineation"],
                    metadata={"n_dams_in_watershed": 0, **extra},
                ).model_dump()
        except ImportError:
            pass  # geopandas/shapely not installed — fall through to insufficient_data

    return ValidatorResult(
        validator="regulated_basin",
        status="insufficient_data",
        message=(
            "Regulation status could not be determined: no 'regulated' metadata "
            "flag in session and NID GeoPackage not found at "
            "<AIHYDRO_HOME>/cache/full_nid_inventory.gpkg."
        ),
        affected_slots=["delineation"],
        recommendation=(
            "Add 'regulated: false/true' to the watershed metadata, or download "
            "the NID GeoPackage to <AIHYDRO_HOME>/cache/full_nid_inventory.gpkg. "
            "As a precaution, list regulation status as a limitation in any claim."
        ),
        metadata={"quality_flag": "regulation_unknown", **extra},
    ).model_dump()


@mcp.tool()
def check_regulated_basin(
    session_id: str,
    source: Literal["signatures", "streamflow"] = "streamflow",
    series_digest: str | None = None,
    run_id: str | None = None,
    feature: str | None = None,
) -> dict:
    """
    Flag basins with known upstream regulation (dams, diversions).

    First checks the session delineation slot for an explicit 'regulated'
    metadata flag (populated by delineate_watershed when NID data is
    available). Falls back to checking the NID GeoPackage at
    ~/.aihydro/cache/full_nid_inventory.gpkg — if present, performs a
    spatial query on the watershed boundary.

    source="signatures" (or a series_digest) binds the check to the series the signatures consumed: the
    result applies only when the session's delineated basin is the gauge that series describes; otherwise it
    is insufficient_data with series_mismatch (never a verdict about a different basin).

    Returns 'insufficient_data' when neither the metadata flag nor the NID
    cache is available. In that case, the regulation status is unknown and
    should be listed as a limitation.
    """
    try:
        session_id = resolve_existing_study(session_id)
        session = HydroSession.load(session_id)
        if source != "streamflow" or series_digest or run_id or feature:
            b = _bound(session, source, series_digest, run_id, feature)
            if not b.resolved:
                return _series_problem_result("regulated_basin", b, [b.slot])
            return _regulated_verdict(session, b.gauge_id, b.describe())
        return _regulated_verdict(session)
    except Exception as exc:
        return _tool_error_to_dict(exc)


def _stationarity_verdict(dates: list, q_vals: list, affected: list[str], series_md: dict | None = None) -> dict:
    extra = {"series": series_md} if series_md else {}
    if not dates or not q_vals or len(dates) != len(q_vals):
        return ValidatorResult(
            validator="stationarity",
            status="insufficient_data",
            message=(
                "Streamflow time-series arrays (dates, q_cms) not found or "
                "mismatched in the streamflow slot."
            ),
            affected_slots=affected,
            recommendation=(
                "The streamflow slot may have been stored in a compact form "
                "(arrays stripped to save context). Re-fetch streamflow data to "
                "populate the full arrays, or ensure _data_file is set."
            ),
            metadata=extra,
        ).model_dump()

    # Annual totals by USGS water year (Oct 1 starts WY, labelled by the ending year: aihydro-core water_year,
    # the one convention every Wave 3 producer and consumer shares).
    import pandas as _pd
    from aihydro_core.science import water_year as _water_year
    from collections import defaultdict as _dd
    keep_d: list = []
    keep_q: list[float] = []
    for date_str, q in zip(dates, q_vals):
        if q is None or isinstance(q, bool) or not isinstance(q, (int, float)):
            continue
        if q != q or q in (float("inf"), float("-inf")) or q < 0:
            continue          # NaN / inf / negative discharge is missing, not data
        keep_d.append(str(date_str)[:10])
        keep_q.append(float(q))
    ts = _pd.to_datetime(_pd.Series(keep_d, dtype="object"), errors="coerce", format="%Y-%m-%d")
    ok = ts.notna().to_numpy()
    year_totals: dict[int, list[float]] = _dd(list)
    if ok.any():
        wys = _water_year(_pd.DatetimeIndex(ts[ok]))
        for wy, q in zip(wys, (v for v, k in zip(keep_q, ok) if k)):
            year_totals[int(wy)].append(q)

    # Only keep water years with ≥ 330 days of data (avoid incomplete years)
    annual: list[tuple[int, float]] = sorted(
        (wy, sum(vals)) for wy, vals in year_totals.items() if len(vals) >= 330
    )

    if len(annual) < 5:
        return ValidatorResult(
            validator="stationarity",
            status="insufficient_data",
            message=(
                f"Only {len(annual)} complete water year(s) found "
                f"(need ≥ 5 for a meaningful trend test)."
            ),
            affected_slots=affected,
            recommendation="Extend the record to at least 5 complete water years.",
            metadata=extra,
        ).model_dump()

    years = [a[0] for a in annual]
    totals = [a[1] for a in annual]

    # Mann-Kendall via scipy.stats.kendalltau (available as a scipy dep)
    try:
        from scipy.stats import kendalltau as _kt
        tau, p_value = _kt(years, totals)
    except ImportError:
        return ValidatorResult(
            validator="stationarity",
            status="insufficient_data",
            message=(
                "scipy is not installed — Mann-Kendall trend test could not run."
            ),
            affected_slots=affected,
            recommendation="Install scipy (pip install scipy) to enable stationarity testing.",
            metadata=extra,
        ).model_dump()

    import math as _m
    if not (_m.isfinite(tau) and _m.isfinite(p_value)):
        return ValidatorResult(
            validator="stationarity",
            status="insufficient_data",
            message="Mann-Kendall statistic is not defined for these annual totals "
                    "(e.g. constant series); trend cannot be assessed.",
            affected_slots=affected,
            metadata=extra,
        ).model_dump()

    direction = "increasing" if tau > 0 else "decreasing"
    n_years = len(annual)
    wy_md = {"water_year_convention": "USGS water year labelled by its ending year (aihydro-core water_year)",
             "first_water_year": years[0], "last_water_year": years[-1]}

    if p_value < 0.05:
        return ValidatorResult(
            validator="stationarity",
            status="warning",
            severity="high",
            message=(
                f"Significant {direction} trend in annual streamflow totals "
                f"over {n_years} water years (Mann-Kendall τ = {tau:.3f}, "
                f"p = {p_value:.4f} < 0.05). Flood-frequency analysis assumes "
                "stationarity — this assumption may be violated."
            ),
            recommendation=(
                "Add a limitations entry: 'Statistically significant "
                f"{direction} streamflow trend detected (p = {p_value:.4f}); "
                "classical FFA stationarity assumption may not hold.' "
                "Consider non-stationary FFA (e.g. GEV with time covariate) "
                "or climate-corrected analysis."
            ),
            affected_slots=affected,
            metadata={
                "quality_flag": "non_stationary",
                "mann_kendall_tau": round(tau, 4),
                "p_value": round(p_value, 4),
                "n_water_years": n_years,
                "trend_direction": direction,
                **wy_md, **extra,
            },
        ).model_dump()

    return ValidatorResult(
        validator="stationarity",
        status="pass",
        message=(
            f"No significant trend in annual streamflow totals over {n_years} "
            f"water years (Mann-Kendall τ = {tau:.3f}, p = {p_value:.4f} ≥ 0.05). "
            "Stationarity assumption is not violated."
        ),
        affected_slots=affected,
        metadata={
            "mann_kendall_tau": round(tau, 4),
            "p_value": round(p_value, 4),
            "n_water_years": n_years,
            **wy_md, **extra,
        },
    ).model_dump()


@mcp.tool()
def check_stationarity(
    session_id: str,
    source: Literal["signatures", "streamflow"] = "streamflow",
    series_digest: str | None = None,
    run_id: str | None = None,
    feature: str | None = None,
) -> dict:
    """
    Test for a statistically significant trend in annual streamflow totals.

    Applies the Mann-Kendall rank-correlation trend test (Kendall 1975;
    Mann 1945) to annual streamflow totals (USGS water years, aihydro-core
    ``water_year``) derived from the series being judged: the session
    streamflow slot (source="streamflow", default) or the series
    extract_hydrological_signatures consumed (source="signatures", retained and
    digest-bound). A significant positive or negative trend (p < 0.05) indicates
    non-stationarity, which invalidates the classical Flood Frequency Analysis
    assumption of identically distributed annual maxima.

    A different series than the one named (series_digest) is never validated: the
    result is insufficient_data with series_mismatch.

    Requires at least 5 years of data for a meaningful test.
    Degrades gracefully when scipy is unavailable.
    """
    try:
        session_id = resolve_existing_study(session_id)
        session = HydroSession.load(session_id)
        if source != "streamflow" or series_digest or run_id or feature:
            b = _bound(session, source, series_digest, run_id, feature)
            if not b.resolved:
                return _series_problem_result("stationarity", b, [b.slot])
            return _stationarity_verdict(b.dates, b.q, [b.slot], b.describe())
        sf = session.get("streamflow")
        if not sf:
            return ValidatorResult(
                validator="stationarity",
                status="insufficient_data",
                message="No streamflow slot found. Fetch streamflow data first.",
                affected_slots=["streamflow"],
                recommendation="Call fetch_streamflow_data or data_fetch(variable='streamflow', ...).",
            ).model_dump()

        data = sf.get("data", {})
        dates: list = data.get("dates", [])
        q_vals: list = data.get("q_cms", [])

        # Large arrays are stripped to counts when saved (session store threshold = 50).
        # Load them back from the _data_file path if available.
        if (not dates or not q_vals) and data.get("_data_file"):
            try:
                import json as _json
                from pathlib import Path as _PF
                raw = _json.loads(_PF(data["_data_file"]).read_text())
                dates = raw.get("dates", dates)
                q_vals = raw.get("q_cms", q_vals)
            except Exception:
                pass
        return _stationarity_verdict(dates, q_vals, ["streamflow"])
    except Exception as exc:
        return _tool_error_to_dict(exc)


def run_signature_validators(session_id: str, bound, basin_gauge: str | None = None) -> list[dict]:
    """Validator results for the series a signatures call just consumed, bound by digest (no slot lookup).

    Run by ``extract_hydrological_signatures`` and appended to its ``quality_flags``: record length, qualification
    codes, stationarity and regulated basin. ``bound`` is a resolved ``BoundSeries`` or the problem that stopped it;
    in the latter case each validator reports it (``series_not_retained`` / ``series_mismatch``) instead of running
    on some other series. A validator that raises becomes an ``error`` flag, never a failed signatures call.
    """
    out: list[dict] = []
    session = HydroSession.load(session_id)
    jobs = (
        ("record_length", lambda: _record_length_for_series(bound)),
        ("usgs_qualification_codes", lambda: _qualification_for_series(bound)),
        ("stationarity", lambda: _stationarity_verdict(bound.dates, bound.q, [bound.slot], bound.describe())),
        ("regulated_basin", lambda: _regulated_verdict(session, basin_gauge, bound.describe())),
    )
    for name, job in jobs:
        try:
            out.append(_series_problem_result(name, bound, [bound.slot]) if not bound.resolved else job())
        except Exception as exc:                                              # noqa: BLE001
            log.warning("signatures validator %s raised: %s", name, exc)
            out.append({"validator": name, "status": "error", "message": str(exc), "severity": None})
    return out


def check_uncertainty_present(session_id: str, slot: str = "signatures") -> dict:
    """
    Check that a session result slot contains uncertainty (bootstrap CI) data.

    Fires as a post-run validator for analysis tools that should produce
    uncertainty estimates. Returns a warning quality_flag when '_uncertainty'
    is absent, so the agent knows to interpret results as point estimates only.

    This is NOT an @mcp.tool() — it is registered via register_post_validator
    and fires automatically after Tier 1 analysis tools complete.
    """
    try:
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        result = session.get(slot)

        if result is None:
            return ValidatorResult(
                validator="uncertainty_present",
                status="insufficient_data",
                message=f"Slot '{slot}' not found in session.",
                affected_slots=[slot],
                recommendation=f"Run the analysis tool that populates '{slot}' first.",
            ).model_dump()

        data = result.get("data", {}) if isinstance(result, dict) else {}
        # C3 results nest data under feature_id → params_key → {data, meta, uncertainty}.
        # Walk one level: check direct 'data' and also first feature's first result.
        has_uncertainty = bool(data.get("_uncertainty"))   # an empty block is not uncertainty
        if not has_uncertainty:
            for fid_val in data.values():
                if isinstance(fid_val, dict):
                    for pk_val in fid_val.values():
                        if isinstance(pk_val, dict) and (pk_val.get("data") or {}).get("_uncertainty"):
                            has_uncertainty = True
                            break
                if has_uncertainty:
                    break

        if not has_uncertainty:
            return ValidatorResult(
                validator="uncertainty_present",
                status="warning",
                severity="medium",
                message=(
                    f"Slot '{slot}' result has no '_uncertainty' key — "
                    "all values are point estimates without confidence intervals."
                ),
                recommendation=(
                    "The analysis ran without bootstrap CI computation (possibly "
                    "insufficient data length, n<30). Treat numeric values as "
                    "point estimates. For defensible claims, ensure ≥30 data "
                    "points and re-run the analysis."
                ),
                affected_slots=[slot],
                metadata={"quality_flag": "uncertainty_missing"},
            ).model_dump()

        return ValidatorResult(
            validator="uncertainty_present",
            status="pass",
            message=f"Slot '{slot}' contains bootstrap CI uncertainty estimates.",
            affected_slots=[slot],
        ).model_dump()

    except Exception as exc:
        return _tool_error_to_dict(exc)
