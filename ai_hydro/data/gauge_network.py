"""Upstream and downstream USGS streamgages of one gauge, from NLDI and the Water Data API (US only).

Read-only over public services; nothing is stored in a study. The river network is not copied or
hosted: NLDI (NHDPlus V2) is asked, the answer is filtered and labelled, and the request URLs and
response digests are returned so the answer can be checked against the service.

Sources
-------
* NLDI navigation (``/nwissite/USGS-<id>/navigation/<UT|DM>/nwissite``): the NWIS sites on the flow
  path, which include lake, reservoir, spring and well-adjacent sites. The ``mainstem`` property is a
  Geoconnex PID (``https://geoconnex.us/ref/mainstems/<n>``).
* USGS Water Data API ``time-series-metadata``: keeps a site only if it has a daily-mean discharge
  series (parameter 00060, statistic 00003) and gives the series' begin and end. This is the
  "streamflow site" filter; a site number's shape is not used for it.
* USGS Water Data API ``monitoring-locations``: name, location and drainage area (as the API states
  it, square miles).
* Geoconnex reference gages: the PID of the starting gauge, through ``aihydro_watershed.identity``.

Limits that go into every answer: United States only (NLDI covers NHDPlus V2); ``distance_km`` is a
search radius along the network, not a count of gauges; ``UT`` includes every tributary above the
gauge, ``DM`` follows the main stem down; the drainage areas are the USGS station records, not
recomputed; a gauge the NLDI does not know is "not available", never guessed.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlencode, urlparse

log = logging.getLogger("ai_hydro.data.gauge_network")

NLDI_BASE = "https://api.water.usgs.gov/nldi/linked-data"
OGC_BASE = "https://api.waterdata.usgs.gov/ogcapi/v1"
ALLOWED_HOSTS = ("api.water.usgs.gov", "api.waterdata.usgs.gov", "reference.geoconnex.us")

MAX_DISTANCE_KM = 500
DEFAULT_DISTANCE_KM = 100
MAX_PER_DIRECTION = 50
_CHUNK = 40
SQMI_TO_KM2 = 2.589988110336

_SITE_RE = re.compile(r"^(?:USGS-)?(\d{8,15})$")

LIMITS = (
    "United States only (NLDI, NHDPlus V2). distance_km is a search radius along the river network, not a "
    "count. Upstream (UT) includes all tributaries above the gauge; downstream (DM) follows the main stem. "
    "Only sites with a daily-mean discharge series (00060/00003) are listed. Drainage areas are the USGS "
    "station values in square miles, not recomputed. A listed neighbour is a fact about the network; it is not "
    "evidence that its flows are independent of, or unregulated relative to, this gauge."
)


class NetworkLookupError(Exception):
    """A service could not be asked or answered unusably. ``retryable`` says whether trying later may help."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


