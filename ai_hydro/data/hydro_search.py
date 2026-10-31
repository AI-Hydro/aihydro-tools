"""
USGS gauge / NID search and gauge layers for the AI-Hydro map panel.

Gauges come from the USGS Water Data API monitoring-locations collection through the
aihydro-data adapter (``aihydro_data.sources.usgs_waterdata.get_monitoring_locations``):
by site id, by bounding box, or by EXACT monitoring-location name. The API's partial
(substring) name match is unverified, so it is not used: a text query is matched
client-side against the gauges inside the map bbox (id or name substring), and a
CONUS-wide search without a bbox matches only an exact monitoring-location name.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

from ai_hydro.data.wbd_layers import _huc_code_from_row, is_conus_bbox, is_conus_point

log = logging.getLogger(__name__)

# HUC2 regions covering CONUS (used when the map view spans most of the country).
_CONUS_HUC2_CODES = tuple(f"{i:02d}" for i in range(1, 19))
_NID_FILTER_BATCH = 10
_MAX_HUC_CODES_FOR_WBD = 60

_MAX_SEARCH_HITS = 20
_MAX_GAUGES_IN_VIEW = 250
_SITE_ID_RE = re.compile(r"^\d{5,15}$")


def _problem(exc: BaseException) -> dict[str, Any]:
    """A USGS lookup failure as data: ``terminal`` True for a final refusal, False for "could not
    look" (rate limit, timeout, outage). Lets a caller say "no gauges" apart from "could not ask"."""
    try:
        from aihydro_data.exceptions import TerminalSourceError
        terminal = isinstance(exc, TerminalSourceError)
    except ImportError:
        terminal = False
    return {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)[:300], "terminal": terminal}


def _site_no(loc: Any) -> str:
    sid = str(loc.site_id or "")
    return sid[5:] if sid.upper().startswith("USGS-") else sid


def _site_to_hit(loc: Any, source: str = "gauge") -> dict[str, Any]:
    """Map-search hit for a ``MonitoringLocation`` (EPSG:4326 point, [lon, lat])."""
    site_no = _site_no(loc)
    name = str(loc.name or site_no)
    label = f"{site_no} — {name}"
    meta: dict[str, str] = {"gauge_id": site_no}
    if loc.drainage_area_raw is not None:
        # The API does not state the unit; USGS documents square miles and the adapter
        # cross-checked one gauge against the legacy site record (870 = 870).
        meta["drainage_area_sqmi"] = str(loc.drainage_area_raw)
    return {"label": label, "lat": float(loc.lat), "lon": float(loc.lon), "source": source, "meta": meta}


_MAX_BBOX_DEG2 = 25.0     # the former NWIS site service refused larger boxes; keep the paging bounded


