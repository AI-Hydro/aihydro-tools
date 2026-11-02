"""NHDPlus flowline geometry upstream and downstream of a USGS gauge, from NLDI (US only).

Built on ``gauge_network``'s pieces: its gauge-id parser, its errors (``NotAvailable``, ``NetworkLookupError``), and its ``Client`` (request
budget, one wall-clock deadline for the whole call, per-request timeout bounded by what is left of it, response digests), asked for a body
cap. At most two requests (one per direction). The geometry is for drawing on a map; it is returned as a GeoJSON FeatureCollection and
is not copied into a model's context (the tool writes it to the workspace). Each feature carries its NHDPlus COMID, the direction it was
found in, and its length computed from the coordinates (haversine along the line, km).

NLDI's ``UT`` follows every tributary above the gauge; ``DM`` follows the main stem. The ``distance`` is a search radius along the
network. The feature cap applies **per direction**, so a large tributary network cannot push the downstream main stem out. Nothing about
the flowlines says whether a reach is regulated, perennial or gauged.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
from typing import Any, Callable, Dict, List, Optional

from ai_hydro.data.gauge_network import (  # noqa: F401  (re-exported: one set of errors and one parser for both tools)
    NLDI_BASE, Client, NetworkLookupError, NotAvailable, parse_gauge_id,
)

MAX_DISTANCE_KM = 200
DEFAULT_DISTANCE_KM = 50
MAX_FEATURES_PER_DIRECTION = 750
MAX_RESPONSE_BYTES = 8_000_000
DEADLINE_S = 45.0
MODES = {"upstream": "UT", "downstream": "DM"}

LIMITS = ("United States only (NLDI, NHDPlus V2). Upstream (UT) includes all tributaries within distance_km along the network; "
          "downstream (DM) follows the main stem. Geometry is NHDPlus V2 flowlines as served; it is not a regulation, "
          "perenniality or gauge-coverage statement. The feature cap is per direction.")

LookupFailed = NetworkLookupError            # the name this module used before it shared gauge_network's errors


def line_length_km(coords: List[List[float]]) -> float:
    total = 0.0
    for (x1, y1, *_), (x2, y2, *_) in zip(coords, coords[1:]):
        p1, p2 = math.radians(y1), math.radians(y2)
        h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(x2 - x1) / 2) ** 2
        total += 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))
    return total


def gauge_flowlines(site: str, direction: str = "both", distance_km: int = DEFAULT_DISTANCE_KM, *,
                    max_features: int = MAX_FEATURES_PER_DIRECTION, client: Optional[Client] = None) -> Dict[str, Any]:
    """``{"summary": {...}, "geojson": FeatureCollection}``. ``max_features`` is per direction.

    Raises ``NotAvailable`` / ``NetworkLookupError`` / ``ValueError``."""
    if direction not in ("upstream", "downstream", "both"):
        raise ValueError(f"direction must be 'upstream', 'downstream' or 'both', got {direction!r}")
    if not isinstance(distance_km, int) or isinstance(distance_km, bool) or not 1 <= distance_km <= MAX_DISTANCE_KM:
        raise ValueError(f"distance_km must be an integer from 1 to {MAX_DISTANCE_KM}, got {distance_km!r}")
    if not isinstance(max_features, int) or isinstance(max_features, bool) or not 1 <= max_features <= MAX_FEATURES_PER_DIRECTION:
        raise ValueError(f"max_features must be an integer from 1 to {MAX_FEATURES_PER_DIRECTION}, got {max_features!r}")
    c = client or Client(max_requests=2, deadline_s=DEADLINE_S, bytes_cap=MAX_RESPONSE_BYTES)
    wanted = ["upstream", "downstream"] if direction == "both" else [direction]
    features: List[dict] = []
    per_dir: Dict[str, Dict[str, Any]] = {}
    for d in wanted:
        doc = c.json(f"{NLDI_BASE}/nwissite/USGS-{site}/navigation/{MODES[d]}/flowlines",
                     {"distance": distance_km, "f": "json"}, not_found_ok=True)
        if doc is None:
            raise NotAvailable("GAUGE_NOT_IN_NLDI",
                               f"NLDI has no NWIS site USGS-{site}: not in NLDI or outside the United States. Nothing was guessed.")
        seen: set = set()
        rows: List[dict] = []
        for f in doc.get("features", []):
            geom = f.get("geometry") if isinstance(f, dict) else None
            if not isinstance(geom, dict) or geom.get("type") != "LineString" or len(geom.get("coordinates") or []) < 2:
                continue
            comid = (f.get("properties") or {}).get("nhdplus_comid")
            if comid is not None:
                if comid in seen:
                    continue
                seen.add(comid)
            km = line_length_km(geom["coordinates"])
            rows.append({"type": "Feature", "geometry": geom,
                         "properties": {"nhdplus_comid": comid, "direction": d, "length_km": round(km, 3)}})
        kept = rows[:max_features]
        per_dir[d] = {"n_flowlines": len(rows), "length_km": round(sum(r["properties"]["length_km"] for r in rows), 1),
                      "n_flowlines_returned": len(kept), "length_km_returned": round(sum(r["properties"]["length_km"] for r in kept), 1),
                      "truncated": len(rows) > len(kept)}
        features.extend(kept)
    xs = [pt[0] for f in features for pt in f["geometry"]["coordinates"]]
    ys = [pt[1] for f in features for pt in f["geometry"]["coordinates"]]
    bbox = [min(xs), min(ys), max(xs), max(ys)] if xs else None
    summary = {
        "available": True, "gauge": f"USGS-{site}", "direction": direction, "distance_km": distance_km,
        "directions": per_dir,
        "n_features_total": sum(v["n_flowlines"] for v in per_dir.values()), "n_features_returned": len(features),
        "truncated": any(v["truncated"] for v in per_dir.values()), "max_features_per_direction": max_features,
        "bbox": [round(v, 5) for v in bbox] if bbox else None, "bbox_order": "west,south,east,north (EPSG:4326)",
        "source": "NLDI navigation over NHDPlus V2 (api.water.usgs.gov/nldi)",
        "retrieved_at": c.retrieved_at,
        "request_urls": "\n".join(c.request_urls), "response_sha256": " ".join(c.response_sha256), "limits": LIMITS,
    }
    return {"summary": summary, "geojson": {"type": "FeatureCollection", "features": features}}
