"""
Multi-resolution tile pyramid generator for large rasters.

For rasters with > 8 M pixels, rendering a single full-resolution PNG as a
Leaflet/deck.gl overlay is slow and memory-intensive on the browser side.
This module solves that by generating a lightweight chip-based tile pyramid:

  - **Overview** (single downsampled PNG): always produced, immediately usable
    with the existing ``push_raster_layer`` / BitmapLayer path.
  - **Chip tiles** (optional, ``generate_tiles=True``): one PNG per chip at
    each zoom level using ``CatchmentGridSampler``; enables progressive loading
    for very large basins.
  - **Manifest** (``manifest.json``): maps each chip to its geographic bounds
    so the webview can render chips individually as they load.

Usage
-----
::

    from ai_hydro.analysis.tile_pyramid import generate_tile_pyramid

    result = generate_tile_pyramid(
        raster=dem_da,                  # xr.DataArray  (y, x), any CRS
        output_dir="/tmp/my_session",
        name="twi",
        colormap="RdYlGn_r",
        watershed=basin_geom,          # shapely geometry, same CRS as raster
    )
    # result["overview_png"]  — single downsampled tile ready for BitmapLayer
    # result["manifest_path"] — JSON manifest for progressive tile loading
    # result["bounds_wgs84"]  — [west, south, east, north]

Design notes
~~~~~~~~~~~~
- Overview is always ≤ ``max_overview_px`` pixels on the longest side (default
  2048).  Below this the caller gets the original raster unchanged.
- Chip tiles are 256×256 px (configurable) per zoom level.  Zoom levels span
  ``zoom_range`` (default 5–12) with resolution doubling each step.
- Chips are named ``<z>/<ix>_<iy>.png`` inside the ``output_dir/<name>/``
  directory to avoid filesystem inode exhaustion with deep z/x/y hierarchies.
- The manifest carries the CRS-independent bounds of each chip (WGS84) plus
  the per-level colormap vmin/vmax so the webview can render consistently.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# Rasters larger than this get a tile pyramid; smaller rasters use single-PNG.
DEFAULT_TILE_TRIGGER = 8_000_000  # 8 M cells

# Longest side of the overview PNG.
DEFAULT_MAX_OVERVIEW_PX = 2048

# Default chip size for chip-level tiles.
DEFAULT_CHIP_PX = 256


def generate_tile_pyramid(
    raster: "xr.DataArray",
    output_dir: str,
    name: str,
    colormap: str = "viridis",
    watershed: "Optional[BaseGeometry]" = None,
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
    zoom_range: tuple[int, int] = (5, 12),
    chip_px: int = DEFAULT_CHIP_PX,
    max_overview_px: int = DEFAULT_MAX_OVERVIEW_PX,
    generate_tiles: bool = True,
) -> dict:
    """Generate a multi-resolution tile pyramid for a large raster.

    Parameters
    ----------
    raster : xr.DataArray
        2-D DataArray with ``y, x`` dims.  CRS must be accessible via
        ``rioxarray`` or uniform x/y coordinates (any projected or geographic
        CRS; WGS84 bounds are derived automatically).
    output_dir : str
        Root directory.  Tiles go into ``output_dir/<name>/``.
    name : str
        Layer name used as the sub-directory name and in the manifest.
    colormap : str
        Matplotlib colormap name (e.g. ``"viridis"``, ``"Blues"``,
        ``"RdYlGn"``).
    watershed : shapely geometry or None
        Optional watershed polygon (same CRS as raster) for chip pruning
        inside the ``CatchmentGridSampler``.  If None, chips cover the full
        raster bounding box.
    vmin, vmax : float or None
        Value range for the colormap.  Defaults to the 2nd–98th percentile of
        non-NaN values.
    zoom_range : (int, int)
        Minimum and maximum zoom levels to generate chip tiles for.
    chip_px : int
        Chip side length in pixels (default 256).
    max_overview_px : int
        Longest side of the overview PNG (default 2048).  The overview is
        always produced regardless of ``generate_tiles``.
    generate_tiles : bool
        If True, produce chip-level tiles in addition to the overview.

    Returns
    -------
    dict with keys:

    - ``overview_png`` : absolute path to the downsampled overview PNG.
    - ``manifest_path``: absolute path to the JSON manifest.
    - ``bounds_wgs84`` : [west, south, east, north] in EPSG:4326.
    - ``tile_dir``     : ``output_dir/<name>/`` directory path.
    - ``n_chips``      : total number of chip tiles generated (0 if
                         ``generate_tiles=False``).
    - ``colormap``     : colormap used.
    - ``vmin``, ``vmax``: normalisation range used.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import Normalize
    except ImportError as exc:
        raise ImportError(
            "tile_pyramid requires matplotlib: pip install aihydro-tools[viz]"
        ) from exc

    try:
        import xarray as xr
    except ImportError as exc:
        raise ImportError("tile_pyramid requires xarray") from exc

    # ------------------------------------------------------------------
    # Resolve WGS84 bounds
    # ------------------------------------------------------------------
    bounds_wgs84 = _raster_bounds_wgs84(raster)

    # ------------------------------------------------------------------
    # Colour range
    # ------------------------------------------------------------------
    arr = raster.values.astype(float)
    valid = arr[np.isfinite(arr)]
    if valid.size == 0:
        raise ValueError(f"generate_tile_pyramid: raster '{name}' has no finite pixels")
    if vmin is None:
        vmin = float(np.percentile(valid, 2))
    if vmax is None:
        vmax = float(np.percentile(valid, 98))
    if vmin == vmax:
        vmax = vmin + 1.0

    norm = Normalize(vmin=vmin, vmax=vmax)
    cmap = plt.get_cmap(colormap)

    # ------------------------------------------------------------------
    # Output directory
    # ------------------------------------------------------------------
    tile_dir = Path(output_dir) / name
    tile_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Overview PNG (always generated)
    # ------------------------------------------------------------------
    overview_path = _write_overview(
        arr, norm, cmap, tile_dir, name, max_overview_px
    )

    # ------------------------------------------------------------------
    # Chip tiles (optional)
    # ------------------------------------------------------------------
    n_chips = 0
    manifest: dict = {
        "name": name,
        "colormap": colormap,
        "vmin": vmin,
        "vmax": vmax,
        "bounds_wgs84": bounds_wgs84,
        "overview_png": str(overview_path),
        "chip_px": chip_px,
        "zoom_range": list(zoom_range),
        "levels": [],
    }

    if generate_tiles:
        n_chips = _write_chip_tiles(
            raster=raster,
            arr=arr,
            norm=norm,
            cmap=cmap,
            tile_dir=tile_dir,
            watershed=watershed,
            chip_px=chip_px,
            zoom_range=zoom_range,
            manifest=manifest,
        )

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------
    manifest_path = tile_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    log.info(
        "generate_tile_pyramid: '%s' → %d chip tiles, overview at %s",
        name, n_chips, overview_path,
    )
    return {
        "overview_png": str(overview_path),
        "manifest_path": str(manifest_path),
        "bounds_wgs84": bounds_wgs84,
        "tile_dir": str(tile_dir),
        "n_chips": n_chips,
        "colormap": colormap,
        "vmin": vmin,
        "vmax": vmax,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _raster_bounds_wgs84(raster: "xr.DataArray") -> list[float]:
    """Return [west, south, east, north] in WGS84 for a DataArray."""
    # Try rioxarray first
    try:
        bounds = raster.rio.bounds()  # (west, south, east, north) native CRS
        src_crs = raster.rio.crs
        if src_crs is not None and str(src_crs) not in ("EPSG:4326", "4326"):
            from rasterio.warp import transform_bounds
            west, south, east, north = transform_bounds(src_crs, "EPSG:4326", *bounds)
        else:
            west, south, east, north = bounds
        return [float(west), float(south), float(east), float(north)]
    except Exception:
        pass

    # Fallback: assume x=lon, y=lat (geographic CRS)
    try:
        x = raster.x.values
        y = raster.y.values
        return [float(x.min()), float(y.min()), float(x.max()), float(y.max())]
    except Exception:
        return [-180.0, -90.0, 180.0, 90.0]


def _colorise(arr: np.ndarray, norm, cmap, nodata_alpha: bool = True) -> np.ndarray:
    """Apply norm + cmap to arr; NaN cells get alpha=0.  Returns RGBA float."""
    rgba = cmap(norm(arr))  # (H, W, 4) float32 0–1
    if nodata_alpha:
        rgba[..., 3] = np.isfinite(arr).astype(float)
    return rgba


def _write_overview(
    arr: np.ndarray,
    norm,
    cmap,
    tile_dir: Path,
    name: str,
    max_overview_px: int,
) -> Path:
    """Downsample arr and save as a decoration-free RGBA PNG."""
    import matplotlib.pyplot as plt

    h, w = arr.shape
    if max(h, w) > max_overview_px:
        scale = max_overview_px / max(h, w)
        new_h = max(1, int(h * scale))
        new_w = max(1, int(w * scale))
        # Simple nearest-neighbour downsample — avoids scipy/skimage dep.
        row_idx = (np.arange(new_h) * h / new_h).astype(int)
        col_idx = (np.arange(new_w) * w / new_w).astype(int)
        arr_ds = arr[np.ix_(row_idx, col_idx)]
    else:
        arr_ds = arr

    rgba = _colorise(arr_ds, norm, cmap)
    out_path = tile_dir / f"{name}_overview.png"

    fig, ax = plt.subplots(
        figsize=(rgba.shape[1] / 100, rgba.shape[0] / 100), dpi=100
    )
    ax.imshow(rgba, aspect="auto", interpolation="nearest")
    ax.axis("off")
    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)
    plt.savefig(str(out_path), dpi=100, bbox_inches="tight", pad_inches=0, transparent=True)
    plt.close()

    log.debug("overview PNG: %s  shape=%s", out_path.name, rgba.shape[:2])
    return out_path


