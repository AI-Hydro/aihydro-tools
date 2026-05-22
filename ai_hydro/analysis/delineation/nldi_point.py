"""NLDI watershed at a pour point (CONUS) via nearest COMID."""

from __future__ import annotations

import json
import logging

import geopandas as gpd

from ai_hydro.analysis.delineation.types import area_km2
from ai_hydro.core import DataSource, HydroMeta, HydroResult, ToolError

log = logging.getLogger(__name__)

_TOOL_PATH = "ai_hydro.analysis.delineation.nldi_point.delineate_nldi_at_point"

_SOURCES = [
    DataSource(
        name="USGS NLDI",
        url="https://labs.waterdata.usgs.gov/api/nldi/",
    ),
]

# Rough CONUS extent for NLDI hydrologic indexing
CONUS_LON = (-125.0, -66.0)
CONUS_LAT = (24.0, 50.0)

# Pour-point quick pick (no published drainage area): expand off tiny tributary reaches.
_QUICK_TRIBUTARY_MAX_KM2 = 80.0
_QUICK_DOWNSTREAM_KM = 30
_QUICK_MAX_CANDIDATES = 12
_QUICK_MAX_BASIN_KM2 = 15_000.0


def is_conus(lat: float, lon: float) -> bool:
    return CONUS_LON[0] <= lon <= CONUS_LON[1] and CONUS_LAT[0] <= lat <= CONUS_LAT[1]


def snap_outlet_nldi(lat: float, lon: float) -> tuple[float, float, bool]:
    """Snap pour point to nearest NHD COMID location (CONUS). Uses pynhd, not legacy REST."""
    if not is_conus(lat, lon):
        return lat, lon, False
    try:
        from pynhd import NLDI

        df = NLDI().comid_byloc((lon, lat))
        if df.empty:
            return lat, lon, False
        geom = df.geometry.iloc[0]
        return float(geom.y), float(geom.x), True
    except Exception as e:
        log.debug("NLDI outlet snap failed: %s", e)
        return lat, lon, False


def resolve_comid_for_area(
    lat: float,
    lon: float,
    expected_area_km2: float,
    *,
    search_distance_km: int = 120,
    max_candidates: int = 60,
) -> tuple[int, float]:
    """
    Pick an NHD COMID whose indexed basin area best matches ``expected_area_km2``.

    The nearest COMID from ``comid_byloc`` is often a small tributary; this searches
    upstream/downstream flowlines in the NLDI network for a better match.
    """
    from pynhd import NLDI

    nldi = NLDI()
    start = int(nldi.comid_byloc((lon, lat)).comid.iloc[0])
    candidates: set[int] = {start}

    for navigation in ("upstreamMain", "upstreamTributaries", "downstreamMain"):
        try:
            flowlines = nldi.navigate_byid(
                "comid",
                start,
                navigation,
                "flowlines",
                distance=search_distance_km,
            )
            col = "nhdplus_comid" if "nhdplus_comid" in flowlines.columns else "comid"
            if col in flowlines.columns:
                candidates.update(int(c) for c in flowlines[col].dropna().astype(int))
        except Exception as e:
            log.debug("NLDI navigate %s skipped: %s", navigation, e)

    best_comid = start
    best_area = area_km2(nldi.get_basins(start, fsource="comid").dissolve())
    best_err = abs(best_area - expected_area_km2)

    for comid in list(candidates)[:max_candidates]:
        if comid == start:
            continue
        try:
            a = area_km2(nldi.get_basins(comid, fsource="comid").dissolve())
        except Exception:
            continue
        err = abs(a - expected_area_km2)
        if err < best_err:
            best_err = err
            best_comid = comid
            best_area = a

    log.info(
        "COMID area match: start=%s -> selected=%s (%.0f km², target %.0f km²)",
        start,
        best_comid,
        best_area,
        expected_area_km2,
    )
    return best_comid, best_area


