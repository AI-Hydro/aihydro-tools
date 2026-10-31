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


# --------------------------------------------------------------------------- #
# The study graph: runs and claims, over the edges the records actually declare
# --------------------------------------------------------------------------- #

#: How a run's own record describes where its inputs came from. Only ``declared`` and
#: ``acquisition_root`` say the parent list is complete.
DECLARED = "declared"                        # parents (or run-valued input_refs) are listed
DECLARED_UNRESOLVED = "declared_unresolved"  # the producer said it could not name its parent
ACQUISITION_ROOT = "acquisition_root"        # no parents by construction: it fetched external data
NO_RECORD = "no_record"                      # a legacy or unsealed row: nothing can be said
NOT_IN_LOG = "not_in_log"                    # named as a parent or evidence, but absent from the run log
#: UNDECLARED (above) is the fifth case: a sealed record with no parent and no reason. It is not a root.

KNOWN_COMPLETE = (DECLARED, ACQUISITION_ROOT)


def edge_state(row: Any) -> str:
    """Classify what ``row`` says about its own inputs (see the constants above)."""
    rec = record_of(row)
    if rec is None:
        return NO_RECORD
    if rec.get("parents"):
        return DECLARED
    extra = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
    if extra.get("parent_unresolved"):
        return DECLARED_UNRESOLVED
    if acquires_external_data(row):
        return ACQUISITION_ROOT
    return UNDECLARED


def run_parents(row: Any, run_ids: Any) -> List[tuple]:
    """``[(parent_run_id, via)]``: ``record.parents`` first, then run-valued ``input_refs`` not already listed."""
    rec = record_of(row)
    if rec is None:
        return []
    out: List[tuple] = []
    seen = set()
    for p in rec.get("parents") or []:
        if isinstance(p, str) and p and p not in seen:
            seen.add(p)
            out.append((p, "parents"))
    for ref in rec.get("input_refs") or []:
        r = ref.get("ref") if isinstance(ref, dict) else None
        if isinstance(r, str) and r in run_ids and r not in seen and r != rec.get("run_id"):
            seen.add(r)
            out.append((r, "input_refs"))
    return out


def claim_evidence(claim: Any) -> List[tuple]:
    """``[(kind, source_id, metric_ref)]`` from a claim's evidence spans; ``kind`` is the span's ``source_type``."""
    spans = claim.get("evidence_spans") if isinstance(claim, dict) else None
    out: List[tuple] = []
    for sp in spans or []:
        if isinstance(sp, dict) and sp.get("source_id"):
            out.append((str(sp.get("source_type") or "run"), str(sp["source_id"]), sp.get("metric_ref")))
    return out


#: This view's own run rows (one per call) are not results anything depends on; they are left out of
#: ``may_also_depend`` and of the coverage counts so that looking does not change what is seen.
VIEW_TOOLS = frozenset({"trace_lineage"})


def _stamp(row: Any) -> str:
    rec = record_of(row) or {}
    return str((row or {}).get("timestamp") or rec.get("recorded_at") or "")


def resolve_node(run_log: Dict[str, Any], claims: Dict[str, Any], node: str) -> tuple:
    """``(kind, id)`` for ``node``: ``run:<id>`` / ``claim:<id>`` prefixes win; a bare id is a run, else a claim,
    else ``("unknown", id)``. An id that is both a run and a claim is ambiguous: ``("ambiguous", id)``."""
    node = (node or "").strip()
    if node.startswith("run:"):
        return ("run", node[4:]) if node[4:] in run_log else ("unknown", node[4:])
    if node.startswith("claim:"):
        return ("claim", node[6:]) if node[6:] in claims else ("unknown", node[6:])
    in_run, in_claim = node in run_log, node in claims
    if in_run and in_claim:
        return "ambiguous", node
    return ("run", node) if in_run else ("claim", node) if in_claim else ("unknown", node)


