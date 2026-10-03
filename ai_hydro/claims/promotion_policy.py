"""Promotion policy as one pure function (P1 / W1, plan ``b1-p1-eval-harness``).

``promotion_violations(session, claim)`` answers "what would block promoting this
claim to the registry?" from the claim and the session's retained records alone. It
reads, never writes, and never touches the approval store or the revision chain
(those need a human and write rows, so they stay in
``promote_claim_to_registry`` after this policy passes).

Two callers use it, so the advisory a drafting agent sees and the refusal
production issues cannot drift apart:

* ``promote_claim_to_registry`` raises the exception of the FIRST blocking
  violation, in the order below. Codes, messages and envelopes are byte-identical
  to the pre-refactor inline checks (``tests/test_promotion_refusal_golden.py``).
* ``add_claim`` / ``update_claim_status`` attach every violation as
  ``promotion_check`` (advisory; nothing is refused there).

Families (stable labels) and their order of evaluation
------------------------------------------------------
``id``                 INVALID_ID                      stored id outside the safe charset
``basin_identity``     BASIN_REF_REQUIRED, BASIN_REF_UNKNOWN
``evidence``           EVIDENCE_REQUIRED               no evidence span
``limitations``        LIMITATIONS_REQUIRED
``status``             STATUS_NOT_ELIGIBLE
``uncertainty``        UNCERTAINTY_NOT_VERIFIED        metric-scoped claim, flag unset
``observed_modelled``  MODELLED_LIMITATION_REQUIRED    name-based modelled-signature gate
``metric_binding``     EVIDENCE_METRIC_UNAVAILABLE (metric claim, no metric_ref),
                       EVIDENCE_METRIC_MISMATCH
``evidence``           EVIDENCE_UNRESOLVED / _IDENTITY_MISMATCH / _SEAL_INVALID / _CHECK_FAILED /
                       _METRIC_UNAVAILABLE / _UNCERTAINTY_UNAVAILABLE / _INVALID /
                       _MISMATCH                       first failing retained span
                       (these codes are ``registry.evidence.EvidenceError`` codes)
``evidence`` (advisory, non-blocking) EVIDENCE_QUALITY_WARNING

Codes of checks that previously raised a bare ``ValueError`` are new, stable names
for the policy; the production envelope for those refusals is unchanged
(``UNEXPECTED_ERROR`` with the same message), carried by ``Violation.exception``.

The revision-chain / evidence-drift / approval checks are not here: they need the
approval store and record a revision row, so they follow the policy in production.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ai_hydro import identity

# Metric/keyword markers for hydrology signatures that MAY derive from
# modelled (not observed) streamflow — e.g. GEOGLOWS-routed BFI, flood
# frequency, or flow-duration-curve slope on a gauge-less basin. See
# claim_touches_hydrology_signature_metric() docstring for why this is a
# name-based heuristic rather than a true evidence-provenance lookup.
_MODELLED_HYDROLOGY_METRIC_MARKERS = (
    "baseflow_index", "bfi", "flood_freq", "flood frequency", "fdc_slope",
    "flow duration", "q_mean", "q5", "q95", "high_flow", "low_flow",
    "runoff_ratio", "stream_elas", "hfd", "half_flow_date", "flow timing",
)
_MODELLED_LIMITATION_ACKNOWLEDGEMENT_MARKERS = (
    "model", "geoglows", "ungauged", "simulat", "unobserved", "no gauge",
)

PROMOTABLE_STATUSES = ("supported", "weakly_supported")
_NON_PASS_FLAG_STATUSES = {"warning", "insufficient_data"}


@dataclass(frozen=True)
class Violation:
    """One reason a claim cannot (or may not cleanly) be promoted."""
    code: str
    message: str
    family: str
    blocking: bool = True
    # The exception production raises for this violation (kept so envelopes stay
    # byte-identical). Not part of the advisory dict.
    exception: Exception | None = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "family": self.family, "blocking": self.blocking}


@dataclass(frozen=True)
class PromotionEvaluation:
    violations: list
    evidence_versions: dict   # source_id -> fingerprint; valid only if no blocking violation

    @property
    def first_blocking(self) -> Violation | None:
        return next((v for v in self.violations if v.blocking), None)


def claim_requires_uncertainty(claim_dict: dict) -> bool:
    """Return whether an empirical claim is explicitly tied to a metric.

    ``ScientificClaim.claim_type`` has no ``quantitative`` value. Quantitative
    intent is instead made explicit by ``scope.metric`` or an evidence span's
    ``metric_ref``. Keeping the rule tied to those structured fields avoids
    guessing from numbers in prose (which may be dates, gauge IDs, or sample
    counts) and makes the gate reachable through the public schema.
    """
    if claim_dict.get("claim_type") not in {"empirical_result", "negative_result"}:
        return False
    scope = claim_dict.get("scope") or {}
    if scope.get("metric"):
        return True
    return any(
        isinstance(span, dict) and bool(span.get("metric_ref"))
        for span in claim_dict.get("evidence_spans", [])
    )


def claim_touches_hydrology_signature_metric(claim_dict: dict, refs_by_id: dict | None = None) -> bool:
    """
    Heuristic: does this claim's scope.metric or statement reference a
    hydrology-signature metric that may be computed from modelled (not
    observed) streamflow?

    This is a name-based heuristic, not a true evidence-provenance lookup.
    aihydro-lsh's AttributeResult.provenance[family].is_observed and
    value_provenance already record the real answer per attribute value —
    but the lsh_attributes MCP tool does not take a session_id or write to
    the session run log, so a promoted claim's EvidenceSpan cannot currently
    be resolved back to that provenance. Wiring the lsh MCP tool surface into
    the session/run-log system so promotion can do a real lookup is a
    separate, larger architectural task (tracked in audits/STATUS.md as N-12).
    Until then, this keyword gate is the safety net.

    Deliberately does NOT flag a claim whose scope.basins are all
    USGS-gauge-shaped IDs (8-15 digits) — that is the common, legitimate
    CONUS-observed case (e.g. "Q_mean for 01013500"), and flagging it would
    make the gate noise researchers learn to route around rather than a
    signal they act on.
    """
    scope = claim_dict.get("scope") or {}
    basins = scope.get("basins") or []
    if identity.gauge_shaped(basins, scope.get("basin_refs"), refs_by_id):
        return False
    haystack = " ".join([
        str(scope.get("metric") or ""),
        str(claim_dict.get("claim", claim_dict.get("statement", "")) or ""),
    ]).lower()
    return any(marker in haystack for marker in _MODELLED_HYDROLOGY_METRIC_MARKERS)


def _metric_name(name: str) -> str:
    for prefix in ("metric.", "key_outputs.", "data."):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _from_exc(code: str, family: str, exc: Exception, *, blocking: bool = True) -> Violation:
    return Violation(code, str(exc), family, blocking, exc)


def _quality_warnings(session: Any, spans: list[dict]) -> list[dict]:
    """Run-evidence validator flags that are not a pass and not blocking (warning / insufficient_data).

    Uses the same flag locations as ``registry.evidence.validate_span`` and never
    raises: an unresolvable span is already an ``evidence`` violation.
    """
    from ai_hydro.registry.evidence import resolve_source
    found = []
    for span in spans:
        if span.get("source_type") != "run":
            continue
        try:
            record = resolve_source(session, span)
        except Exception:
            continue
        evidence = record.get("evidence")
        payload = evidence if isinstance(evidence, dict) else record
        data = payload.get("data", payload.get("key_outputs", payload))
        flags = payload.get("quality_flags") or (
            data.get("_quality_flags") if isinstance(data, dict) else None) or []
        for flag in flags:
            if isinstance(flag, dict) and str(flag.get("status", "")).lower() in _NON_PASS_FLAG_STATUSES:
                found.append({"source_id": span["source_id"], "validator": flag.get("validator"),
                              "status": str(flag.get("status")).lower()})
    return found


def evaluate_promotion(session: Any, claim: dict, *, claim_id: str | None = None,
                       like: dict | None = None) -> PromotionEvaluation:
    """All promotion violations for ``claim`` plus the verified evidence fingerprints.

    ``claim`` is the session's claim dict. Exceptions other than the structured
    refusals below (e.g. a malformed claim) propagate exactly as the inline checks
    did. ``like``: the claim's latest stored ``evidence_versions``; fingerprints are
    computed in each stored entry's version so an old binding does not drift.
    """
    from ai_hydro.registry.evidence import EvidenceError, verified_versions
    from ai_hydro.session.models import ScientificClaim

    claim_id = claim_id if claim_id is not None else claim.get("id")
    model = ScientificClaim(**claim)
    out: list[Violation] = []

    # id: stored ids that predate the id rule stay readable but cannot be promoted.
    try:
        identity.require_safe_ids(getattr(session, "session_id", None), claim_id, stored=True)
    except identity.InvalidIdError as exc:
        out.append(_from_exc(identity.INVALID_ID, "id", exc))

    # basin_identity: canonical place identity first, fail closed.
    if model.scope.basins and not model.scope.basin_refs:
        exc = identity.BasinRefRequiredError(claim_id, model.scope.basins, session.session_id)
        out.append(_from_exc(identity.BASIN_REF_REQUIRED, "basin_identity", exc))
    retained = identity.retained_refs(session, claim)
    unknown = [e["id"] for e in model.scope.basin_refs or [] if e["id"] not in retained]
    if unknown:
        exc = identity.BasinRefUnknownError(unknown, session.session_id, claim_id)
        out.append(_from_exc(identity.BASIN_REF_UNKNOWN, "basin_identity", exc))

    if not model.evidence_spans:
        out.append(_from_exc("EVIDENCE_REQUIRED", "evidence", ValueError(
            "Promotion requires at least one evidence_span. "
            "Add a typed EvidenceSpan (run, paper, or dataset) via add_claim or update_claim_status.")))
    if not model.limitations:
        out.append(_from_exc("LIMITATIONS_REQUIRED", "limitations", ValueError(
            "Promotion requires at least one limitation to be listed.")))
    if model.status not in PROMOTABLE_STATUSES:
        out.append(_from_exc("STATUS_NOT_ELIGIBLE", "status", ValueError(
            f"Claim status '{model.status}' is not eligible for promotion.")))

    requires_uncertainty = claim_requires_uncertainty(claim)
    if requires_uncertainty and not claim.get("uncertainty_verified"):
        out.append(_from_exc("UNCERTAINTY_NOT_VERIFIED", "uncertainty", ValueError(
            "Metric-scoped empirical claim cannot be promoted without uncertainty_verified=True. "
            "Call update_claim_status(uncertainty_verified=True) after confirming "
            "that an uncertainty estimate is available for the referenced metric.")))
    if claim_touches_hydrology_signature_metric(claim, identity.retained_refs(session, claim)):
        limitations_text = " ".join(model.limitations).lower()
        if not any(w in limitations_text for w in _MODELLED_LIMITATION_ACKNOWLEDGEMENT_MARKERS):
            out.append(_from_exc("MODELLED_LIMITATION_REQUIRED", "observed_modelled", ValueError(
                "Claim references a hydrology-signature metric (e.g. baseflow index, "
                "flood frequency, flow-duration-curve slope) that may be computed from "
                "modelled streamflow (e.g. GEOGLOWS routing on a gauge-less basin) rather "
                "than gauge observations. Promotion requires a limitation that acknowledges "
                "this — e.g. 'Signature computed from modelled GEOGLOWS discharge, not gauge "
                "observations.' If the underlying data is confirmed gauge-observed, add a "
                "limitation stating that explicitly instead.")))

    # metric_binding + retained-evidence verification.
    spans = [s if isinstance(s, dict) else s.model_dump() for s in model.evidence_spans]
    metric_spans = [span for span in spans if span.get("metric_ref")]
    if requires_uncertainty and not metric_spans:
        out.append(_from_exc("EVIDENCE_METRIC_UNAVAILABLE", "metric_binding", EvidenceError(
            "EVIDENCE_METRIC_UNAVAILABLE", claim_id,
            "Metric-scoped claims require an explicit metric_ref on their evidence.")))
    if model.scope.metric:
        if not any(_metric_name(span["metric_ref"]) == _metric_name(model.scope.metric)
                   for span in metric_spans):
            out.append(_from_exc("EVIDENCE_METRIC_MISMATCH", "metric_binding", EvidenceError(
                "EVIDENCE_METRIC_MISMATCH", claim_id,
                "The scope metric does not match any referenced evidence metric.")))
    versions: dict = {}
    try:
        versions = verified_versions(session, spans, require_uncertainty=requires_uncertainty, like=like)
    except EvidenceError as exc:
        out.append(_from_exc(exc.code, "evidence", exc))

    for warn in _quality_warnings(session, spans):
        out.append(Violation(
            "EVIDENCE_QUALITY_WARNING",
            f"Evidence run '{warn['source_id']}' carries a '{warn['status']}' flag from validator "
            f"'{warn['validator']}'. It does not block promotion; state it as a limitation or "
            "re-run the analysis with the issue resolved.",
            "evidence", blocking=False))
    return PromotionEvaluation(out, versions)


def promotion_violations(session: Any, claim: dict, *, claim_id: str | None = None) -> list[Violation]:
    """Every reason ``claim`` would be refused (``blocking``) or flagged, in evaluation order."""
    return evaluate_promotion(session, claim, claim_id=claim_id).violations


def promotion_check(session: Any, claim: dict, *, claim_id: str | None = None) -> list[dict]:
    """Advisory form for tool responses: the violations as plain dicts, never raising.

    A policy failure must not turn a successful drafting call into an error, so an
    unexpected exception becomes one non-blocking ``POLICY_CHECK_UNAVAILABLE`` entry.
    """
    try:
        return [v.to_dict() for v in promotion_violations(session, claim, claim_id=claim_id)]
    except Exception as exc:  # noqa: BLE001 - advisory path must not fail the call
        return [Violation("POLICY_CHECK_UNAVAILABLE",
                          f"Promotion policy could not be evaluated: {exc}",
                          "policy", blocking=False).to_dict()]
