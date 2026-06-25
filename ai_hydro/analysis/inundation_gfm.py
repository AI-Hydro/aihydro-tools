"""
GFM (Global Flood Monitoring) reference extent for hindcast validation.

Live fetch is optional (network + future aihydro-data product). Fixture mode
supports bench and offline agent workflows.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

GFM_CITATION = (
    "Copernicus Emergency Management Service Global Flood Monitoring (GFM), "
    "Sentinel-1 SAR inundation product."
)

__all__ = [
    "GFM_CITATION",
    "fixture_gfm_extent_geojson",
    "fetch_gfm_extent",
    "resolve_gfm_reference",
    "bench_gfm_hindcast_validation",
]


def _parse_date(event_date: str) -> str:
    raw = str(event_date).strip()[:10]
    datetime.strptime(raw, "%Y-%m-%d")
    return raw


def fixture_gfm_extent_geojson(
    bounds: list[float],
    event_date: str,
    *,
    inset: float = 0.15,
) -> dict[str, Any]:
    """
    Synthetic GFM-like reference polygon inside bounds (WGS84).

    Used for bench/offline hindcast validation when live GFM is unavailable.
    """
    if len(bounds) < 4:
        raise ValueError("bounds must be [west, south, east, north]")
    west, south, east, north = [float(v) for v in bounds[:4]]
    _parse_date(event_date)
    dx = (east - west) * inset
    dy = (north - south) * inset
    cx = (west + east) / 2.0
    cy = (south + north) / 2.0
    hw = (east - west) * (0.5 - inset)
    hh = (north - south) * (0.5 - inset)
    ring = [
        [cx - hw, cy - hh],
        [cx + hw, cy - hh],
        [cx + hw, cy + hh],
        [cx - hw, cy + hh],
        [cx - hw, cy - hh],
    ]
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {
                    "source": "gfm_fixture",
                    "event_date": event_date,
                    "citation": GFM_CITATION,
                },
            }
        ],
    }


def fetch_gfm_extent(
    bounds: list[float],
    event_date: str,
    *,
    allow_network: bool = True,
    use_fixture: bool = False,
) -> dict[str, Any]:
    """
    Fetch GFM inundation extent GeoJSON for an event date and bounding box.

    When live fetch is unavailable, returns a documented fixture polygon.
    """
    try:
        from aihydro_data.flood.gfm import fetch_gfm_extent as _ad_fetch

        return _ad_fetch(
            bounds,
            event_date,
            allow_network=allow_network,
            use_fixture=use_fixture,
        )
    except ImportError:
        pass

    _parse_date(event_date)
    if use_fixture or not allow_network:
        gj = fixture_gfm_extent_geojson(bounds, event_date)
        return {
            "geojson": gj,
            "source": "gfm_fixture",
            "event_date": event_date,
            "live": False,
            "citation": GFM_CITATION,
            "note": "Synthetic GFM reference for offline/bench validation.",
        }

    # Live path placeholder — wire to aihydro-data GFM product when available.
    try:
        _attempt_live_gfm(bounds, event_date)
    except NotImplementedError as exc:
        log.info("GFM live fetch not available: %s — using fixture", exc)
    except Exception as exc:
        log.warning("GFM live fetch failed: %s — using fixture", exc)

    gj = fixture_gfm_extent_geojson(bounds, event_date)
    return {
        "geojson": gj,
        "source": "gfm_fixture_fallback",
        "event_date": event_date,
        "live": False,
        "citation": GFM_CITATION,
        "note": (
            "Live GFM fetch not yet wired (Phase 2 aihydro-data). "
            "Fixture reference used; pass reference_extent_geojson for observed extent."
        ),
        "recovery": "Provide reference_extent_geojson from GFM portal export.",
    }


def _attempt_live_gfm(bounds: list[float], event_date: str) -> dict[str, Any]:
    """Raise until aihydro-data exposes a GFM inundation product."""
    raise NotImplementedError(
        "GFM live fetch pending aihydro-data product registration (Phase 2)."
    )


def resolve_gfm_reference(
    bounds: list[float],
    event_date: str,
    *,
    allow_network: bool = True,
    use_fixture: bool = False,
) -> dict[str, Any]:
    """Convenience wrapper returning geojson + metadata for hindcast validation."""
    out = fetch_gfm_extent(
        bounds,
        event_date,
        allow_network=allow_network,
        use_fixture=use_fixture,
    )
    out["reference_label"] = "GFM"
    return out


def bench_gfm_hindcast_validation() -> dict[str, Any]:
    """
    End-to-end hindcast metrics on synthetic model vs GFM fixture masks.

    HRB B-066: pipeline returns CSI/POD/FAR with GFM label.
    """
    import numpy as np

    from ai_hydro.analysis.inundation_validation import validate_extent_masks

    model = np.array(
        [
            [0, 1, 1, 0],
            [0, 1, 1, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 0],
        ],
        dtype=bool,
    )
    ref = np.array(
        [
            [0, 1, 1, 1],
            [0, 1, 0, 0],
            [0, 0, 0, 0],
            [0, 0, 0, 0],
        ],
        dtype=bool,
    )
    metrics = validate_extent_masks(model, ref, reference_label="GFM")
    return {
        **metrics,
        "event_date": "2023-07-15",
        "gfm_source": "gfm_fixture",
    }