def trace(run_log: Dict[str, Any], claims: Dict[str, Any], node: str, direction: str = "both",
          max_depth: int = 8, max_nodes: int = 200) -> Dict[str, Any]:
    """What produced ``node`` (``up``) and what depends on it (``down``), over declared edges only.

    A node is ``run:<id>``, ``claim:<id>`` or a bare id. Edges: ``record.parents`` and run-valued
    ``input_refs`` (run -> run) and claim ``evidence_spans`` of type run (claim -> run). Nothing is
    inferred. Every run node carries ``edge_state`` and ``parents_known``: "no parents" (a fetch, which
    has none) is different from "the record does not say" (``undeclared``, ``no_record``, ``declared_unresolved``,
    ``not_in_log``), and ``complete`` is true only when no node reached is of the second kind. Downstream,
    ``may_also_depend`` lists later runs whose inputs are not declared, which the walk cannot exclude.
    """
    run_ids = set(run_log)
    kind, nid = resolve_node(run_log, claims, node)
    if kind in ("unknown", "ambiguous"):
        return {"found": False, "node": nid, "kind": kind,
                "reason": ("matches both a run and a claim; use run:<id> or claim:<id>" if kind == "ambiguous"
                           else "no run or claim with this id in the study")}

    # reverse index: id -> [(child_id, child_kind, via)]
    children: Dict[str, List[tuple]] = {}
    for rid, row in run_log.items():
        for p, via in run_parents(row, run_ids):
            children.setdefault(p, []).append((rid, "run", via))
    for cid, cl in claims.items():
        for sk, sid, _m in claim_evidence(cl):
            if sk == "run":
                children.setdefault(sid, []).append((f"claim:{cid}", "claim", "evidence_span"))

    def describe(k: str, i: str) -> Dict[str, Any]:
        if k == "run":
            row = run_log.get(i)
            st = edge_state(row) if row is not None else NOT_IN_LOG
            d = {"id": i, "kind": "run", "tool": tool_of(row) if row is not None else None, "edge_state": st,
                 "parents_known": st in KNOWN_COMPLETE, "recorded_at": _stamp(row) or None}
            if row is not None and (record_of(row) or {}).get("status") not in (None, "ok"):
                d["status"] = (record_of(row) or {}).get("status")
            if st == DECLARED_UNRESOLVED:
                d["reason"] = str(((record_of(row) or {}).get("extra") or {}).get("parent_unresolved"))[:200]
            return d
        if k == "claim":
            cl = claims.get(i) or {}
            return {"id": f"claim:{i}", "kind": "claim", "status": cl.get("status"),
                    "parents_known": True}      # a claim lists its evidence; it can still be empty
        return {"id": i, "kind": k, "parents_known": True}

    def key(k: str, i: str) -> str:
        return f"claim:{i}" if k == "claim" else i

    start = describe(kind, nid)
    out: Dict[str, Any] = {"found": True, "node": start}
    truncated = False

    def walk(up: bool) -> Dict[str, Any]:
        nonlocal truncated
        nodes: List[Dict[str, Any]] = []
        seen = {key(kind, nid)}
        frontier = [(kind, nid, 0)]
        unknown: List[str] = []
        if up and start["kind"] == "run" and not start["parents_known"]:
            unknown.append(start["id"])
        while frontier:
            k, i, depth = frontier.pop(0)
            if depth >= max_depth:
                continue
            if up:
                if k == "run":
                    nxt = [("run", p, via) for p, via in run_parents(run_log.get(i), run_ids)]
                elif k == "claim":
                    nxt = [("run" if sk == "run" else sk, sid, "evidence_span") for sk, sid, _m in claim_evidence(claims.get(i))]
                else:
                    nxt = []
            else:
                nxt = [(ck, (cid[6:] if ck == "claim" else cid), via) for cid, ck, via in children.get(i, [])] \
                    if k == "run" else []
            for nk, ni, via in nxt:
                kk = key(nk, ni)
                if kk in seen:
                    continue
                if len(nodes) >= max_nodes:
                    truncated = True
                    return {"nodes": nodes, "unknown": unknown}
                seen.add(kk)
                d = describe(nk, ni)
                d.update(depth=depth + 1, via=via)
                nodes.append(d)
                if up and d["kind"] == "run" and not d["parents_known"]:
                    unknown.append(d["id"])
                frontier.append((nk, ni, depth + 1))
        return {"nodes": nodes, "unknown": unknown}

    if direction in ("up", "both"):
        up = walk(True)
        out["upstream"] = {"nodes": up["nodes"], "complete": not up["unknown"] and not truncated,
                           "parents_unknown_at": up["unknown"]}
    if direction in ("down", "both"):
        dn = walk(False)
        reached = {n["id"] for n in dn["nodes"]} | {start["id"]}
        later = []
        if start["kind"] == "run":
            t0 = start.get("recorded_at") or ""
            for rid, row in run_log.items():
                if rid in reached or tool_of(row) in VIEW_TOOLS:
                    continue
                st = edge_state(row)
                if st in KNOWN_COMPLETE:
                    continue
                if t0 and _stamp(row) and _stamp(row) < t0:
                    continue                        # recorded before this node existed: cannot depend on it
                later.append({"id": rid, "tool": tool_of(row), "edge_state": st})
        out["downstream"] = {"nodes": dn["nodes"], "complete": not later and not truncated,
                             "may_also_depend": later[:50], "may_also_depend_n": len(later)}
    cov: Dict[str, int] = {}
    n_runs = 0
    for row in run_log.values():
        if tool_of(row) in VIEW_TOOLS:
            continue
        n_runs += 1
        st = edge_state(row)
        cov[st] = cov.get(st, 0) + 1
    out["edge_coverage"] = {"runs": n_runs, **cov}
    if truncated:
        out["truncated"] = True
    return out
