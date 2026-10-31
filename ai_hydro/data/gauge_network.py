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
import math
import os
import re
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlencode, urlparse

log = logging.getLogger("ai_hydro.data.gauge_network")

NLDI_BASE = "https://api.water.usgs.gov/nldi/linked-data"
OGC_BASE = "https://api.waterdata.usgs.gov/ogcapi/v1"
ALLOWED_HOSTS = ("api.water.usgs.gov", "api.waterdata.usgs.gov", "reference.geoconnex.us")

MAX_DISTANCE_KM = 500
DEFAULT_DISTANCE_KM = 100
MAX_PER_DIRECTION = 20                  # what is shown is what is sealed, so every listed gauge is citable
MAX_CANDIDATES = 80                     # NLDI sites checked for a discharge series, nearest first
MAX_REQUESTS = 12                       # a whole call, Geoconnex excluded
DEADLINE_S = 45.0
_CHUNK = 40
SQMI_TO_KM2 = 2.589988110336

_SITE_RE = re.compile(r"^(?:USGS-)?([0-9]{8,15})$")

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


def _default_get(url: str, params: Optional[Mapping[str, Any]], timeout: float = 30.0) -> Tuple[int, bytes]:
    import requests

    host = urlparse(url).hostname
    if urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:
        raise NetworkLookupError("HOST_NOT_ALLOWED", f"{url} is not an allowed host")
    headers = {"User-Agent": "aihydro-tools/gauge_network", "Accept": "application/json"}
    key = os.environ.get("API_USGS_PAT")
    if key and host == "api.waterdata.usgs.gov":             # the key goes to that host only
        headers["X-Api-Key"] = key
    last: Optional[Exception] = None
    for _attempt in (0, 1):                                    # one retry for a connection fault or a 5xx
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout, allow_redirects=False)
            if r.status_code >= 500 and _attempt == 0:
                continue
            return r.status_code, r.content
        except requests.exceptions.RequestException as exc:
            last = exc
    raise NetworkLookupError("SERVICE_UNREACHABLE", f"cannot reach {host}: {type(last).__name__}", retryable=True)


class Client:
    """GET + JSON + provenance, under a request budget and a wall-clock deadline.

    ``get`` is injectable so tests never touch the network. When the budget or the deadline is
    spent, ``json`` raises ``NetworkLookupError`` (``BUDGET_EXHAUSTED`` / ``DEADLINE_EXCEEDED``) and the
    caller returns what it already has, marked incomplete.
    """

    def __init__(self, get: Optional[Callable[..., Tuple[int, bytes]]] = None, *,
                 max_requests: int = MAX_REQUESTS, deadline_s: float = DEADLINE_S):
        self._get = get or _default_get
        self.max_requests, self.deadline_s = max_requests, deadline_s
        self._t0 = time.monotonic()
        self.request_urls: List[str] = []
        self.response_sha256: List[str] = []
        self.retrieved_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def json(self, url: str, params: Optional[Mapping[str, Any]] = None, *, not_found_ok: bool = False) -> Optional[dict]:
        if len(self.request_urls) >= self.max_requests:
            raise NetworkLookupError("BUDGET_EXHAUSTED", f"request budget of {self.max_requests} spent", retryable=True)
        if time.monotonic() - self._t0 > self.deadline_s:
            raise NetworkLookupError("DEADLINE_EXCEEDED", f"{self.deadline_s:.0f} s deadline passed", retryable=True)
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


class Progress:
    """What a call could not finish, and why. A call that stops early still returns what it has."""

    def __init__(self) -> None:
        self.reasons: List[str] = []
        self.unchecked: int = 0

    @property
    def incomplete(self) -> bool:
        return bool(self.reasons)

    def stop(self, exc: "NetworkLookupError", what: str) -> None:
        self.reasons.append(f"{what}: {exc.code} ({exc.message})")


