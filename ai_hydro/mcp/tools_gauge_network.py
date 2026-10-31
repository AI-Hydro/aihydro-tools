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
def gauge_network_context(gauge_id: str, distance_km: int = 100) -> dict:
    """
    Streamgages upstream and downstream of a USGS gauge, within distance_km along the river network.

    United States only (NLDI, NHDPlus V2). Outside it, or for an id NLDI does not know, the answer is
    available=false with a code; nothing is guessed. Lists only sites with a daily-mean discharge
    series (lakes and other NWIS sites are dropped and counted), each with name, location, drainage
    area (USGS, square miles) and its ratio to this gauge, record begin/end and mainstem PID. Reports
    the gauge's Geoconnex PID, the request URLs and response digests.

    A neighbour is a network fact, not evidence that its flows are independent or unregulated.
    Changes nothing in the study; makes network requests.

    gauge_id: USGS site number, e.g. 01013500 or USGS-01013500.
    distance_km: search radius along the network, 1 to 500 (default 100).
    """
    try:
        from ai_hydro.data import gauge_network as gn

        try:
            site = gn.parse_gauge_id(gauge_id)
            from aihydro_watershed.identity import resolve_geoconnex_gage

            return gn.gauge_network(site, distance_km, geoconnex=resolve_geoconnex_gage)
        except gn.NotAvailable as exc:
            return {"available": False, "code": exc.code, "message": exc.message, "limits": gn.LIMITS}
        except gn.NetworkLookupError as exc:
            return {"error": True, "code": exc.code, "message": exc.message, "retryable": exc.retryable,
                    "recovery": "Retry later." if exc.retryable else "Check the gauge id and distance_km."}
        except ValueError as exc:
            return {"error": True, "code": "INVALID_PARAMETER", "message": str(exc)}
    except Exception as exc:
        log.error("gauge_network_context failed: %s", exc)
        return _tool_error_to_dict(exc)