def _bbox_within_service_limit(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> bool:
    return (max_lon - min_lon) * (max_lat - min_lat) <= _MAX_BBOX_DEG2


def _dam_display_name(row: Any) -> str:
    dam_id = str(row.get("id", row.get("nid_id", "")))
    return str(row.get("dam_name", row.get("name", dam_id)))


def _dam_to_hit(row: Any) -> dict[str, Any]:
    if hasattr(row, "geometry") and row.geometry is not None:
        lat = float(row.geometry.y)
        lon = float(row.geometry.x)
    else:
        lat = float(row.get("latitude", row.get("lat", 0)))
        lon = float(row.get("longitude", row.get("lon", 0)))
    dam_id = str(row.get("id", row.get("nid_id", "")))
    name = _dam_display_name(row)
    label = f"{dam_id} — {name}" if dam_id else name
    meta: dict[str, str] = {}
    if dam_id:
        meta["dam_id"] = dam_id
    if hasattr(row, "max_storage") and row.max_storage is not None:
        meta["max_storage_acft"] = str(row.max_storage)
    return {"label": label, "lat": lat, "lon": lon, "source": "dam", "meta": meta}


def _bbox_spans(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float
) -> tuple[float, float]:
    return max_lon - min_lon, max_lat - min_lat


def _is_mostly_conus_view(span_lon: float, span_lat: float) -> bool:
    return span_lon >= 14.0 and span_lat >= 10.0


def _nid_huc_level_for_view(span_lon: float, span_lat: float) -> int:
    if _is_mostly_conus_view(span_lon, span_lat):
        return 2
    if span_lon >= 3.5 or span_lat >= 3.0:
        return 6
    return 8


def _huc_codes_for_bbox(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    *,
    huc_level: int,
) -> list[str]:
    if huc_level == 2 and _is_mostly_conus_view(
        *_bbox_spans(min_lon, min_lat, max_lon, max_lat)
    ):
        return list(_CONUS_HUC2_CODES)

    from pygeohydro import WBD

    layer_key = f"huc{huc_level}"
    gdf = WBD(layer_key).bygeom((min_lon, min_lat, max_lon, max_lat), geo_crs=4326)
    codes: list[str] = []
    for _, row in gdf.iterrows():
        code = _huc_code_from_row(row, huc_level)
        if not code:
            continue
        digits = re.sub(r"\D", "", code)
        norm = digits[:huc_level]
        if norm and norm not in codes:
            codes.append(norm)
    return codes


def _query_nid_by_huc_codes(
    nid: Any,
    codes: list[str],
    huc_level: int,
) -> gpd.GeoDataFrame:
    if not codes:
        return gpd.GeoDataFrame()

    field = f"huc{huc_level}"
    batch_size = 4 if huc_level == 2 else _NID_FILTER_BATCH
    frames: list[gpd.GeoDataFrame] = []
    for i in range(0, len(codes), batch_size):
        chunk = codes[i : i + batch_size]
        try:
            batch = nid.get_byfilter([{field: chunk}])
            if batch:
                frames.append(batch[0])
        except Exception as e:
            log.warning("NID %s batch query failed (%s): %s", field, chunk[:3], e)

    if not frames:
        return gpd.GeoDataFrame()

    merged = pd.concat(frames, ignore_index=True)
    if "id" in merged.columns:
        merged = merged.drop_duplicates(subset=["id"])
    return gpd.GeoDataFrame(merged, geometry=merged.geometry, crs=4326)


def fetch_nid_dams_in_bbox(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
) -> gpd.GeoDataFrame:
    """
    Query NID dams in a map bbox via the REST API (get_byfilter), not get_bygeom.

    get_bygeom downloads the full national GeoPackage, which often fails on schema drift.
    """
    from pygeohydro import NID

    span_lon, span_lat = _bbox_spans(min_lon, min_lat, max_lon, max_lat)
    huc_level = _nid_huc_level_for_view(span_lon, span_lat)
    codes = _huc_codes_for_bbox(min_lon, min_lat, max_lon, max_lat, huc_level=huc_level)

    if len(codes) > _MAX_HUC_CODES_FOR_WBD and huc_level > 2:
        huc_level = 6 if huc_level == 8 else 2
        codes = _huc_codes_for_bbox(min_lon, min_lat, max_lon, max_lat, huc_level=huc_level)

    nid = NID()
    dams = _query_nid_by_huc_codes(nid, codes, huc_level)
    if dams.empty:
        return dams

    view = box(min_lon, min_lat, max_lon, max_lat)
    try:
        dams = dams[dams.geometry.intersects(view)].copy()
    except Exception:
        pass
    return dams


def search_hydrology(
    q: str,
    *,
    min_lon: float | None = None,
    min_lat: float | None = None,
    max_lon: float | None = None,
    max_lat: float | None = None,
    limit: int = _MAX_SEARCH_HITS,
    problems: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Search USGS gauges and NID dams. Returns hit dicts for the map SearchBar.

    ``problems`` (optional list) receives one dict per USGS lookup that failed or was skipped
    (``code``, ``message``, ``terminal``), so an empty result can be told from an outage.
    """
    query = (q or "").strip()
    if len(query) < 2:
        return []

    hits: list[dict[str, Any]] = []
    limit = max(1, min(limit, _MAX_SEARCH_HITS))

    try:
        from aihydro_data.sources import usgs_waterdata as uw
    except ImportError as e:
        raise ImportError("aihydro-data is required for hydrology search") from e

    def _locations(**kw: Any) -> list:
        try:
            return list(uw.get_monitoring_locations(**kw))
        except Exception as e:     # map search degrades to "no hits"; the reason is reported, not hidden
            log.warning("USGS monitoring-location search %s failed: [%s] %s",
                        sorted(kw), getattr(e, "code", type(e).__name__), e)
            if problems is not None:
                problems.append(_problem(e))
            return []

    # Site ID lookup (USGS 5–15 digit ids; the API wants 8-15, shorter ids simply find nothing)
    if _SITE_ID_RE.match(query):
        for loc in _locations(ids=[query.zfill(8)])[:limit]:
            hits.append(_site_to_hit(loc))

    # Bbox search (gauges + dams in view)
    if (
        len(hits) < limit
        and min_lon is not None
        and min_lat is not None
        and max_lon is not None
        and max_lat is not None
        and is_conus_bbox(min_lon, min_lat, max_lon, max_lat)
    ):
        q_lower = query.lower()
        if not _bbox_within_service_limit(min_lon, min_lat, max_lon, max_lat) and problems is not None:
            problems.append({"code": "BBOX_TOO_LARGE", "terminal": False,
                             "message": f"map view larger than {_MAX_BBOX_DEG2:g} deg2: gauges not queried"})
        if _bbox_within_service_limit(min_lon, min_lat, max_lon, max_lat):
            for loc in _locations(bbox=[min_lon, min_lat, max_lon, max_lat], site_type_code="ST"):
                if len(hits) >= limit:
                    break
                site_no = _site_no(loc)
                name = str(loc.name or "")
                if _SITE_ID_RE.match(query) and site_no != query:
                    continue
                if len(query) >= 2 and q_lower not in site_no.lower() and q_lower not in name.lower():
                    continue
                if not any(h.get("meta", {}).get("gauge_id") == site_no for h in hits):
                    hits.append(_site_to_hit(loc))

        try:
            dams = fetch_nid_dams_in_bbox(min_lon, min_lat, max_lon, max_lat)
            for _, row in dams.iterrows():
                if len(hits) >= limit:
                    break
                dam_id = str(row.get("id", ""))
                name = _dam_display_name(row)
                if len(query) >= 2 and q_lower not in dam_id.lower() and q_lower not in name.lower():
                    continue
                hit = _dam_to_hit(row)
                if not any(h.get("meta", {}).get("dam_id") == dam_id for h in hits if dam_id):
                    hits.append(hit)
        except Exception as e:
            log.debug("NID bbox search failed: %s", e)

    # Name search (CONUS-wide, no bbox required): EXACT monitoring-location name only.
    if len(hits) < limit and len(query) >= 3:
        for loc in _locations(name=query)[: limit - len(hits)]:
            site_no = _site_no(loc)
            if any(h.get("meta", {}).get("gauge_id") == site_no for h in hits):
                continue
            hits.append(_site_to_hit(loc))

    return hits[:limit]


SQMI_TO_KM2 = 2.589988110336
_DISCHARGE_CHUNK = 40


def stamp_query(layers: list[dict[str, Any]], bbox: tuple[float, float, float, float], *,
                request_urls: list[str] | None = None) -> list[dict[str, Any]]:
    """Record, on each reference layer, when it was retrieved and for which view (in place; returns ``layers``).

    A reference layer is a snapshot of a public service for one map view; without these two facts it cannot be told
    from a stale one or from a different extent. Metadata values are strings."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    west, south, east, north = (float(v) for v in bbox)
    for layer in layers:
        md = layer.setdefault("metadata", {})
        md["retrieved_at"] = now
        md["query_bbox"] = f"{west:.6f},{south:.6f},{east:.6f},{north:.6f}"
        md["query_bbox_order"] = "west,south,east,north (EPSG:4326)"
        if request_urls:
            md["request_urls"] = "\n".join(request_urls)
    return layers


def daily_discharge_records(site_ids: list[str]) -> dict[str, dict[str, Any]]:
    """``{USGS-id: {"begin", "end"}}`` for the sites with a daily-mean discharge series (00060/00003).

    One request per 40 sites. Any failure returns what was gathered so far; the caller then leaves the record
    unstated for the rest, never "none"."""
    try:
        from aihydro_data.sources import usgs_waterdata as uw
    except ImportError:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(site_ids), _DISCHARGE_CHUNK):
        chunk = site_ids[i:i + _DISCHARGE_CHUNK]
        try:
            rec = uw._new_record()
            feats = uw._paged_features(f"{uw.BASE}/collections/time-series-metadata/items",
                                       {"monitoring_location_id": ",".join(chunk), "parameter_code": "00060",
                                        "statistic_id": "00003", "limit": 1000, "f": "json"}, rec)
        except Exception as e:                      # noqa: BLE001 - a missing record must not hide the gauges
            log.warning("gauges_in_view record query failed: %s", e)
            return out
        for f in feats:
            p = f.get("properties") or {}
            sid = p.get("monitoring_location_id")
            if not sid or p.get("parameter_code") != "00060" or p.get("statistic_id") != "00003":
                continue
            cur = out.setdefault(sid, {"begin": None, "end": None})
            b, e = p.get("begin"), p.get("end")
            cur["begin"] = b if cur["begin"] is None or (b and b < cur["begin"]) else cur["begin"]
            cur["end"] = e if cur["end"] is None or (e and e > cur["end"]) else cur["end"]
        for sid in chunk:
            out.setdefault(sid, None)               # asked and answered: no daily discharge series
    return {k: v for k, v in out.items()}


def gauges_in_view_layer(
    *,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    lat: float,
    lon: float,
    limit: int = _MAX_GAUGES_IN_VIEW,
    problems: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a single point layer spec with NWIS sites in the map bbox.

    ``problems`` (optional list) receives the reason when no layer could be built because the
    lookup failed or the view was too large, as opposed to the view holding no gauges."""
    if not is_conus_point(lat, lon) or not is_conus_bbox(min_lon, min_lat, max_lon, max_lat):
        return []

    if not _bbox_within_service_limit(min_lon, min_lat, max_lon, max_lat):
        if problems is not None:
            problems.append({"code": "BBOX_TOO_LARGE", "terminal": False,
                             "message": f"map view larger than {_MAX_BBOX_DEG2:g} deg2: gauges not queried"})
        return []
    try:
        from aihydro_data.sources import usgs_waterdata as uw
    except ImportError as e:
        raise ImportError("aihydro-data is required for gauges-in-view") from e

    try:
        sites = list(uw.get_monitoring_locations(
            bbox=[min_lon, min_lat, max_lon, max_lat], site_type_code="ST"))
    except Exception as e:
        log.warning("gauges_in_view USGS query failed: [%s] %s", getattr(e, "code", type(e).__name__), e)
        if problems is not None:
            problems.append(_problem(e))
        return []

    if not sites:
        return []

    upstream = getattr(sites, "upstream", None) or {}
    sites = list(sites)[: max(1, min(limit, _MAX_GAUGES_IN_VIEW))]
    records = daily_discharge_records([f"USGS-{_site_no(loc)}" for loc in sites])
    features: list[dict[str, Any]] = []
    for loc in sites:
        site_no = _site_no(loc)
        name = str(loc.name or site_no)
        props: dict[str, Any] = {"site_no": site_no, "name": name, "label": f"{site_no} — {name}"}
        da = getattr(loc, "drainage_area_raw", None)
        if isinstance(da, (int, float)) and da > 0:
            # USGS documents square miles; the API does not state the unit, so the raw number and that caveat
            # travel together and the km2 value is labelled as a conversion of it.
            props["drainage_area_raw"] = float(da)
            props["drainage_area_unit_note"] = str(getattr(loc, "drainage_area_units", "") or "")
            props["drainage_area_km2_if_sqmi"] = round(float(da) * SQMI_TO_KM2, 1)
        rec = records.get(f"USGS-{site_no}", "unknown")
        if rec == "unknown":
            props["daily_discharge_record"] = "unknown"           # the record query failed: not "none"
        elif rec is None:
            props["daily_discharge_record"] = "none"              # asked: no daily-mean discharge series
        else:
            props["daily_discharge_record"] = "yes"
            props["daily_discharge_begin"] = rec.get("begin")
            props["daily_discharge_end"] = rec.get("end")
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(loc.lon), float(loc.lat)]},
                "properties": props,
            }
        )

    if not features:
        return []

    geojson = {"type": "FeatureCollection", "features": features}
    return stamp_query([
        {
            "id": "nwis-gauges-view",
            "name": f"USGS gauges in view ({len(features)})",
            "layer_type": "point",
            "geojson": json.loads(json.dumps(geojson, default=str)),
            "style_preset": "gauge",
            "metadata": {
                "source": "nwis",
                "feature_count": str(len(features)),
            },
        }
    ], (min_lon, min_lat, max_lon, max_lat), request_urls=list(upstream.get("request_urls") or []) or None)


_MAX_DAMS_IN_VIEW = 150


def dams_in_view_layer(
    *,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    lat: float,
    lon: float,
    limit: int = _MAX_DAMS_IN_VIEW,
) -> list[dict[str, Any]]:
    """Build a single point layer spec with NID dams in the map bbox."""
    if not is_conus_point(lat, lon) or not is_conus_bbox(min_lon, min_lat, max_lon, max_lat):
        return []

    try:
        from pygeohydro import NID  # noqa: F401 — import check
    except ImportError as e:
        raise ImportError("pygeohydro is required for dams-in-view") from e

    try:
        dams = fetch_nid_dams_in_bbox(min_lon, min_lat, max_lon, max_lat)
    except Exception as e:
        log.warning("dams_in_view NID query failed: %s", e)
        return []

    if dams is None or dams.empty:
        return []

    dams = dams.head(max(1, min(limit, _MAX_DAMS_IN_VIEW)))
    features: list[dict[str, Any]] = []
    for _, row in dams.iterrows():
        try:
            if hasattr(row, "geometry") and row.geometry is not None:
                lat_v = float(row.geometry.y)
                lon_v = float(row.geometry.x)
            else:
                lat_v = float(row.get("latitude", row.get("lat", 0)))
                lon_v = float(row.get("longitude", row.get("lon", 0)))
        except (TypeError, ValueError):
            continue
        dam_id = str(row.get("id", row.get("nid_id", "")))
        name = _dam_display_name(row)
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon_v, lat_v]},
                "properties": {
                    "dam_id": dam_id,
                    "name": name,
                    "label": f"{dam_id} — {name}" if dam_id else name,
                },
            }
        )

    if not features:
        return []

    geojson = {"type": "FeatureCollection", "features": features}
    return stamp_query([
        {
            "id": "nid-dams-view",
            "name": f"NID dams in view ({len(features)})",
            "layer_type": "point",
            "geojson": json.loads(json.dumps(geojson, default=str)),
            "style_preset": "dam",
            "metadata": {
                "source": "nid",
                "feature_count": str(len(features)),
            },
        }
    ], (min_lon, min_lat, max_lon, max_lat))