def daily_discharge_sites(client: Client, ids: List[str], progress: Optional[Progress] = None) -> Dict[str, Dict[str, Any]]:
    """``{USGS-id: {"begin", "end"}}`` for the ids that have a daily-mean discharge series.

    If a request fails part-way, the sites confirmed so far are returned and ``progress`` says how many
    candidates were not checked."""
    out: Dict[str, Dict[str, Any]] = {}
    chunks = _chunks(sorted(set(ids)))
    for n, chunk in enumerate(chunks):
        try:
            doc = client.json(f"{OGC_BASE}/collections/time-series-metadata/items",
                              {"monitoring_location_id": ",".join(chunk), "parameter_code": "00060",
                               "statistic_id": "00003", "limit": 1000, "f": "json"}) or {}
        except NetworkLookupError as exc:
            if progress is None:
                raise
            progress.stop(exc, "discharge-series check")
            progress.unchecked += sum(len(c) for c in chunks[n:])
            break
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


def locations(client: Client, ids: List[str], progress: Optional[Progress] = None) -> Dict[str, Dict[str, Any]]:
    """``{USGS-id: monitoring-location properties}``; a failed request leaves the rest without attributes."""
    out: Dict[str, Dict[str, Any]] = {}
    for chunk in _chunks(sorted(set(ids))):
        try:
            doc = client.json(f"{OGC_BASE}/collections/monitoring-locations/items",
                              {"id": ",".join(chunk), "limit": 1000, "f": "json"}) or {}
        except NetworkLookupError as exc:
            if progress is None:
                raise
            progress.stop(exc, "site attributes")
            break
        for f in doc.get("features", []):
            p = f.get("properties") or {}
            if p.get("id"):
                out[p["id"]] = {**p, "_geometry": f.get("geometry")}
    return out


