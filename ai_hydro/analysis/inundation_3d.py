"""
Compact 3D water-surface meshes for map presentation (Phase 4).

Builds decimated lon/lat/z meshes from HAND stack elevation + depth grids.
Presentation-only — not used for validation metrics.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

# pysheds / ESRI D8 flow direction codes → (drow, dcol)
_D8_OFFSETS: dict[int, tuple[int, int]] = {
    1: (0, 1),
    2: (1, 1),
    4: (1, 0),
    8: (1, -1),
    16: (0, -1),
    32: (-1, -1),
    64: (-1, 0),
    128: (-1, 1),
}

__all__ = [
    "INUNDATION_3D_CAVEAT",
    "build_water_surface_mesh",
    "build_camera_path",
    "build_camera_path_for_stack",
    "trace_main_stem_cells",
    "write_inundation_3d_manifest",
    "bench_inundation_3d_mesh_contract",
    "bench_camera_path_contract",
    "bench_stem_camera_path",
    "bench_merit_flowline_camera_path",
    "bench_terrain_vertical_offset",
    "try_attach_merit_flowline",
    "primary_flowline_coords",
    "terrain_vertical_metadata",
    "egm96_geoid_undulation_m",
]


INUNDATION_3D_CAVEAT = (
    "3D flood view is a presentation layer (terrain + HAND depth extrusion). "
    "Operational extent and validation metrics remain 2D."
)


def _decimate_stride(nrows: int, ncols: int, max_dim: int) -> tuple[int, slice, slice]:
    max_dim = max(int(max_dim), 4)
    stride_r = max(1, int(np.ceil(nrows / max_dim)))
    stride_c = max(1, int(np.ceil(ncols / max_dim)))
    rs = slice(0, nrows, stride_r)
    cs = slice(0, ncols, stride_c)
    return stride_r, rs, cs


def _cell_centers_wgs84(
    bounds: list[float],
    crs_str: str,
    shape: tuple[int, int],
    row_slice: slice,
    col_slice: slice,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (lons, lats) 2D arrays for decimated cell centers."""
    nrows, ncols = shape
    west, south, east, north = [float(x) for x in bounds]
    cols = np.arange(ncols)[col_slice] + 0.5
    rows = np.arange(nrows)[row_slice] + 0.5
    xs = west + (east - west) * cols / ncols
    ys = north - (north - south) * rows / nrows
    grid_x, grid_y = np.meshgrid(xs, ys)

    crs = (crs_str or "EPSG:4326").upper()
    if crs in ("EPSG:4326", "OGC:CRS84"):
        return grid_x.astype(np.float64), grid_y.astype(np.float64)

    try:
        from pyproj import Transformer

        transformer = Transformer.from_crs(crs_str, "EPSG:4326", always_xy=True)
        flat_x = grid_x.ravel()
        flat_y = grid_y.ravel()
        lons, lats = transformer.transform(flat_x, flat_y)
        return lons.reshape(grid_x.shape), lats.reshape(grid_y.shape)
    except Exception:
        return grid_x.astype(np.float64), grid_y.astype(np.float64)


def _grid_triangle_indices(nrows: int, ncols: int) -> list[int]:
    indices: list[int] = []
    for r in range(nrows - 1):
        for c in range(ncols - 1):
            i = r * ncols + c
            indices.extend([i, i + 1, i + ncols, i + 1, i + ncols + 1, i + ncols])
    return indices