def resolve_comid_for_quick(
    lat: float,
    lon: float,
    *,
    search_distance_km: int = _QUICK_DOWNSTREAM_KM,
    max_candidates: int = _QUICK_MAX_CANDIDATES,
) -> tuple[int, float]:
    """
    Pick a main-channel COMID for map quick delineation (no expected_area_km2).

    Nearest ``comid_byloc`` is often a tributary reach (<80 km²). Walk
    ``downstreamMain`` and prefer the largest basin among nearby candidates.
    """
    from pynhd import NLDI

    nldi = NLDI()
    start = int(nldi.comid_byloc((lon, lat)).comid.iloc[0])
    start_area = area_km2(nldi.get_basins(start, fsource="comid").dissolve())

    if start_area >= _QUICK_TRIBUTARY_MAX_KM2:
        return start, start_area

    candidates: set[int] = {start}
    try:
        flowlines = nldi.navigate_byid(
            "comid",
            start,
            "downstreamMain",
            "flowlines",
            distance=search_distance_km,
        )
        col = "nhdplus_comid" if "nhdplus_comid" in flowlines.columns else "comid"
        if col in flowlines.columns:
            candidates.update(int(c) for c in flowlines[col].dropna().astype(int))
    except Exception as e:
        log.debug("NLDI downstreamMain skipped: %s", e)

    best_comid = start
    best_area = start_area
    for comid in list(candidates)[:max_candidates]:
        if comid == start:
            continue
        try:
            a = area_km2(nldi.get_basins(comid, fsource="comid").dissolve())
        except Exception:
            continue
        if a > best_area:
            best_area = a
            best_comid = comid

    if best_area > _QUICK_MAX_BASIN_KM2:
        log.info(
            "Quick COMID expansion capped at %.0f km²; using start COMID %s (%.0f km²)",
            _QUICK_MAX_BASIN_KM2,
            start,
            start_area,
        )
        return start, start_area

    log.info(
        "Quick COMID: start=%s (%.0f km²) -> selected=%s (%.0f km²)",
        start,
        start_area,
        best_comid,
        best_area,
    )
    return best_comid, best_area


def _normalize_nldi_basins(basins: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if basins.empty:
        return basins
    if basins.geometry.name is None:
        if "geometry" in basins.columns:
            basins = basins.set_geometry("geometry")
        else:
            geom_cols = [c for c in basins.columns if basins[c].dtype.name == "geometry"]
            if geom_cols:
                basins = basins.set_geometry(geom_cols[0])
    if basins.crs is None:
        basins = basins.set_crs("EPSG:4326")
    else:
        basins = basins.to_crs(4326)
    return basins.dissolve().reset_index(drop=True)


def nldi_basin_gdf(
    lat: float,
    lon: float,
    *,
    expected_area_km2: float | None = None,
) -> gpd.GeoDataFrame | None:
    """Return dissolved NLDI basin polygon for (lat, lon), or None if unavailable."""
    try:
        result = delineate_nldi_at_point(
            lat, lon, expected_area_km2=expected_area_km2
        )
    except Exception:
        return None
    geom = result.data.get("geometry_geojson")
    if not geom:
        return None
    gdf = gpd.GeoDataFrame.from_features([geom], crs=4326)
    if gdf.empty or gdf.geometry.is_empty.all():
        return None
    return gdf.dissolve().reset_index(drop=True)


def delineate_nldi_at_point(
    lat: float,
    lon: float,
    *,
    expected_area_km2: float | None = None,
) -> HydroResult:
    """
    Delineate using NLDI: snap to nearest NHD COMID, then fetch indexed basin.

    When ``expected_area_km2`` is set, searches the local river network for a COMID
    whose basin area is a closer match (avoids tiny tributary mis-picks).
    """
    if not is_conus(lat, lon):
        raise ToolError(
            code="OUT_OF_DOMAIN",
            message="NLDI point delineation is only available for CONUS.",
            tool=_TOOL_PATH,
            recovery="Use method='fast' or merit_basins outside CONUS.",
        )

    from pynhd import NLDI

    nldi = NLDI()
    comid_df = nldi.comid_byloc((lon, lat))
    if comid_df.empty:
        raise ToolError(
            code="NLDI_NOT_FOUND",
            message=f"No NHD COMID found near ({lat}, {lon}).",
            tool=_TOOL_PATH,
        )

    comid = int(comid_df.comid.iloc[0])
    if expected_area_km2 and expected_area_km2 > 0:
        comid, _ = resolve_comid_for_area(lat, lon, expected_area_km2)
    else:
        comid, _ = resolve_comid_for_quick(lat, lon)

    basins = nldi.get_basins(comid, fsource="comid")
    if basins.empty:
        raise ToolError(
            code="DELINEATION_FAILED",
            message=f"NLDI returned empty basin for COMID {comid}.",
            tool=_TOOL_PATH,
        )

    dissolved = _normalize_nldi_basins(basins)
    a = area_km2(dissolved)
    fc = json.loads(dissolved.to_json())
    geom = fc["features"][0]
    geom.setdefault("properties", {})

    import ai_hydro

    return HydroResult(
        data={
            "geometry_geojson": geom,
            "area_km2": round(a, 3),
            "outlet_lat": lat,
            "outlet_lon": lon,
            "method_used": "nldi_comid",
            "comid": comid,
            "name": f"basin_comid_{comid}",
        },
        meta=HydroMeta(
            tool=_TOOL_PATH,
            version=getattr(ai_hydro, "__version__", "unknown"),
            params={
                "lat": lat,
                "lon": lon,
                "comid": comid,
                "expected_area_km2": expected_area_km2,
            },
            sources=_SOURCES,
        ),
    )
