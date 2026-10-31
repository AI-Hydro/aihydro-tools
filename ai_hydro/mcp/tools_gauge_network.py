"""
Gauge network context: which streamgages sit upstream and downstream of a gauge.

  gauge_network_context   NLDI + Water Data API + Geoconnex; US only; changes nothing in the study

The map trace and any nested-basin check build on this answer. See ``ai_hydro.data.gauge_network``.
"""
from __future__ import annotations

import logging

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp.gauge_network")


@mcp.tool()
def gauge_network_context(gauge_id: str, distance_km: int = 100, session_id: str | None = None) -> dict:
    """
    Streamgages upstream and downstream of a USGS gauge, within distance_km along the river network.

    United States only (NLDI, NHDPlus V2). Outside it, or for an id NLDI does not know, the answer is
    available=false with a code; nothing is guessed. Lists only sites with a daily-mean discharge
    series (others are dropped and counted), nearest first, at most 20 per side, keyed by USGS id so
    [run:<id>#key_outputs.upstream_gauges.USGS-0xxxxxxx.drainage_area_sqmi] cites one. Each has name,
    location, drainage area (USGS, square miles) and its ratio to this gauge, and record begin/end.

    Bounded: about 12 requests and 45 s. If a limit, a 429 or a service fault stops it, you still get what
    was gathered with incomplete=true and why. A neighbour is a network fact, not evidence that its flows
    are independent or unregulated. Changes nothing in the study; makes network requests.

    gauge_id: USGS site number, e.g. 01013500 or USGS-01013500.
    distance_km: search radius along the network, 1 to 500 (default 100).
    """
    try:
        from ai_hydro.data import gauge_network as gn

        try:
            site = gn.parse_gauge_id(gauge_id)
            try:
                from aihydro_watershed.identity import resolve_geoconnex_gage as geo
            except Exception:                       # the PID is a nicety: the network answer does not need it
                geo = None
            data = gn.gauge_network(site, distance_km, geoconnex=geo)
        except gn.NotAvailable as exc:
            return {"available": False, "code": exc.code, "message": exc.message, "limits": gn.LIMITS}
        except ValueError as exc:
            return {"error": True, "code": "INVALID_PARAMETER", "message": str(exc)}

        result = {"data": data, "meta": {"tool": "gauge_network_context", "source": "NLDI + USGS Water Data API + Geoconnex",
                                         "params": {"gauge_id": site, "distance_km": distance_km}}}
        sid = None
        try:
            from ai_hydro.mcp.helpers import _resolve_session

            sid = _resolve_session(session_id, None, allow_auto_create=False, allow_recent_fallback=False,
                                   must_exist=bool(session_id))
        except Exception as exc:
            if session_id:
                raise
            log.debug("gauge_network_context: no study resolved, not sealed (%s)", exc)
        if sid:
            from ai_hydro.mcp.enforcement import post_run

            return post_run("gauge_network_context", sid, result, inputs={"gauge_id": site, "distance_km": distance_km})
        return result
    except Exception as exc:
        log.error("gauge_network_context failed: %s", exc)
        return _tool_error_to_dict(exc)