def _build_per_cell_quad_mesh(
    lons: np.ndarray,
    lats: np.ndarray,
    surface_z: np.ndarray,
    valid: np.ndarray,
) -> tuple[list[float], list[int]]:
    """One axis-aligned quad per inundated cell — works for sparse stream ribbons."""
    dr, dc = lons.shape
    if dr > 1:
        half_lat = float(np.nanmedian(np.abs(np.diff(lats, axis=0)))) * 0.5
    else:
        half_lat = 0.00005
    if dc > 1:
        half_lon = float(np.nanmedian(np.abs(np.diff(lons, axis=1)))) * 0.5
    else:
        half_lon = 0.00005
    half_lat = max(half_lat, 1e-6)
    half_lon = max(half_lon, 1e-6)

    positions: list[float] = []
    indices: list[int] = []

    for r in range(dr):
        for c in range(dc):
            if not valid[r, c]:
                continue
            lon_c = float(lons[r, c])
            lat_c = float(lats[r, c])
            z = float(surface_z[r, c])
            base = len(positions) // 3
            positions.extend(
                [
                    lon_c - half_lon,
                    lat_c - half_lat,
                    z,
                    lon_c + half_lon,
                    lat_c - half_lat,
                    z,
                    lon_c + half_lon,
                    lat_c + half_lat,
                    z,
                    lon_c - half_lon,
                    lat_c + half_lat,
                    z,
                ]
            )
            indices.extend([base, base + 1, base + 2, base, base + 2, base + 3])

    return positions, indices


