"""
Which studies hold a gauge.

  find_studies_for_gauge   studies bound to a USGS gauge, and the runs in any study that name it

Read-only over the studies on this machine. The same index feeds the gauge layer on the map ("this gauge has N runs in M studies").
"""
from __future__ import annotations

import logging

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp.study_index")


@mcp.tool()
def find_studies_for_gauge(gauge_id: str, scope: str = "workspace", include_hidden: bool = False,
                           session_id: str | None = None) -> dict:
    """
    Studies bound to a USGS gauge or holding runs that name it, with those runs.

    scope "workspace" (default): studies in the current study's workspace folder. "all": every study on this
    machine (they may belong to other projects). A run names a gauge in its recorded outputs, inputs or resolved
    place; nothing is guessed from a name. Each study says whether it is bound to the gauge, how many runs name
    it, and the runs newest first. Up to 50 studies (studies_truncated says more). A study whose run log cannot be
    read is run_log_readable=false: unknown, not empty. Test and archived studies are left out unless
    include_hidden. Workspaces are named by folder name only.

    gauge_id: USGS site number, e.g. 01013500 or USGS-01013500.
    """
    try:
        from ai_hydro.session.study_index import runs_holding_gauge

        if scope not in ("workspace", "all"):
            return {"error": True, "code": "INVALID_PARAMETER", "message": f"scope must be 'workspace' or 'all', got {scope!r}."}
        from ai_hydro.session.study_index import _norm_gauge

        if _norm_gauge(gauge_id) is None:
            return {"error": True, "code": "INVALID_PARAMETER", "message": f"{gauge_id!r} is not a USGS site number."}
        ws = None
        if scope == "workspace":
            try:
                from ai_hydro.mcp.helpers import _resolve_session
                from ai_hydro.session import HydroSession

                sid = _resolve_session(session_id, None, allow_auto_create=False, allow_recent_fallback=False, must_exist=True)
                ws = HydroSession.load(sid).workspace_dir
            except Exception:
                ws = None
            if not ws:
                return {"error": True, "code": "NO_WORKSPACE_SCOPE",
                        "message": "No current study with a workspace folder to scope to.",
                        "recovery": "Pass scope='all' to search every study on this machine, or bind a study first."}
        out = runs_holding_gauge(gauge_id, include_hidden=include_hidden, workspace=ws)
        if out.get("error"):
            return {"error": True, "code": "INVALID_PARAMETER", "message": f"{gauge_id!r} is not a USGS site number."}
        return out
    except Exception as e:
        log.error("find_studies_for_gauge failed: %s", e)
        return _tool_error_to_dict(e)
