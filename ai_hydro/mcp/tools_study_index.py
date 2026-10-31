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
def find_studies_for_gauge(gauge_id: str, include_hidden: bool = False) -> dict:
    """
    Studies on this machine that are bound to a USGS gauge or hold runs that name it, with those runs.

    A run names a gauge in its recorded outputs, inputs or resolved place; nothing is guessed from a study's
    name. Each study says whether it is bound to the gauge (its own site), how many runs name it, and the runs
    newest first (id, tool, slot, time, where the gauge appears). A study whose run log cannot be read is
    listed with run_log_readable=false: unknown, not empty. Test and archived studies are left out unless
    include_hidden=true.

    gauge_id: USGS site number, e.g. 01013500 or USGS-01013500.
    """
    try:
        from ai_hydro.session.study_index import runs_holding_gauge

        out = runs_holding_gauge(gauge_id, include_hidden=include_hidden)
        if out.get("error"):
            return {"error": True, "code": "INVALID_PARAMETER", "message": f"{gauge_id!r} is not a USGS site number."}
        return out
    except Exception as e:
        log.error("find_studies_for_gauge failed: %s", e)
        return _tool_error_to_dict(e)