def build_water_surface_mesh(
    elev: np.ndarray,
    depth: np.ndarray,
    bounds: list[float],
    crs: str,
    *,
    max_dim: int = 64,
    min_depth_m: float = 0.05,
) -> dict[str, Any]:
    """
    Build a deck.gl SimpleMesh-compatible payload in WGS84 meters elevation.

    Only cells with depth >= min_depth_m contribute triangles.
    """
    elev_a = np.asarray(elev, dtype=np.float64)
    depth_a = np.asarray(depth, dtype=np.float64)
    if elev_a.shape != depth_a.shape:
        raise ValueError(f"elev/depth shape mismatch: {elev_a.shape} vs {depth_a.shape}")

    nrows, ncols = elev_a.shape
    stride_r, rs, cs = _decimate_stride(nrows, ncols, max_dim)
    elev_d = elev_a[rs, cs]
    depth_d = depth_a[rs, cs]
    dr, dc = elev_d.shape

    lons, lats = _cell_centers_wgs84(bounds, crs, (nrows, ncols), rs, cs)
    surface_z = elev_d + np.where(depth_d >= min_depth_m, depth_d, 0.0)

    valid = np.isfinite(surface_z) & np.isfinite(lons) & np.isfinite(lats)
    valid &= depth_d >= min_depth_m
    valid_count = int(np.sum(valid))

    positions: list[float] = []
    index_map = -np.ones((dr, dc), dtype=np.int32)
    for r in range(dr):
        for c in range(dc):
            if not valid[r, c]:
                continue
            index_map[r, c] = len(positions) // 3
            positions.extend(
                [float(lons[r, c]), float(lats[r, c]), float(surface_z[r, c])]
            )

    indices: list[int] = []
    for r in range(dr - 1):
        for c in range(dc - 1):
            quad = [index_map[r, c], index_map[r, c + 1], index_map[r + 1, c], index_map[r + 1, c + 1]]
            if any(i < 0 for i in quad):
                continue
            i0, i1, i2, i3 = (int(quad[0]), int(quad[1]), int(quad[2]), int(quad[3]))
            indices.extend([i0, i1, i2, i1, i3, i2])

    triangle_count = len(indices) // 3
    # Shallow fluvial floods often form 1-cell-wide stream ribbons — grid
    # connectivity yields almost no triangles. Fall back to per-cell quads.
    if valid_count > 0 and triangle_count < max(8, valid_count // 4):
        positions, indices = _build_per_cell_quad_mesh(lons, lats, surface_z, valid)

    if not positions:
        return {
            "positions": [],
            "indices": [],
            "vertex_count": 0,
            "triangle_count": 0,
            "bounds_wgs84": None,
        }

    lon_arr = np.array(positions[0::3])
    lat_arr = np.array(positions[1::3])
    bounds_wgs84 = [
        float(lon_arr.min()),
        float(lat_arr.min()),
        float(lon_arr.max()),
        float(lat_arr.max()),
    ]
    return {
        "positions": positions,
        "indices": indices,
        "vertex_count": len(positions) // 3,
        "triangle_count": len(indices) // 3,
        "bounds_wgs84": bounds_wgs84,
    }


def _upstream_predecessors(fdir: np.ndarray, row: int, col: int) -> list[tuple[int, int]]:
    """Cells that flow into ``(row, col)`` via their D8 direction."""
    ny, nx = fdir.shape
    preds: list[tuple[int, int]] = []
    for code, (dr, dc) in _D8_OFFSETS.items():
        nr, nc = row - dr, col - dc
        if 0 <= nr < ny and 0 <= nc < nx and int(fdir[nr, nc]) == code:
            preds.append((nr, nc))
    return preds


def trace_main_stem_cells(
    fdir: np.ndarray,
    acc: np.ndarray,
    *,
    stream_acc_threshold: int = 100,
    max_steps: int | None = None,
) -> list[tuple[int, int]]:
    acc_a = np.asarray(acc, dtype=float)
    fdir_a = np.asarray(fdir)
    outlet = np.unravel_index(int(np.argmax(np.where(np.isfinite(acc_a), acc_a, 0))), acc_a.shape)
    path: list[tuple[int, int]] = [outlet]
    limit = max_steps or int(acc_a.size)
    min_acc = max(float(stream_acc_threshold), 5.0)

    for _ in range(limit):
        row, col = path[-1]
        preds = _upstream_predecessors(fdir_a, row, col)
        if not preds:
            break
        next_cell = max(preds, key=lambda rc: float(acc_a[rc[0], rc[1]]))
        if float(acc_a[next_cell[0], next_cell[1]]) < min_acc:
            break
        if next_cell in path:
            break
        path.append(next_cell)

    return list(reversed(path))


def _cell_lonlat(
    row: float,
    col: float,
    shape: tuple[int, int],
    bounds: list[float],
    crs: str,
) -> tuple[float, float]:
    nrows, ncols = shape
    west, south, east, north = [float(x) for x in bounds]
    x = west + (east - west) * (float(col) + 0.5) / ncols
    y = north - (north - south) * (float(row) + 0.5) / nrows
    crs_u = (crs or "EPSG:4326").upper()
    if crs_u in ("EPSG:4326", "OGC:CRS84"):
        return x, y
    try:
        from pyproj import Transformer

        transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        return transformer.transform(x, y)
    except Exception:
        return x, y


def _bearing_deg(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    dlon = math.radians(lon2 - lon1)
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _iter_line_rings(geojson: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """Extract coordinate rings from GeoJSON FeatureCollection or single geometry."""
    rings: list[list[tuple[float, float]]] = []

    def _ring_from_coords(raw: Any) -> list[tuple[float, float]] | None:
        if not raw or not isinstance(raw, list):
            return None
        if isinstance(raw[0], (int, float)):
            return None
        return [(float(pt[0]), float(pt[1])) for pt in raw if isinstance(pt, (list, tuple)) and len(pt) >= 2]

    if geojson.get("type") == "FeatureCollection":
        for feature in geojson.get("features") or []:
            rings.extend(_iter_line_rings(feature))
        return rings

    geom = geojson.get("geometry") if geojson.get("type") == "Feature" else geojson
    if not geom:
        return rings
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "LineString":
        ring = _ring_from_coords(coords)
        if ring and len(ring) >= 2:
            rings.append(ring)
    elif gtype == "MultiLineString":
        for part in coords or []:
            ring = _ring_from_coords(part)
            if ring and len(ring) >= 2:
                rings.append(ring)
    return rings


def _line_score(
    coords: list[tuple[float, float]],
    bounds_wgs84: list[float],
    uparea: float | None,
) -> float:
    if len(coords) < 2:
        return -1.0
    w, s, e, n = [float(x) for x in bounds_wgs84]
    clip = (w, s, e, n)
    inside = [
        (lon, lat)
        for lon, lat in coords
        if clip[0] <= lon <= clip[2] and clip[1] <= lat <= clip[3]
    ]
    if len(inside) < 2:
        return -1.0
    length = sum(
        math.hypot(inside[i + 1][0] - inside[i][0], inside[i + 1][1] - inside[i][1])
        for i in range(len(inside) - 1)
    )
    if uparea is not None and math.isfinite(uparea) and uparea > 0:
        return float(uparea)
    return length


def _feature_uparea(feature: dict[str, Any]) -> float | None:
    props = feature.get("properties") or {}
    for key in ("uparea_km2", "UPAREA", "uparea", "Uparea"):
        val = props.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def primary_flowline_coords(
    geojson: dict[str, Any],
    bounds_wgs84: list[float],
    *,
    outlet_lonlat: tuple[float, float] | None = None,
) -> list[tuple[float, float]]:
    """Pick the dominant MERIT-like line in bounds and orient headwater → outlet."""
    best_coords: list[tuple[float, float]] = []
    best_score = -1.0

    if geojson.get("type") == "FeatureCollection":
        for feature in geojson.get("features") or []:
            for ring in _iter_line_rings(feature):
                score = _line_score(ring, bounds_wgs84, _feature_uparea(feature))
                if score > best_score:
                    best_score = score
                    best_coords = ring
    else:
        uparea = _feature_uparea(geojson) if geojson.get("type") == "Feature" else None
        for ring in _iter_line_rings(geojson):
            score = _line_score(ring, bounds_wgs84, uparea)
            if score > best_score:
                best_score = score
                best_coords = ring

    if len(best_coords) < 2:
        return best_coords
    if outlet_lonlat is None:
        return best_coords

    outlet_lon, outlet_lat = outlet_lonlat
    start = best_coords[0]
    end = best_coords[-1]
    d_start = math.hypot(start[0] - outlet_lon, start[1] - outlet_lat)
    d_end = math.hypot(end[0] - outlet_lon, end[1] - outlet_lat)
    if d_start < d_end:
        return list(reversed(best_coords))
    return best_coords


def _sample_line_coords(coords: list[tuple[float, float]], n: int) -> list[tuple[float, float]]:
    count = max(int(n), 1)
    if count == 1 or len(coords) <= 1:
        return [coords[0]] if coords else []
    seg_lens: list[float] = []
    total = 0.0
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i]
        lon2, lat2 = coords[i + 1]
        seg = math.hypot(lon2 - lon1, lat2 - lat1)
        seg_lens.append(seg)
        total += seg
    if total <= 0:
        idxs = np.linspace(0, len(coords) - 1, count, dtype=int)
        return [coords[int(i)] for i in idxs]

    targets = [total * i / (count - 1) for i in range(count)]
    samples: list[tuple[float, float]] = []
    seg_idx = 0
    walked = 0.0
    for target in targets:
        while seg_idx < len(seg_lens) and walked + seg_lens[seg_idx] < target:
            walked += seg_lens[seg_idx]
            seg_idx += 1
        if seg_idx >= len(seg_lens):
            samples.append(coords[-1])
            continue
        seg = seg_lens[seg_idx]
        t = 0.0 if seg <= 0 else (target - walked) / seg
        lon1, lat1 = coords[seg_idx]
        lon2, lat2 = coords[seg_idx + 1]
        samples.append((lon1 + (lon2 - lon1) * t, lat1 + (lat2 - lat1) * t))
    return samples


def build_camera_path_from_flowline(
    coords: list[tuple[float, float]],
    n_frames: int,
    *,
    pitch: float = 52.0,
) -> list[dict[str, float]]:
    count = max(int(n_frames), 1)
    lonlats = _sample_line_coords(coords, count)
    keyframes: list[dict[str, float]] = []
    for i, (lon, lat) in enumerate(lonlats):
        if i + 1 < len(lonlats):
            bearing = _bearing_deg(lon, lat, lonlats[i + 1][0], lonlats[i + 1][1])
        elif i > 0:
            bearing = _bearing_deg(lonlats[i - 1][0], lonlats[i - 1][1], lon, lat)
        else:
            bearing = 0.0
        keyframes.append(
            {
                "frame_index": float(i),
                "longitude": float(lon),
                "latitude": float(lat),
                "pitch": float(pitch),
                "bearing": float(bearing),
            }
        )
    return keyframes


def try_attach_merit_flowline(
    stack: dict[str, Any],
    bounds_wgs84: list[float],
) -> dict[str, Any]:
    """Attach clipped MERIT river GeoJSON to stack when local vectors are installed."""
    if stack.get("flowline_geojson"):
        return stack
    if len(bounds_wgs84) != 4:
        return stack
    w, s, e, n = [float(x) for x in bounds_wgs84]
    lat, lon = (s + n) / 2.0, (w + e) / 2.0
    try:
        from ai_hydro.data.merit_map_layers import merit_map_layers_for_view

        layers = merit_map_layers_for_view(
            lat=lat,
            lon=lon,
            min_lon=w,
            min_lat=s,
            max_lon=e,
            max_lat=n,
            include_rivers=True,
            include_catchments=False,
            include_level2=False,
        )
        for layer in layers:
            meta = layer.get("metadata") or {}
            if meta.get("merit_layer") == "rivers" and layer.get("geojson"):
                stack["flowline_geojson"] = layer["geojson"]
                stack["flowline_source"] = "merit_rivers"
                break
    except Exception:
        pass
    return stack


def _stack_outlet_lonlat(
    stack: dict[str, Any],
    bounds_wgs84: list[float],
    *,
    stream_acc_threshold: int,
) -> tuple[float, float]:
    fdir = stack.get("fdir")
    acc = stack.get("acc")
    bounds = stack.get("bounds")
    crs = stack.get("crs") or "EPSG:4326"
    if fdir is not None and acc is not None and bounds:
        stem = trace_main_stem_cells(
            np.asarray(fdir),
            np.asarray(acc),
            stream_acc_threshold=stream_acc_threshold,
        )
        if stem:
            return _cell_lonlat(stem[-1][0], stem[-1][1], np.asarray(acc).shape, list(bounds), str(crs))
    w, s, e, n = [float(x) for x in bounds_wgs84]
    return (w + e) / 2.0, (s + n) / 2.0


def build_camera_path_from_stem(
    stack: dict[str, Any],
    bounds_wgs84: list[float],
    n_frames: int,
    *,
    stream_acc_threshold: int = 100,
    pitch: float = 52.0,
) -> tuple[list[dict[str, float]], str]:
    flowline = stack.get("flowline_geojson")
    if flowline:
        outlet = _stack_outlet_lonlat(
            stack,
            bounds_wgs84,
            stream_acc_threshold=stream_acc_threshold,
        )
        coords = primary_flowline_coords(flowline, bounds_wgs84, outlet_lonlat=outlet)
        if len(coords) >= 2:
            return (
                build_camera_path_from_flowline(coords, n_frames, pitch=pitch),
                "merit_flowline",
            )

    fdir = stack.get("fdir")
    acc = stack.get("acc")
    bounds = stack.get("bounds")
    crs = stack.get("crs") or "EPSG:4326"
    if fdir is None or acc is None or not bounds:
        return build_camera_path(bounds_wgs84, n_frames, pitch=pitch), "bounds_major_axis"

    stem = trace_main_stem_cells(
        np.asarray(fdir),
        np.asarray(acc),
        stream_acc_threshold=stream_acc_threshold,
    )
    if len(stem) < 2:
        return build_camera_path(bounds_wgs84, n_frames, pitch=pitch), "bounds_major_axis"

    shape = np.asarray(acc).shape
    count = max(int(n_frames), 1)
    idxs = (
        np.linspace(0, len(stem) - 1, count, dtype=int).tolist()
        if count > 1
        else [len(stem) // 2]
    )

    lonlats = [_cell_lonlat(r, c, shape, list(bounds), str(crs)) for r, c in (stem[i] for i in idxs)]

    keyframes: list[dict[str, float]] = []
    for i, (lon, lat) in enumerate(lonlats):
        if i + 1 < len(lonlats):
            bearing = _bearing_deg(lon, lat, lonlats[i + 1][0], lonlats[i + 1][1])
        elif i > 0:
            bearing = _bearing_deg(lonlats[i - 1][0], lonlats[i - 1][1], lon, lat)
        else:
            bearing = 0.0
        keyframes.append(
            {
                "frame_index": float(i),
                "longitude": float(lon),
                "latitude": float(lat),
                "pitch": float(pitch),
                "bearing": float(bearing),
            }
        )
    return keyframes, "flowdir_main_stem"


def build_camera_path_for_stack(
    stack: dict[str, Any],
    bounds_wgs84: list[float],
    n_frames: int,
    *,
    stream_acc_threshold: int = 100,
    pitch: float = 52.0,
) -> tuple[list[dict[str, float]], str]:
    return build_camera_path_from_stem(
        stack,
        bounds_wgs84,
        n_frames,
        stream_acc_threshold=stream_acc_threshold,
        pitch=pitch,
    )


def build_camera_path(
    bounds_wgs84: list[float],
    n_frames: int,
    *,
    pitch: float = 52.0,
) -> list[dict[str, float]]:
    """
    Simple downstream traverse keyframes for cinematic 3D playback.

    Moves along the major axis of the AOI bounds (presentation-only).
    """
    if len(bounds_wgs84) != 4:
        raise ValueError("bounds_wgs84 must be [west, south, east, north]")
    w, s, e, n = [float(x) for x in bounds_wgs84]
    count = max(int(n_frames), 1)
    lon_span = max(e - w, 1e-6)
    lat_span = max(n - s, 1e-6)
    east_west = lon_span >= lat_span
    bearing = 90.0 if east_west else 0.0

    path: list[dict[str, float]] = []
    for i in range(count):
        t = i / (count - 1) if count > 1 else 0.5
        if east_west:
            lon = w + lon_span * t
            lat = (s + n) / 2.0
        else:
            lon = (w + e) / 2.0
            lat = s + lat_span * t
        path.append(
            {
                "frame_index": float(i),
                "longitude": float(lon),
                "latitude": float(lat),
                "pitch": float(pitch),
                "bearing": bearing,
            }
        )
    return path


# EGM96-lite geoid undulation (m). Fitted to 8 global anchor points for presentation-only
# orthometric → ellipsoid conversion: h_ellipsoid = H_orthometric + N.
_GEOID_COEF = (
    -18.88560578,
    175.0142151,
    -71.96354001,
    -136.31019995,
    88.04765796,
    -139.45839611,
    -27.89851216,
    197.51538412,
    111.69808263,
    61.40159596,
)


def _geoid_basis(lat_deg: float, lon_deg: float) -> tuple[float, ...]:
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    return (
        1.0,
        math.sin(lat),
        math.cos(lat),
        math.sin(2 * lat),
        math.cos(2 * lat),
        math.sin(lon),
        math.cos(lon),
        math.sin(lat) * math.cos(lon),
        math.cos(lat) * math.sin(lon),
        math.sin(2 * lon),
    )


def egm96_geoid_undulation_m(lat_deg: float, lon_deg: float) -> float:
    """Approximate EGM96 geoid height N (m) at WGS84 lat/lon."""
    basis = _geoid_basis(lat_deg, lon_deg)
    return float(sum(c * b for c, b in zip(_GEOID_COEF, basis, strict=True)))


def infer_mesh_vertical_datum(stack: dict[str, Any]) -> str:
    product = str(stack.get("dem_product") or "").lower()
    source = str(stack.get("dem_source") or "").lower()
    explicit = stack.get("mesh_vertical_datum")
    if explicit:
        return str(explicit)
    if "3dep" in product or "3dep" in source or "navd" in product or "navd" in source:
        return "orthometric_navd88"
    crs = str(stack.get("crs") or "").upper()
    bounds = stack.get("bounds")
    if bounds and len(bounds) == 4 and crs in ("EPSG:5070", "EPSG:4269", "EPSG:6318"):
        return "orthometric_navd88"
    return "ellipsoid_wgs84"


def terrain_vertical_metadata(
    stack: dict[str, Any],
    bounds_wgs84: list[float],
) -> dict[str, Any]:
    """Manifest metadata aligning HAND mesh Z with Terrarium ellipsoid terrain."""
    if len(bounds_wgs84) != 4:
        raise ValueError("bounds_wgs84 must be [west, south, east, north]")
    w, s, e, n = [float(x) for x in bounds_wgs84]
    mesh_datum = infer_mesh_vertical_datum(stack)
    offset_m = 0.0
    note = "Mesh and terrain share WGS84 ellipsoid heights; no vertical shift applied."
    if mesh_datum.startswith("orthometric"):
        lat = (s + n) / 2.0
        lon = (w + e) / 2.0
        offset_m = egm96_geoid_undulation_m(lat, lon)
        note = (
            "HAND mesh uses orthometric elevations; Terrarium uses ellipsoid heights. "
            f"Apply +{offset_m:.1f} m to mesh Z when terrain is enabled (EGM96-lite)."
        )
    return {
        "mesh_vertical_datum": mesh_datum,
        "terrain_vertical_datum": "wgs84_ellipsoid_terrarium",
        "terrain_vertical_offset_m": float(offset_m),
        "terrain_vertical_note": note,
    }


def write_inundation_3d_manifest(
    workspace: str | Path,
    prefix: str,
    stack: dict[str, Any],
    frames: list[dict[str, Any]],
    *,
    bounds_wgs84: list[float],
    caveat: str = INUNDATION_3D_CAVEAT,
    max_dim: int = 64,
) -> Path:
    """
    Write manifest + per-frame mesh JSON files under workspace.

    Each entry in ``frames`` must include ``index``, ``depth`` (2D array), and
    optional metadata keys copied into the manifest frame record.
    """
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    mesh_dir = ws / f"{prefix}_3d"
    mesh_dir.mkdir(parents=True, exist_ok=True)

    elev = stack["elev"]
    bounds = stack["bounds"]
    crs = stack.get("crs") or "EPSG:4326"

    frame_records: list[dict[str, Any]] = []
    for frame in frames:
        idx = int(frame["index"])
        depth = frame["depth"]
        mesh = build_water_surface_mesh(
            elev, depth, bounds, crs, max_dim=max_dim,
        )
        mesh_path = mesh_dir / f"water_f{idx:02d}.json"
        mesh_path.write_text(json.dumps(mesh), encoding="utf-8")
        record = {
            "index": idx,
            "mesh_file": mesh_path.name,
            "vertex_count": mesh["vertex_count"],
            "triangle_count": mesh["triangle_count"],
        }
        for key in (
            "time_start",
            "time_end",
            "time_hr",
            "discharge_m3s",
            "stage_m",
            "hydrograph_frame",
        ):
            if key in frame:
                record[key] = frame[key]
        frame_records.append(record)

    camera_path, camera_source = build_camera_path_for_stack(
        stack,
        bounds_wgs84,
        len(frame_records),
    )
    vertical = terrain_vertical_metadata(stack, bounds_wgs84)

    manifest = {
        "version": 1,
        "presentation_tier": "flood_3d",
        "terrain_hint": "terrarium_hillshade_client",
        "cinematic_hint": "frame_camera_path",
        "camera_path_source": camera_source,
        "caveat": caveat,
        "bounds_wgs84": bounds_wgs84,
        "mesh_dir": str(mesh_dir),
        "camera_path": camera_path,
        "frames": frame_records,
        **vertical,
    }
    manifest_path = ws / f"{prefix}_3d_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def bench_camera_path_contract() -> dict[str, Any]:
    """Camera path length matches frame count (B-074)."""
    path = build_camera_path([-68.1, 44.5, -67.9, 44.6], 5)
    return {
        "n_keyframes": len(path),
        "first_longitude": path[0]["longitude"],
        "last_longitude": path[-1]["longitude"],
        "monotonic_lon": path[-1]["longitude"] > path[0]["longitude"],
    }


def bench_stem_camera_path() -> dict[str, Any]:
    """Synthetic D8 stem yields flowdir_main_stem path (B-075)."""
    fdir = np.array(
        [
            [1, 1, 1, 1],
            [64, 64, 64, 64],
            [64, 64, 64, 64],
        ],
        dtype=np.int32,
    )
    acc = np.array(
        [
            [5.0, 50.0, 500.0, 5000.0],
            [1.0, 1.0, 1.0, 1.0],
            [1.0, 1.0, 1.0, 1.0],
        ],
        dtype=np.float64,
    )
    stack = {
        "fdir": fdir,
        "acc": acc,
        "bounds": [-68.1, 44.5, -67.9, 44.6],
        "crs": "EPSG:4326",
    }
    path, source = build_camera_path_for_stack(stack, [-68.1, 44.5, -67.9, 44.6], 4)
    stem = trace_main_stem_cells(fdir, acc, stream_acc_threshold=5)
    return {
        "camera_path_source": source,
        "n_keyframes": len(path),
        "stem_cells": len(stem),
        "stem_downstream": stem[-1] == (0, 3),
    }


def bench_merit_flowline_camera_path() -> dict[str, Any]:
    """Synthetic MERIT flowline overrides D8 stem (B-076)."""
    bounds = [-68.1, 44.5, -67.9, 44.6]
    flowline = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"uparea_km2": 1200.0},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [-68.05, 44.52],
                        [-68.02, 44.54],
                        [-67.98, 44.55],
                        [-67.95, 44.56],
                    ],
                },
            }
        ],
    }
    stack = {
        "fdir": np.array([[1, 1, 1, 1]], dtype=np.int32),
        "acc": np.array([[5.0, 50.0, 500.0, 5000.0]], dtype=np.float64),
        "bounds": bounds,
        "crs": "EPSG:4326",
        "flowline_geojson": flowline,
    }
    path, source = build_camera_path_for_stack(stack, bounds, 4)
    return {
        "camera_path_source": source,
        "n_keyframes": len(path),
        "first_longitude": path[0]["longitude"],
        "last_longitude": path[-1]["longitude"],
        "monotonic_lon": path[-1]["longitude"] > path[0]["longitude"],
    }


