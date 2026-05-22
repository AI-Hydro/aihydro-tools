"""
NWIS / NID search and gauge layers for the AI-Hydro map panel.
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


def _site_to_hit(row: Any, source: str = "gauge") -> dict[str, Any]:
    lat = float(row.dec_lat_va)
    lon = float(row.dec_long_va)
    site_no = str(row.site_no)
    name = str(getattr(row, "station_nm", site_no))
    label = f"{site_no} — {name}"
    meta: dict[str, str] = {"gauge_id": site_no}
    if hasattr(row, "drain_area_va") and row.drain_area_va is not None:
        try:
            meta["drainage_area_sqmi"] = str(row.drain_area_va)
        except Exception:
            pass
    return {"label": label, "lat": lat, "lon": lon, "source": source, "meta": meta}


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
) -> list[dict[str, Any]]:
    """
    Search USGS gauges and NID dams. Returns hit dicts for the map SearchBar.
    """
    query = (q or "").strip()
    if len(query) < 2:
        return []

    hits: list[dict[str, Any]] = []
    limit = max(1, min(limit, _MAX_SEARCH_HITS))

    try:
        from pygeohydro import NWIS, NID
    except ImportError as e:
        raise ImportError("pygeohydro is required for hydrology search") from e

    nwis = NWIS()
    nid = NID()

    # Site ID lookup (USGS 5–15 digit ids)
    if _SITE_ID_RE.match(query):
        try:
            sites = nwis.get_info({"sites": query}, expanded=True)
            for _, row in sites.head(limit).iterrows():
                hits.append(_site_to_hit(row))
        except Exception as e:
            log.debug("NWIS site_no search failed: %s", e)

    # Bbox search (gauges + dams in view)
    if (
        len(hits) < limit
        and min_lon is not None
        and min_lat is not None
        and max_lon is not None
        and max_lat is not None
        and is_conus_bbox(min_lon, min_lat, max_lon, max_lat)
    ):
        bbox_str = ",".join(f"{v:.06f}" for v in (min_lon, min_lat, max_lon, max_lat))
        try:
            sites = nwis.get_info(
                {"bBox": bbox_str, "siteTypeCd": "ST", "siteStatus": "all"},
                expanded=True,
            )
            q_lower = query.lower()
            for _, row in sites.iterrows():
                if len(hits) >= limit:
                    break
                site_no = str(row.site_no)
                name = str(getattr(row, "station_nm", ""))
                if _SITE_ID_RE.match(query) and site_no != query:
                    continue
                if len(query) >= 2 and q_lower not in site_no.lower() and q_lower not in name.lower():
                    continue
                hit = _site_to_hit(row)
                if not any(h.get("meta", {}).get("gauge_id") == site_no for h in hits):
                    hits.append(hit)
        except Exception as e:
            log.debug("NWIS bbox search failed: %s", e)

        try:
            dams = fetch_nid_dams_in_bbox(min_lon, min_lat, max_lon, max_lat)
            q_lower = query.lower()
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

    # Name search (CONUS-wide, no bbox required)
    if len(hits) < limit and len(query) >= 3:
        try:
            sites = nwis.get_info(
                {
                    "siteName": query,
                    "siteNameMatch": "2",
                    "siteTypeCd": "ST",
                    "siteStatus": "all",
                },
                expanded=True,
            )
            for _, row in sites.head(limit - len(hits)).iterrows():
                site_no = str(row.site_no)
                if any(h.get("meta", {}).get("gauge_id") == site_no for h in hits):
                    continue
                hits.append(_site_to_hit(row))
        except Exception as e:
            log.debug("NWIS siteName search failed: %s", e)

    return hits[:limit]


def gauges_in_view_layer(
    *,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    lat: float,
    lon: float,
    limit: int = _MAX_GAUGES_IN_VIEW,
) -> list[dict[str, Any]]:
    """Build a single point layer spec with NWIS sites in the map bbox."""
    if not is_conus_point(lat, lon) or not is_conus_bbox(min_lon, min_lat, max_lon, max_lat):
        return []

    try:
        from pygeohydro import NWIS
    except ImportError as e:
        raise ImportError("pygeohydro is required for gauges-in-view") from e

    bbox_str = ",".join(f"{v:.06f}" for v in (min_lon, min_lat, max_lon, max_lat))
    nwis = NWIS()
    try:
        sites = nwis.get_info(
            {"bBox": bbox_str, "siteTypeCd": "ST", "siteStatus": "all"},
            expanded=False,
        )
    except Exception as e:
        log.warning("gauges_in_view NWIS query failed: %s", e)
        return []

    if sites is None or sites.empty:
        return []

    sites = sites.head(max(1, min(limit, _MAX_GAUGES_IN_VIEW)))
    features: list[dict[str, Any]] = []
    for _, row in sites.iterrows():
        try:
            lat_v = float(row.dec_lat_va)
            lon_v = float(row.dec_long_va)
        except (TypeError, ValueError):
            continue
        site_no = str(row.site_no)
        name = str(getattr(row, "station_nm", site_no))
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon_v, lat_v]},
                "properties": {
                    "site_no": site_no,
                    "name": name,
                    "label": f"{site_no} — {name}",
                },
            }
        )

    if not features:
        return []

    geojson = {"type": "FeatureCollection", "features": features}
    return [
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
    ]


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
    return [
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
    ]
