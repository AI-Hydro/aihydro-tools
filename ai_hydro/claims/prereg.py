"""Pre-registration checks shared by the ledger and the defensibility report (R-04).

"Confirmatory" is a claim of order: the plan existed before the analysis it names was run. So

* a claim's ``prereg_id`` must name THIS study's locked plan (``plan_for_prereg``);
* a planned analysis counts as executed only if a run of that exact tool (or a slot of that exact
  name) happened AFTER ``locked_at`` (``analysis_status``);
* a claim is confirmatory only if every ``run`` span it cites was recorded strictly AFTER ``locked_at``
  (``runs_not_after_lock``); this holds even when a planned analysis is a free-text description that names
  no tool ("baseflow analysis"), where name matching cannot see that it already ran;
* a plan locked after a planned analysis had already run is ``post_hoc``: its claims are not
  confirmatory (``is_post_hoc``). Setup runs that the plan does not name (delineation, data fetch)
  do not make a plan post hoc.

Matching is by exact normalised name (``Compute Flood Frequency`` -> ``compute_flood_frequency``), never
by substring: a tool name that merely occurs inside a longer description is not that analysis.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def norm(name: str) -> str:
    return "_".join(str(name).strip().lower().replace("-", "_").split())


def parse_ts(value: Any) -> datetime | None:
    """ISO-8601 timestamp -> aware UTC datetime; ``None`` if absent or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def locked_plan(session: Any) -> dict | None:
    plan = session.get("_research_plan")
    return plan if isinstance(plan, dict) and plan.get("locked") else None


def plan_for_prereg(session: Any, prereg_id: str | None) -> dict | None:
    """The locked plan iff ``prereg_id`` names it."""
    plan = locked_plan(session)
    return plan if plan and prereg_id and plan.get("prereg_id") == prereg_id else None


def known_names() -> set[str]:
    """Normalised names of registered tools and common slots: what a planned analysis can resolve to."""
    names: set[str] = set()
    try:
        from ai_hydro.mcp.tools_docs import _list_tools_sync
        names |= {norm(t.name) for t in _list_tools_sync()}
    except Exception:
        pass
    try:
        from ai_hydro.session.store import _COMMON_SLOTS
        names |= {norm(x) for x in _COMMON_SLOTS}
    except Exception:
        pass
    return names


def analysis_status(session: Any, plan: dict) -> list[dict]:
    """Per planned analysis: ``{analysis, ran_after_lock, ran_before_lock, unmatched}``.

    ``unmatched``: the entry is neither a registered tool/slot name nor the name of any run in the log, so
    "not yet run" would be a guess; it is reported as unmatched instead.
    """
    locked_at = parse_ts(plan.get("locked_at"))
    run_log = session.get("_run_log") or {}
    events: dict[str, list[datetime | None]] = {}
    for entry in run_log.values():
        if isinstance(entry, dict) and entry.get("tool_name"):
            events.setdefault(norm(entry["tool_name"]), []).append(parse_ts(entry.get("timestamp")))
    for slot in session.computed_healthy():
        meta = (session.get(slot) or {}).get("meta") or {}
        events.setdefault(norm(slot), []).append(parse_ts(meta.get("computed_at")))
    known = known_names() | set(events)
    out = []
    for pa in plan.get("planned_analyses", []):
        times = events.get(norm(pa), [])
        after = bool(locked_at) and any(t is not None and t > locked_at for t in times)
        before = (not locked_at) or any(t is None or t <= locked_at for t in times)
        out.append({"analysis": pa, "ran_after_lock": after, "ran_before_lock": bool(times) and before,
                    "unmatched": norm(pa) not in known})
    return out


def runs_not_after_lock(session: Any, plan: dict, claim: dict) -> list[str]:
    """Cited ``run`` source ids not timestamped strictly after the plan's lock (missing/unreadable = not after)."""
    locked_at = parse_ts(plan.get("locked_at"))
    run_log = session.get("_run_log") or {}
    bad = []
    for sp in claim.get("evidence_spans") or []:
        if not isinstance(sp, dict) or sp.get("source_type") != "run":
            continue
        rid = str(sp.get("source_id"))
        row = run_log.get(rid)
        t = parse_ts(row.get("timestamp")) if isinstance(row, dict) else None
        if locked_at is None or t is None or t <= locked_at:
            if rid not in bad:
                bad.append(rid)
    return bad


def is_post_hoc(session: Any, plan: dict) -> bool:
    """True when a planned analysis had already run when the plan was locked (stamped value wins)."""
    if "post_hoc" in plan:
        return bool(plan["post_hoc"])
    return any(a["ran_before_lock"] for a in analysis_status(session, plan))


def is_confirmatory(session: Any, claim: dict) -> bool:
    return prereg_label(session, claim) == "confirmatory"


def prereg_label(session: Any, claim: dict) -> str:
    """``confirmatory`` | ``post_hoc_plan`` | ``unverified_prereg_id`` | ``exploratory``."""
    pid = claim.get("prereg_id")
    if not pid:
        return "exploratory"
    plan = plan_for_prereg(session, pid)
    if plan is None:
        return "unverified_prereg_id"
    if is_post_hoc(session, plan) or runs_not_after_lock(session, plan, claim):
        return "post_hoc_plan"
    return "confirmatory"