def bench_terrain_vertical_offset() -> dict[str, Any]:
    """CONUS 3DEP stack yields negative EGM96-lite offset for Terrarium (B-077)."""
    stack = {
        "dem_product": "3dep_1m",
        "dem_source": "py3dep",
        "bounds": [-68.1, 44.5, -67.9, 44.6],
        "crs": "EPSG:5070",
    }
    meta = terrain_vertical_metadata(stack, [-68.1, 44.5, -67.9, 44.6])
    offset = float(meta["terrain_vertical_offset_m"])
    return {
        "mesh_vertical_datum": meta["mesh_vertical_datum"],
        "terrain_vertical_datum": meta["terrain_vertical_datum"],
        "terrain_vertical_offset_m": offset,
        "offset_in_penobscot_range": -35.0 < offset < -20.0,
    }


def bench_inundation_3d_mesh_contract() -> dict[str, Any]:
    """Synthetic stack → non-empty mesh with WGS84 bounds (B-073)."""
    elev = np.array(
        [[100.0, 100.5, 101.0], [100.2, 100.8, 101.2], [100.4, 101.0, 101.5]],
        dtype=np.float64,
    )
    depth = np.array(
        [[0.0, 1.0, 2.0], [0.0, 1.5, 2.5], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    mesh = build_water_surface_mesh(
        elev,
        depth,
        bounds=[-68.0, 44.5, -67.9, 44.6],
        crs="EPSG:4326",
        max_dim=8,
    )
    return {
        "vertex_count": mesh["vertex_count"],
        "triangle_count": mesh["triangle_count"],
        "has_bounds": mesh["bounds_wgs84"] is not None,
    }