class NotAvailable(Exception):
    """The question has no answer from these services (outside the US, or an unknown gauge)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def parse_gauge_id(gauge_id: str) -> str:
    """The bare site number of a USGS gauge id (``USGS-01013500`` or ``01013500``). Raises ``NotAvailable`` otherwise."""
    m = _SITE_RE.match(str(gauge_id or "").strip())
    if not m:
        raise NotAvailable(
            "US_ONLY",
            f"{gauge_id!r} is not a USGS site number. This tool covers United States gauges (NLDI/NHDPlus V2); "
            "there is no equivalent network lookup for other countries here.")
    return m.group(1)


def _default_get(url: str, params: Optional[Mapping[str, Any]]) -> Tuple[int, bytes]:
    import requests

    host = urlparse(url).hostname
    if urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:
        raise NetworkLookupError("HOST_NOT_ALLOWED", f"{url} is not an allowed host")
    last: Optional[Exception] = None
    for _attempt in (0, 1):                                    # one retry for a connection fault or a 5xx
        try:
            r = requests.get(url, params=params, headers={"User-Agent": "aihydro-tools/gauge_network",
                                                          "Accept": "application/json"},
                             timeout=30, allow_redirects=False)
            if r.status_code >= 500 and _attempt == 0:
                continue
            return r.status_code, r.content
        except requests.exceptions.RequestException as exc:
            last = exc
    raise NetworkLookupError("SERVICE_UNREACHABLE", f"cannot reach {host}: {type(last).__name__}", retryable=True)


class Client:
    """GET + JSON + provenance. ``get`` is injectable so tests never touch the network."""

    def __init__(self, get: Optional[Callable[[str, Optional[Mapping[str, Any]]], Tuple[int, bytes]]] = None):
        self._get = get or _default_get
        self.request_urls: List[str] = []
        self.response_sha256: List[str] = []
        self.retrieved_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def json(self, url: str, params: Optional[Mapping[str, Any]] = None, *, not_found_ok: bool = False) -> Optional[dict]:
        status, body = self._get(url, params)
        self.request_urls.append(url + ("?" + urlencode(params) if params else ""))
        self.response_sha256.append(hashlib.sha256(body).hexdigest())
        if status == 404 and not_found_ok:
            return None
        if status == 429:
            raise NetworkLookupError("RATE_LIMITED", f"HTTP 429 from {urlparse(url).hostname}", retryable=True)
        if status >= 500:
            raise NetworkLookupError("SERVICE_ERROR", f"HTTP {status} from {urlparse(url).hostname}", retryable=True)
        if status != 200:
            raise NetworkLookupError("HTTP_ERROR", f"HTTP {status} from {urlparse(url).path}")
        try:
            doc = json.loads(body)
        except ValueError as exc:
            raise NetworkLookupError("BAD_BODY", f"{urlparse(url).hostname} did not return JSON: {exc}") from exc
        return doc if isinstance(doc, dict) else {}


def nldi_navigate(client: Client, site: str, mode: str, distance_km: int) -> Optional[List[dict]]:
    doc = client.json(f"{NLDI_BASE}/nwissite/USGS-{site}/navigation/{mode}/nwissite",
                      {"distance": distance_km, "f": "json"}, not_found_ok=True)
    if doc is None:
        return None
    return [f for f in doc.get("features", []) if isinstance(f, dict) and isinstance(f.get("properties"), dict)]


def _chunks(ids: List[str]) -> List[List[str]]:
    return [ids[i:i + _CHUNK] for i in range(0, len(ids), _CHUNK)]


def daily_discharge_sites(client: Client, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """``{USGS-id: {"begin", "end"}}`` for the ids that have a daily-mean discharge series."""
    out: Dict[str, Dict[str, Any]] = {}
    for chunk in _chunks(sorted(set(ids))):
        doc = client.json(f"{OGC_BASE}/collections/time-series-metadata/items",
                          {"monitoring_location_id": ",".join(chunk), "parameter_code": "00060",
                           "statistic_id": "00003", "limit": 1000, "f": "json"}) or {}
        for f in doc.get("features", []):
            p = f.get("properties") or {}
            sid = p.get("monitoring_location_id")
            if not sid or p.get("parameter_code") != "00060" or p.get("statistic_id") != "00003":
                continue
            cur = out.setdefault(sid, {"begin": None, "end": None})
            b, e = p.get("begin"), p.get("end")
            cur["begin"] = b if cur["begin"] is None or (b and b < cur["begin"]) else cur["begin"]
            cur["end"] = e if cur["end"] is None or (e and e > cur["end"]) else cur["end"]
    return out


def locations(client: Client, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """``{USGS-id: monitoring-location properties}``."""
    out: Dict[str, Dict[str, Any]] = {}
    for chunk in _chunks(sorted(set(ids))):
        doc = client.json(f"{OGC_BASE}/collections/monitoring-locations/items",
                          {"id": ",".join(chunk), "limit": 1000, "f": "json"}) or {}
        for f in doc.get("features", []):
            p = f.get("properties") or {}
            if p.get("id"):
                out[p["id"]] = {**p, "_geometry": f.get("geometry")}
    return out


def _area(p: Mapping[str, Any]) -> Optional[float]:
    v = p.get("drainage_area")
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def _row(feature: dict, loc: Mapping[str, Any], series: Mapping[str, Any], origin_area: Optional[float]) -> dict:
    pr = feature["properties"]
    area = _area(loc)
    coords = (feature.get("geometry") or {}).get("coordinates") or (loc.get("_geometry") or {}).get("coordinates") or [None, None]
    return {
        "usgs_id": pr["identifier"],
        "name": loc.get("monitoring_location_name") or pr.get("name"),
        "lon": coords[0], "lat": coords[1],
        "comid": pr.get("comid"),
        "reachcode": pr.get("reachcode"),
        "mainstem_pid": pr.get("mainstem"),
        "drainage_area_sqmi": area,
        "drainage_area_km2": round(area * SQMI_TO_KM2, 1) if area else None,
        "drainage_area_ratio_to_gauge": round(area / origin_area, 3) if area and origin_area else None,
        "daily_discharge_record": {"begin": series.get("begin"), "end": series.get("end")},
        "uri": pr.get("uri"),
    }


def gauge_network(site: str, distance_km: int = DEFAULT_DISTANCE_KM, *, client: Optional[Client] = None,
                  geoconnex: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """Streamgages upstream and downstream of ``site`` within ``distance_km`` along the network.

    Raises ``NotAvailable`` (US-only / unknown gauge) or ``NetworkLookupError`` (service trouble).
    """
    if not isinstance(distance_km, int) or isinstance(distance_km, bool) or not 1 <= distance_km <= MAX_DISTANCE_KM:
        raise ValueError(f"distance_km must be an integer from 1 to {MAX_DISTANCE_KM}, got {distance_km!r}")
    c = client or Client()
    origin = f"USGS-{site}"

    up = nldi_navigate(c, site, "UT", distance_km)
    if up is None:
        raise NotAvailable(
            "GAUGE_NOT_IN_NLDI",
            f"NLDI has no NWIS site {origin}. NLDI covers United States gauges on NHDPlus V2; this id is either "
            "not in it or is outside the United States. Nothing was guessed.")
    down = nldi_navigate(c, site, "DM", distance_km) or []

    by_id: Dict[str, dict] = {}
    for f in up + down:
        ident = f["properties"].get("identifier")
        if isinstance(ident, str):
            by_id.setdefault(ident, f)
    ids = list(by_id)
    series = daily_discharge_sites(c, ids + ([origin] if origin not in by_id else []))
    locs = locations(c, [i for i in ids if i in series] + [origin])

    o_loc = locs.get(origin, {})
    o_area = _area(o_loc)
    o_feat = by_id.get(origin) or {"properties": {"identifier": origin}, "geometry": o_loc.get("_geometry")}

    def side(feats: List[dict]) -> Tuple[List[dict], int, int]:
        seen, rows, excluded = set(), [], 0
        for f in feats:
            ident = f["properties"].get("identifier")
            if ident in seen or ident == origin or not isinstance(ident, str):
                continue
            seen.add(ident)
            if ident not in series:
                excluded += 1
                continue
            rows.append(_row(f, locs.get(ident, {}), series[ident], o_area))
        rows.sort(key=lambda r: (r["drainage_area_sqmi"] is None, -(r["drainage_area_sqmi"] or 0.0), r["usgs_id"]))
        return rows[:MAX_PER_DIRECTION], excluded, max(0, len(rows) - MAX_PER_DIRECTION)

    u_rows, u_excl, u_cut = side(up)
    d_rows, d_excl, d_cut = side(down)

    gx = None
    if geoconnex is not None:
        try:
            lk = geoconnex(site)
            gx = {"status": getattr(lk, "status", None), "pid": getattr(lk, "uri", None)}
        except Exception as exc:                                   # a lookup that fails is reported, not fatal
            gx = {"status": "unresolved", "pid": None, "error": type(exc).__name__}

    return {
        "available": True,
        "gauge": {
            "usgs_id": origin, "name": o_loc.get("monitoring_location_name") or (o_feat["properties"].get("name")),
            "site_type": o_loc.get("site_type"), "comid": (o_feat["properties"] or {}).get("comid"),
            "mainstem_pid": (o_feat["properties"] or {}).get("mainstem"),
            "drainage_area_sqmi": o_area, "drainage_area_km2": round(o_area * SQMI_TO_KM2, 1) if o_area else None,
            "geoconnex_gage": gx,
            "daily_discharge_record": series.get(origin),
        },
        "search": {"distance_km": distance_km, "upstream_mode": "UT", "downstream_mode": "DM"},
        "upstream": {"n": len(u_rows), "gauges": u_rows, "excluded_without_daily_discharge": u_excl,
                     "omitted_beyond_limit": u_cut},
        "downstream": {"n": len(d_rows), "gauges": d_rows, "excluded_without_daily_discharge": d_excl,
                       "omitted_beyond_limit": d_cut},
        "sources": {
            "network": "NLDI navigation over NHDPlus V2 (api.water.usgs.gov/nldi)",
            "streamflow_filter": "USGS Water Data API time-series-metadata, parameter 00060, statistic 00003",
            "attributes": "USGS Water Data API monitoring-locations (drainage_area in square miles)",
            "retrieved_at": c.retrieved_at, "request_urls": c.request_urls, "response_sha256": c.response_sha256,
        },
        "limits": LIMITS,
    }
