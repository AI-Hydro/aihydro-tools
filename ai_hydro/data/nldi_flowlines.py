"""NHDPlus flowline geometry upstream and downstream of a USGS gauge, from NLDI (US only).

Public-service read, bounded: at most two requests (one per direction), a size cap on each response, a feature cap,
and an https host allow-list. The geometry is for drawing on a map; it is returned as a GeoJSON FeatureCollection and
is not copied into a model's context (the tool writes it to the workspace). Each feature carries its NHDPlus COMID, the
direction it was found in, and its length computed from the coordinates (haversine along the line, km).

NLDI's ``UT`` follows every tributary above the gauge; ``DM`` follows the main stem down. The ``distance`` is a search
radius along the network. Nothing about the flowlines says whether a reach is regulated, perennial or gauged.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlencode

NLDI_BASE = "https://api.water.usgs.gov/nldi/linked-data"
MAX_DISTANCE_KM = 200
DEFAULT_DISTANCE_KM = 50
MAX_FEATURES = 1500
MAX_RESPONSE_BYTES = 8_000_000
_SITE_RE = re.compile(r"^(?:USGS-)?([0-9]{8,15})$")
MODES = {"upstream": "UT", "downstream": "DM"}

LIMITS = ("United States only (NLDI, NHDPlus V2). Upstream (UT) includes all tributaries within distance_km along the network; "
          "downstream (DM) follows the main stem. Geometry is NHDPlus V2 flowlines as served; it is not a regulation, "
          "perenniality or gauge-coverage statement.")


class NotAvailable(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class LookupFailed(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


def parse_gauge_id(gauge_id: str) -> str:
    m = _SITE_RE.match(str(gauge_id or "").strip())
    if not m:
        raise NotAvailable("US_ONLY", f"{gauge_id!r} is not a USGS site number. This tool covers United States gauges (NLDI/NHDPlus V2).")
    return m.group(1)


def _default_get(url: str, params: Optional[Mapping[str, Any]]) -> Tuple[int, bytes]:
    import requests

    if not url.startswith(NLDI_BASE + "/"):
        raise LookupFailed("HOST_NOT_ALLOWED", f"{url} is not the NLDI host")
    try:
        r = requests.get(url, params=params, headers={"User-Agent": "aihydro-tools/flowlines", "Accept": "application/json"},
                         timeout=40, allow_redirects=False, stream=True)
        body = r.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
    except requests.exceptions.RequestException as exc:
        raise LookupFailed("SERVICE_UNREACHABLE", f"cannot reach NLDI: {type(exc).__name__}", retryable=True) from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise LookupFailed("RESPONSE_TOO_LARGE", f"NLDI answered with more than {MAX_RESPONSE_BYTES} bytes; lower distance_km")
    return r.status_code, body


def line_length_km(coords: List[List[float]]) -> float:
    total = 0.0
    for (x1, y1, *_), (x2, y2, *_) in zip(coords, coords[1:]):
        p1, p2 = math.radians(y1), math.radians(y2)
        h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(x2 - x1) / 2) ** 2
        total += 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))
    return total


def gauge_flowlines(site: str, direction: str = "both", distance_km: int = DEFAULT_DISTANCE_KM, *,
                    max_features: int = MAX_FEATURES,
                    get: Optional[Callable[[str, Optional[Mapping[str, Any]]], Tuple[int, bytes]]] = None) -> Dict[str, Any]:
    """``{"summary": {...}, "geojson": FeatureCollection}``. Raises ``NotAvailable`` / ``LookupFailed`` / ``ValueError``."""
    if direction not in ("upstream", "downstream", "both"):
        raise ValueError(f"direction must be 'upstream', 'downstream' or 'both', got {direction!r}")
    if not isinstance(distance_km, int) or isinstance(distance_km, bool) or not 1 <= distance_km <= MAX_DISTANCE_KM:
        raise ValueError(f"distance_km must be an integer from 1 to {MAX_DISTANCE_KM}, got {distance_km!r}")
    if not isinstance(max_features, int) or isinstance(max_features, bool) or not 1 <= max_features <= MAX_FEATURES:
        raise ValueError(f"max_features must be an integer from 1 to {MAX_FEATURES}, got {max_features!r}")
    fetch = get or _default_get
    wanted = ["upstream", "downstream"] if direction == "both" else [direction]
    urls: List[str] = []
    hashes: List[str] = []
    features: List[dict] = []
    per_dir: Dict[str, Dict[str, Any]] = {}
    seen: set = set()
    for d in wanted:
        url = f"{NLDI_BASE}/nwissite/USGS-{site}/navigation/{MODES[d]}/flowlines"
        params = {"distance": distance_km, "f": "json"}
        status, body = fetch(url, params)
        urls.append(url + "?" + urlencode(params))
        hashes.append(hashlib.sha256(body).hexdigest())
        if status == 404:
            raise NotAvailable("GAUGE_NOT_IN_NLDI", f"NLDI has no NWIS site USGS-{site}: not in NLDI or outside the United States. Nothing was guessed.")
        if status == 429 or status >= 500:
            raise LookupFailed("RATE_LIMITED" if status == 429 else "SERVICE_ERROR", f"NLDI answered HTTP {status}", retryable=True)
        if status != 200:
            raise LookupFailed("HTTP_ERROR", f"NLDI answered HTTP {status}")
        try:
            doc = json.loads(body)
        except ValueError as exc:
            raise LookupFailed("BAD_BODY", f"NLDI did not return JSON: {exc}") from exc
        n, length = 0, 0.0
        for f in doc.get("features", []) if isinstance(doc, dict) else []:
            geom = f.get("geometry") if isinstance(f, dict) else None
            if not isinstance(geom, dict) or geom.get("type") != "LineString" or len(geom.get("coordinates") or []) < 2:
                continue
            comid = (f.get("properties") or {}).get("nhdplus_comid")
            key = (d, comid)
            if comid is not None and key in seen:
                continue
            seen.add(key)
            km = line_length_km(geom["coordinates"])
            n += 1
            length += km
            features.append({"type": "Feature", "geometry": geom,
                             "properties": {"nhdplus_comid": comid, "direction": d, "length_km": round(km, 3)}})
        per_dir[d] = {"n_flowlines": n, "length_km": round(length, 1)}
    total = len(features)
    truncated = total > max_features
    if truncated:
        features = features[:max_features]
        kept = {d: [f for f in features if f["properties"]["direction"] == d] for d in per_dir}
        for d in per_dir:
            per_dir[d]["n_flowlines_returned"] = len(kept[d])
            per_dir[d]["length_km_returned"] = round(sum(f["properties"]["length_km"] for f in kept[d]), 1)
    xs = [c[0] for f in features for c in f["geometry"]["coordinates"]]
    ys = [c[1] for f in features for c in f["geometry"]["coordinates"]]
    bbox = [min(xs), min(ys), max(xs), max(ys)] if xs else None
    summary = {
        "available": True, "gauge": f"USGS-{site}", "direction": direction, "distance_km": distance_km,
        "directions": per_dir, "n_features_total": total, "n_features_returned": len(features),
        "truncated": truncated, "max_features": max_features,
        "bbox": [round(v, 5) for v in bbox] if bbox else None, "bbox_order": "west,south,east,north (EPSG:4326)",
        "source": "NLDI navigation over NHDPlus V2 (api.water.usgs.gov/nldi)",
        "retrieved_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "request_urls": "\n".join(urls), "response_sha256": " ".join(hashes), "limits": LIMITS,
    }
    return {"summary": summary, "geojson": {"type": "FeatureCollection", "features": features}}
