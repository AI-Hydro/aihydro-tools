"""
Flowline geometry up and downstream of a gauge, for the map.

  gauge_flowlines   NLDI flowlines (US only); writes a GeoJSON file to the study workspace and returns a summary

The geometry stays out of the model's context: the file is recorded against the run, and ``map_add_layer_from_run`` puts it on the map.
"""
from __future__ import annotations

import hashlib
import json
import logging

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp.flowlines")


@mcp.tool()
def gauge_flowlines(gauge_id: str, direction: str = "both", distance_km: int = 50, session_id: str | None = None) -> dict:
    """
    River flowlines upstream and/or downstream of a USGS gauge, for drawing on the map.

    US only (NLDI, NHDPlus V2); another country's id or an id NLDI does not know gives available=false.
    direction: "upstream" (all tributaries), "downstream" (main stem) or "both". distance_km 1-200 (default 50).
    At most two requests and 1500 features. The GeoJSON (COMID, direction, length per line) is written to the
    study workspace, recorded with this run, and not returned: show it with map_add_layer_from_run(run_id=_run_id).
    Returns counts, lengths, bbox, truncated, geometry_file, request URLs and digests. Lines say nothing about
    regulation or gauge coverage. Changes nothing else in the study; makes network requests.
    """
    try:
        from ai_hydro.data import nldi_flowlines as nf

        try:
            site = nf.parse_gauge_id(gauge_id)
            out = nf.gauge_flowlines(site, direction, distance_km)
        except nf.NotAvailable as exc:
            return {"available": False, "code": exc.code, "message": exc.message, "limits": nf.LIMITS}
        except nf.LookupFailed as exc:
            return {"error": True, "code": exc.code, "message": exc.message, "retryable": exc.retryable}
        except ValueError as exc:
            return {"error": True, "code": "INVALID_PARAMETER", "message": str(exc)}
        data = dict(out["summary"])
        sid = None
        try:
            from ai_hydro.mcp.helpers import _resolve_session

            sid = _resolve_session(session_id, None, allow_auto_create=False, allow_recent_fallback=False,
                                   must_exist=bool(session_id))
        except Exception as exc:
            if session_id:
                raise
            log.debug("gauge_flowlines: no study resolved, nothing written (%s)", exc)
        text = json.dumps(out["geojson"], separators=(",", ":"))
        data["geojson_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        data["geometry_file"] = None
        if sid:
            from ai_hydro.mcp.helpers import _workspace_write
            from ai_hydro.mcp.enforcement import post_run

            fname = f"flowlines_{site}_{direction}_{distance_km}km_{data['geojson_sha256'][:8]}.geojson"
            path = _workspace_write(sid, fname, text)
            data["geometry_file"] = fname if path else None
            data["geometry_note"] = ("written to the study workspace" if path else
                                     "the study has no workspace directory, so the geometry was not written")
            result = {"data": data, "meta": {"tool": "gauge_flowlines", "source": data["source"],
                                             "params": {"gauge_id": site, "direction": direction, "distance_km": distance_km}}}
            return post_run("gauge_flowlines", sid, result,
                            inputs={"gauge_id": site, "direction": direction, "distance_km": distance_km})
        data["geometry_note"] = "no study resolved, so the geometry was not written; pass session_id"
        return {"data": data}
    except Exception as exc:
        log.error("gauge_flowlines failed: %s", exc)
        return _tool_error_to_dict(exc)
