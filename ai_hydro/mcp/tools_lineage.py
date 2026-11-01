"""
Provenance view: what produced a run or claim, and what depends on it.

  trace_lineage   walk the declared edges of one study's run records and claims

Read-only. The walk is a plain Python traversal in ``ai_hydro.session.lineage`` over edges the
sealed records already carry (``parents``, run-valued ``input_refs``, claim ``evidence_spans``).
There is no graph store and nothing is inferred.
"""
from __future__ import annotations

import logging

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _resolve_session, _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp.lineage")

_DIRECTIONS = ("up", "down", "both")


@mcp.tool()
def trace_lineage(
    node: str,
    direction: str = "both",
    max_depth: int = 8,
    max_nodes: int = 200,
    session_id: str | None = None,
) -> dict:
    """
    What produced this run or claim (up), and what depends on it (down), from the study's recorded
    edges. node: a run_id or claim id (use run:<id> or claim:<id> if both exist).

    Only declared edges are followed: a run's parents, run-valued input refs, and a claim's run
    evidence. Each run node carries edge_state and parents_known. A fetch has no parents by
    construction (acquisition_root, known). A sealed record with no parent and no reason is
    undeclared: the record does not say where its inputs came from, which is not the same as none.
    So are no_record (legacy row), declared_unresolved (the producer said it could not name its
    parent) and not_in_log.

    upstream.complete is true only if no node reached has unknown parents and the walk was not cut by
    max_depth or max_nodes. downstream.complete is true only if no later run has undeclared inputs
    and the walk was not cut; may_also_depend lists those runs. Use it before re-running or
    retracting something, and say "unknown" where it says so. Each call seals its own run row.

    Returns {found, node, upstream, downstream, edge_coverage}.
    """
    try:
        if direction not in _DIRECTIONS:
            return {"error": True, "code": "INVALID_PARAMETER",
                    "message": f"direction must be one of {list(_DIRECTIONS)}, got {direction!r}."}
        if not isinstance(max_depth, int) or isinstance(max_depth, bool) or not 1 <= max_depth <= 50:
            return {"error": True, "code": "INVALID_PARAMETER",
                    "message": f"max_depth must be an integer from 1 to 50, got {max_depth!r}."}
        if not isinstance(max_nodes, int) or isinstance(max_nodes, bool) or not 1 <= max_nodes <= 1000:
            return {"error": True, "code": "INVALID_PARAMETER",
                    "message": f"max_nodes must be an integer from 1 to 1000, got {max_nodes!r}."}
        session_id = _resolve_session(
            session_id, None, allow_auto_create=False, allow_recent_fallback=False, must_exist=True)
        from ai_hydro.session import HydroSession
        from ai_hydro.session import lineage

        session = HydroSession.load(session_id)
        out = lineage.trace(dict(session.get("_run_log") or {}), dict(session.claims or {}),
                            node, direction, max_depth, max_nodes)
        return {"session_id": session_id, **out}
    except Exception as e:
        log.error("trace_lineage failed: %s", e)
        return _tool_error_to_dict(e)
