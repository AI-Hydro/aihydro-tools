"""Does a run trace back to observed data? A walk over the sealed run records' ``parents``.

Used by the answer audit's advisory ``no_observed_lineage`` check. It reads only what the sealed
records say; it never decides what counts as evidence (that stays with ``audit.resolver``).

Three honest outcomes, never collapsed:

``observed``             the run, or a run it declares as a parent (transitively), acquired external data.
``declared_unobserved``  every run on the walk has a sealed record, parents are declared and resolved,
                         and none acquired external data.
``undeclared``           the walk ended at a run with no declared parent that did not itself acquire data.
                         The record cannot say where its inputs came from; that is not "none".
``unknown``              a row is missing, unsealed, or says its parent could not be resolved
                         (``extra.parent_unresolved``). Nothing can be concluded.

"Acquired external data" is a property of the tool, read from the one annotation table
(``tool_annotations.toml``): open-world (it calls a service) and not destructive (it does not run
arbitrary code). No tool names are listed here.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional

OBSERVED = "observed"
DECLARED_UNOBSERVED = "declared_unobserved"
UNDECLARED = "undeclared"
UNKNOWN = "unknown"

MAX_NODES = 500          # a study has tens of runs; this only bounds a corrupt or cyclic log


@functools.lru_cache(maxsize=1)
def _annotation_classes() -> tuple[FrozenSet[str], FrozenSet[str]]:
    """``(acquiring tools, arbitrary-code tools)`` from the annotation table; empty if it cannot be read."""
    try:
        from ai_hydro.mcp.tool_surface import load_annotation_rows

        rows = load_annotation_rows()
    except Exception:
        return frozenset(), frozenset()
    acquiring = frozenset(n for n, r in rows.items() if r.get("openWorldHint") and not r.get("destructiveHint"))
    code = frozenset(n for n, r in rows.items() if r.get("openWorldHint") and r.get("destructiveHint"))
    return acquiring, code


def record_of(row: Any) -> Optional[dict]:
    rec = row.get("record") if isinstance(row, dict) else None
    return rec if isinstance(rec, dict) and rec.get("run_id") else None


def tool_of(row: Any) -> Optional[str]:
    rec = record_of(row)
    return (rec or {}).get("tool") or (row.get("tool_name") if isinstance(row, dict) else None)


def acquires_external_data(row: Any) -> bool:
    """True when this run's own tool calls an external data service and the call succeeded."""
    rec = record_of(row)
    if rec is None or rec.get("status", "ok") != "ok":
        return False
    return tool_of(row) in _annotation_classes()[0]


def runs_arbitrary_code(row: Any) -> bool:
    return tool_of(row) in _annotation_classes()[1]


@dataclass
class Lineage:
    state: str
    observed_via: Optional[str] = None          # the nearest run that acquired data
    path: List[str] = field(default_factory=list)   # run ids from the cited run to that run (observed) or the walk's end
    walked: int = 0
    reasons: List[str] = field(default_factory=list)  # why the answer is unknown / undeclared
    root_tool: Optional[str] = None             # tool of the run the walk ended at (undeclared)


def trace_to_observed(run_log: Dict[str, Any], run_id: str) -> Lineage:
    """Walk ``record.parents`` upward from ``run_id`` and classify what it rests on."""
    seen: Dict[str, Optional[str]] = {run_id: None}      # id -> the child it was reached from
    queue = [run_id]
    reasons: List[str] = []
    ends: List[str] = []                                  # runs with no declared parent
    while queue and len(seen) <= MAX_NODES:
        rid = queue.pop(0)
        row = run_log.get(rid)
        if row is None:
            reasons.append(f"{rid}: not in the run log")
            continue
        rec = record_of(row)
        if rec is None:
            reasons.append(f"{rid}: no sealed record")
            continue
        if acquires_external_data(row):
            chain = [rid]
            while seen.get(chain[-1]):
                chain.append(seen[chain[-1]])             # type: ignore[arg-type]
            return Lineage(OBSERVED, observed_via=rid, path=list(reversed(chain)), walked=len(seen))
        extra = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
        if extra.get("parent_unresolved"):
            reasons.append(f"{rid}: parent unresolved ({extra['parent_unresolved']})")
        parents = [p for p in (rec.get("parents") or []) if isinstance(p, str) and p]
        if not parents:
            ends.append(rid)
        for p in parents:
            if p not in seen:
                seen[p] = rid
                queue.append(p)
    if reasons:
        return Lineage(UNKNOWN, walked=len(seen), reasons=reasons)
    if len(seen) > 1:
        return Lineage(DECLARED_UNOBSERVED, walked=len(seen), path=list(seen), root_tool=tool_of(run_log.get(ends[0])) if ends else None)
    return Lineage(UNDECLARED, walked=1, path=[run_id], root_tool=tool_of(run_log.get(run_id)),
                   reasons=["the record declares no parent and the tool did not acquire data itself"])
