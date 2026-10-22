"""
Deterministic skeptic checks — no NLI, no external calls.

Each check takes a session (HydroSession) and optional interpretation text,
and returns a list[SkepticIssue].
"""
from __future__ import annotations

import math
import re
from typing import Any, TYPE_CHECKING

from ai_hydro import identity
from ai_hydro.skeptic.models import SkepticIssue, SkepticReport

if TYPE_CHECKING:
    from ai_hydro.session.store import HydroSession

# USGS site IDs mentioned in text (8-15 digits; one rule in ai_hydro.identity)
_GAUGE_RE = identity.USGS_SITE_ID_SEARCH_RE

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
    # (basins labels, plus basin_refs ids/labels and the usgs alias ids of the
    # session's delineated BasinRef when a claim binds to it)
    covered: set[str] = set()
    for claim_dict in session.claims.values():
        covered |= identity.claim_covered_ids(claim_dict.get("scope", {}),
                                              identity.retained_refs(session, claim_dict))

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
                "Consider surfacing it with add_claim(claim_type='methodological', status='proposed', ...) "
                "stating what the claims rely on."
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
# Check 5 — Metric conflicts: one metric, one feature, one period, two values
# ---------------------------------------------------------------------------

#: Definitional, comparable metrics: the hydrologic signature set (what ``extract_hydrological_signatures``
#: and ``separate_baseflow`` report). Model-skill keys (nse, kge, ...) and generic keys (mean, value) are
#: deliberately absent: two models legitimately score differently on the same gauge and period.
_SIGNATURE_METRICS = frozenset({
    "q_mean", "q_std", "q5", "q95", "q_median", "baseflow_index", "runoff_ratio", "stream_elas",
    "high_q_freq", "high_q_dur", "low_q_freq", "low_q_dur", "zero_q_freq", "flow_variability",
    "hfd_mean", "half_flow_date_std", "slope_fdc",
})

#: Names that denote one quantity. Anything not listed stands for itself (normalised).
_METRIC_ALIASES: dict[str, str] = {
    "bfi": "baseflow_index",
    "baseflow_index": "baseflow_index",
    "base_flow_index": "baseflow_index",
    "bfi_mean": "baseflow_index",
    "rr": "runoff_ratio",
    "runoff_ratio": "runoff_ratio",
    "runoff_coefficient": "runoff_ratio",
}

#: Relative difference above which two values of one metric are a conflict. An engineering
#: threshold (rounding and float noise stay below it), not a statistical one.
METRIC_CONFLICT_REL_TOL = 0.01

_FEATURE_KEYS = ("feature_id", "gauge_id", "site_id", "basin_id")
_METHOD_KEYS = ("method", "baseflow_method", "filter", "algorithm")
_MODEL_KEYS = ("model", "model_name", "backend", "architecture", "model_type")
_PERIOD_KEYS = ("start_date", "end_date", "years", "water_year", "water_years", "period")
#: Output sub-dicts that merely wrap a tool's metrics. Any other nesting (obs/sim, per-lead, per-basin ...)
#: means the leaf is not "the metric for this feature and period", so it is not compared.
_CONTAINERS = frozenset({"signatures", "data", "metrics", "key_outputs", "result", "results"})


def canonical_metric(name: Any) -> str:
    """``BFI`` / ``baseflow-index`` -> ``baseflow_index``; unknown names are normalised only."""
    key = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    return _METRIC_ALIASES.get(key, key)


def _first(keys: tuple[str, ...], *sources: Any) -> Any:
    for src in sources:
        if isinstance(src, dict):
            for k in keys:
                if src.get(k) not in (None, ""):
                    return src[k]
    return None