def _area(p: Mapping[str, Any]) -> Optional[float]:
    v = p.get("drainage_area")
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def _lonlat(feature: dict, loc: Mapping[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    c = (feature.get("geometry") or {}).get("coordinates") or (loc.get("_geometry") or {}).get("coordinates") or [None, None]
    return c[0], c[1]


def _km(a: Tuple[Optional[float], Optional[float]], b: Tuple[Optional[float], Optional[float]]) -> float:
    """Straight-line distance, km (haversine); infinity when a position is missing."""
    if None in a or None in b:
        return math.inf
    (x1, y1), (x2, y2) = a, b
    p1, p2 = math.radians(y1), math.radians(y2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(x2 - x1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def _row(feature: dict, loc: Mapping[str, Any], series: Mapping[str, Any], origin_area: Optional[float]) -> dict:
    pr = feature["properties"]
    area = _area(loc)
    lon, lat = _lonlat(feature, loc)
    return {
        "usgs_id": pr["identifier"],
        "name": loc.get("monitoring_location_name") or pr.get("name"),
        "lon": lon, "lat": lat,
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

    Raises ``NotAvailable`` (US-only / unknown gauge). A request budget (``MAX_REQUESTS``), a deadline
    (``DEADLINE_S``), a 429 or a service fault do not raise: the result carries what was gathered with
    ``incomplete: true`` and the reasons. Candidates are checked nearest first (straight-line), at most
    ``MAX_CANDIDATES`` of them.
    """
    if not isinstance(distance_km, int) or isinstance(distance_km, bool) or not 1 <= distance_km <= MAX_DISTANCE_KM:
        raise ValueError(f"distance_km must be an integer from 1 to {MAX_DISTANCE_KM}, got {distance_km!r}")
    c = client or Client()
    origin = f"USGS-{site}"
    prog = Progress()

    up: List[dict] = []
    down: List[dict] = []
    try:
        got = nldi_navigate(c, site, "UT", distance_km)
        if got is None:
            raise NotAvailable(
                "GAUGE_NOT_IN_NLDI",
                f"NLDI has no NWIS site {origin}. NLDI covers United States gauges on NHDPlus V2; this id is either "
                "not in it or is outside the United States. Nothing was guessed.")
        up = got
        down = nldi_navigate(c, site, "DM", distance_km) or []
    except NetworkLookupError as exc:
        prog.stop(exc, "network navigation")

    by_id: Dict[str, dict] = {}
    for f in up + down:
        ident = f["properties"].get("identifier")
        if isinstance(ident, str):
            by_id.setdefault(ident, f)
    o_feat = by_id.get(origin)
    here = _lonlat(o_feat, {}) if o_feat else (None, None)
    cand = sorted((i for i in by_id if i != origin), key=lambda i: (_km(here, _lonlat(by_id[i], {})), i))
    n_cand_total = len(cand)
    cand = cand[:MAX_CANDIDATES]

    series = daily_discharge_sites(c, cand + [origin], prog) if (cand or not prog.incomplete) else {}
    keep = [i for i in cand if i in series]
    locs = locations(c, keep + [origin], prog) if series else {}

    o_loc = locs.get(origin, {})
    o_area = _area(o_loc)
    o_props = (o_feat or {}).get("properties") or {}

    def side(feats: List[dict]) -> Tuple[Dict[str, dict], int, int]:
        seen, rows, excluded = set(), [], 0
        for f in feats:
            ident = f["properties"].get("identifier")
            if ident in seen or ident == origin or ident not in cand:
                continue
            seen.add(ident)
            if ident not in series:
                excluded += 1
                continue
            rows.append(_row(f, locs.get(ident, {}), series[ident], o_area))
        rows.sort(key=lambda r: (_km(here, (r["lon"], r["lat"])), r["usgs_id"]))
        return {r["usgs_id"]: r for r in rows[:MAX_PER_DIRECTION]}, excluded, max(0, len(rows) - MAX_PER_DIRECTION)

    u_rows, u_excl, u_cut = side(up)
    d_rows, d_excl, d_cut = side(down)
    if prog.incomplete and prog.unchecked:
        u_excl = d_excl = None                           # not knowable: some candidates were never checked

    gx: Dict[str, Any] = {"status": "unavailable", "pid": None}
    gx_url = None
    if geoconnex is not None:
        gx_url = f"https://reference.geoconnex.us/collections/gages/items?provider_id={origin}&f=json"
        try:
            lk = geoconnex(site)
            gx = {"status": getattr(lk, "status", None), "pid": getattr(lk, "uri", None)}
        except Exception as exc:                                   # a lookup that fails is reported, not fatal
            gx = {"status": "unresolved", "pid": None, "error": type(exc).__name__}

    urls = list(c.request_urls) + ([gx_url + "  (cached answer used when status is 'cached')"] if gx_url else [])
    return {
        "available": True,
        "incomplete": prog.incomplete,
        "incomplete_reasons": " | ".join(prog.reasons),
        "candidates_nldi_total": n_cand_total,
        "candidates_checked": len(cand) - prog.unchecked,
        "candidates_not_checked": prog.unchecked + max(0, n_cand_total - MAX_CANDIDATES),
        "candidate_order": "straight-line distance from the gauge, nearest first",
        "gauge": {
            "usgs_id": origin, "name": o_loc.get("monitoring_location_name") or o_props.get("name"),
            "site_type": o_loc.get("site_type"), "comid": o_props.get("comid"),
            "mainstem_pid": o_props.get("mainstem"),
            "drainage_area_sqmi": o_area, "drainage_area_km2": round(o_area * SQMI_TO_KM2, 1) if o_area else None,
            "geoconnex_gage_status": gx.get("status"), "geoconnex_gage_pid": gx.get("pid"),
            "daily_discharge_begin": (series.get(origin) or {}).get("begin"),
            "daily_discharge_end": (series.get(origin) or {}).get("end"),
        },
        "search_distance_km": distance_km,
        "search_upstream_mode": "UT",
        "search_downstream_mode": "DM",
        "upstream_n": len(u_rows),
        "upstream_gauges": u_rows,
        "upstream_excluded_without_daily_discharge": u_excl,
        "upstream_omitted_beyond_limit": u_cut,
        "downstream_n": len(d_rows),
        "downstream_gauges": d_rows,
        "downstream_excluded_without_daily_discharge": d_excl,
        "downstream_omitted_beyond_limit": d_cut,
        "source_network": "NLDI navigation over NHDPlus V2 (api.water.usgs.gov/nldi)",
        "source_streamflow_filter": "USGS Water Data API time-series-metadata, parameter 00060, statistic 00003",
        "source_attributes": "USGS Water Data API monitoring-locations (drainage_area in square miles)",
        "retrieved_at": c.retrieved_at,
        "request_urls": "\n".join(urls),
        "response_sha256": " ".join(c.response_sha256),
        "limits": LIMITS,
    }