def _write_chip_tiles(
    raster: "xr.DataArray",
    arr: np.ndarray,
    norm,
    cmap,
    tile_dir: Path,
    watershed,
    chip_px: int,
    zoom_range: tuple[int, int],
    manifest: dict,
) -> int:
    """Write per-chip tile PNGs at each zoom level.

    Zoom levels are modelled as successive 2× downsampling steps starting from
    the full-resolution raster.  At zoom ``z_max`` (highest detail), chips are
    ``chip_px × chip_px`` source pixels.  At zoom ``z_max - 1``, chips are
    effectively 2× wider in geographic extent (2× downsampled source).

    Returns the total number of chip PNG files written.
    """
    import matplotlib.pyplot as plt

    try:
        from aihydro_data.sampling import CatchmentGridSampler
    except ImportError:
        log.warning("aihydro_data not available; skipping chip tiles")
        return 0

    z_min, z_max = zoom_range
    total_chips = 0

    # Build a dummy watershed covering the full raster if none provided
    if watershed is None:
        try:
            x = raster.x.values
            y = raster.y.values
            from shapely.geometry import box
            watershed = box(float(x.min()), float(y.min()),
                            float(x.max()), float(y.max()))
        except Exception:
            log.warning("Could not build fallback watershed for tile pyramid")
            return 0

    for z in range(z_min, z_max + 1):
        # Determine downsampling factor: z_max = full res, each step halves.
        ds_factor = 2 ** (z_max - z)

        if ds_factor > 1:
            # Downsample array for this level
            h, w = arr.shape
            new_h = max(1, h // ds_factor)
            new_w = max(1, w // ds_factor)
            row_idx = (np.arange(new_h) * h / new_h).astype(int)
            col_idx = (np.arange(new_w) * w / new_w).astype(int)
            arr_z = arr[np.ix_(row_idx, col_idx)]
            # Rebuild a matching DataArray for the sampler
            import xarray as xr
            if raster.x.size > 0 and raster.y.size > 0:
                x_ds = raster.x.values[col_idx]
                y_ds = raster.y.values[row_idx]
            else:
                x_ds = np.linspace(float(raster.x.min()), float(raster.x.max()), new_w)
                y_ds = np.linspace(float(raster.y.max()), float(raster.y.min()), new_h)
            raster_z = xr.DataArray(
                arr_z.astype("float32"),
                dims=("y", "x"),
                coords={"y": y_ds, "x": x_ds},
            )
        else:
            arr_z = arr
            raster_z = raster

        # CatchmentGridSampler at this level
        try:
            sampler = CatchmentGridSampler(
                raster_z, watershed,
                chip_size=chip_px, stride=chip_px,  # non-overlapping
            )
        except Exception as exc:
            log.warning("CatchmentGridSampler at z=%d failed: %s", z, exc)
            continue

        level_chips: list[dict] = []
        level_dir = tile_dir / str(z)
        level_dir.mkdir(parents=True, exist_ok=True)

        for chip in sampler:
            win = chip.window
            row_sl = slice(win.row_off, win.row_off + win.height)
            col_sl = slice(win.col_off, win.col_off + win.width)
            chip_arr = arr_z[row_sl, col_sl]

            # Apply mask: outside watershed → NaN
            chip_arr = np.where(chip.mask, chip_arr, np.nan)

            rgba = _colorise(chip_arr, norm, cmap)
            chip_name = f"{chip.ix}_{chip.iy}.png"
            chip_path = level_dir / chip_name

            fig, ax = plt.subplots(
                figsize=(rgba.shape[1] / 100, rgba.shape[0] / 100), dpi=100
            )
            ax.imshow(rgba, aspect="auto", interpolation="nearest")
            ax.axis("off")
            plt.subplots_adjust(left=0, right=1, top=1, bottom=0)
            plt.savefig(str(chip_path), dpi=100, bbox_inches="tight",
                        pad_inches=0, transparent=True)
            plt.close()

            level_chips.append({
                "path": str(chip_path),
                "bounds": list(chip.bounds),  # (minx, miny, maxx, maxy)
                "ix": chip.ix,
                "iy": chip.iy,
            })
            total_chips += 1

        manifest["levels"].append({
            "zoom": z,
            "ds_factor": ds_factor,
            "chip_px": chip_px,
            "n_chips": len(level_chips),
            "chips": level_chips,
        })
        log.debug("z=%d: %d chips written to %s", z, len(level_chips), level_dir)

    return total_chips


# ---------------------------------------------------------------------------
# Convenience: auto-trigger from plot_raster_tile
# ---------------------------------------------------------------------------

def should_use_tile_pyramid(
    array: np.ndarray,
    trigger_size: int = DEFAULT_TILE_TRIGGER,
) -> bool:
    """Return True if the array is large enough to warrant a tile pyramid."""
    return array.size > trigger_size