def _numeric_items(outputs: Any) -> list[tuple[str, float, str]]:
    """Finite numeric ``(name, value, parent_path)``: top level, plus the direct children of container
    sub-dicts (``signatures``, ``data``, ...). Other nesting is skipped, so ``obs.bfi`` and ``sim.bfi``
    of one run never meet."""
    found: list[tuple[str, float, str]] = []
    if not isinstance(outputs, dict):
        return found

    def num(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

    for k, v in outputs.items():
        if num(v):
            found.append((str(k), float(v), ""))
        elif isinstance(v, dict) and str(k).lower() in _CONTAINERS:
            found.extend((str(kk), float(vv), str(k)) for kk, vv in v.items() if num(vv))
    return found


def find_metric_conflicts(session: "HydroSession", rel_tol: float = METRIC_CONFLICT_REL_TOL) -> list[dict]:
    """Same signature metric (aliases resolved), same feature, same *known* period, different values.

    Reads the run log. Per (tool, method/model, container) only the latest run counts, so recomputing
    one tool does not conflict with itself, and one run never conflicts with itself. Runs whose period is
    unknown are not compared (the "period unspecified" bucket stays unflagged). Values from different
    tools or methods that agree within ``rel_tol`` are concordant. Each conflict lists every observation
    with its method and run id.
    """
    try:
        rows = session.get("_run_log") or {}
    except Exception:
        return []
    latest: dict[tuple, dict[tuple, dict]] = {}
    for run_id, row in rows.items():
        if not isinstance(row, dict) or row.get("error") or row.get("minimal"):
            continue
        outputs, inputs = row.get("key_outputs") or {}, row.get("inputs") or {}
        period = tuple(str(_first((k,), inputs, outputs) or "") for k in _PERIOD_KEYS)
        if not any(period):
            continue                                    # period unspecified: never flagged
        tool = str(row.get("tool_name") or row.get("slot") or "")
        method = "/".join(str(x) for x in (_first(_METHOD_KEYS, inputs, outputs) or tool,
                                           _first(_MODEL_KEYS, inputs, outputs) or "") if x)
        feature = str(_first(_FEATURE_KEYS, inputs, outputs) or getattr(session, "site_id", "") or "")
        seen_in_run: set[str] = set()
        for name, value, parent in _numeric_items(outputs):
            metric = canonical_metric(name)
            if metric not in _SIGNATURE_METRICS or metric in seen_in_run:
                continue
            seen_in_run.add(metric)
            obs = {"value": value, "method": method, "tool": tool, "run_id": str(run_id),
                   "metric_name": name, "_ts": str(row.get("timestamp") or "")}
            slot = latest.setdefault((metric, feature, period), {})
            key = (tool, method, parent)
            if key not in slot or obs["_ts"] >= slot[key]["_ts"]:
                slot[key] = obs
    conflicts: list[dict] = []
    for (metric, feature, period), by_src in sorted(latest.items()):
        vals = [o["value"] for o in by_src.values()]
        if len(vals) < 2:
            continue
        hi, lo = max(vals), min(vals)
        if hi - lo <= rel_tol * max(abs(hi), abs(lo), 1e-12):
            continue
        p = dict(zip(_PERIOD_KEYS, period))
        conflicts.append({
            "metric": metric, "feature": feature,
            "period": {k: (v or None) for k, v in p.items() if v} | {"start": p["start_date"] or None,
                                                                   "end": p["end_date"] or None},
            "observations": [{k: v for k, v in o.items() if k != "_ts"}
                             for o in sorted(by_src.values(), key=lambda o: (o["value"], o["run_id"]))],
            "spread": hi - lo,
            "methods_differ": len({o["method"] for o in by_src.values()}) > 1,
        })
    return conflicts


def check_metric_conflicts(session: "HydroSession") -> list[SkepticIssue]:
    issues: list[SkepticIssue] = []
    for c in find_metric_conflicts(session):
        shown = "; ".join(f"{o['value']:.4g} ({o['method']}, run {o['run_id']})" for o in c["observations"])
        issues.append(SkepticIssue(
            issue_type="metric_conflict",
            severity="warning",
            description=(
                f"Metric '{c['metric']}' has {len(c['observations'])} different values for "
                f"feature '{c['feature'] or '?'}' over the same period: {shown}."
            ),
            recommendation=(
                "State which method/run each reported value comes from, or re-run the older one with "
                "the current implementation (clear_session the slot first). Cite only one value per "
                "claim, with its method."
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
    issues.extend(check_metric_conflicts(session))

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
