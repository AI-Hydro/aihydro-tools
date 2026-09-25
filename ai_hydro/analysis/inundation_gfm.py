"""
GFM (Global Flood Monitoring) reference extent for hindcast validation.

Live observations require aihydro-data. Explicit fixture mode is for synthetic tests only.
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

    Used only for explicitly requested synthetic tests.
    """
    if len(bounds) < 4:
        raise ValueError("bounds must be [west, south, east, north]")
    west, south, east, north = [float(v) for v in bounds[:4]]
    _parse_date(event_date)
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
                    "synthetic": True,
                    "evidence_kind": "synthetic_fixture",
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
    """Fetch observations, or explicitly requested synthetic test geometry."""
    if use_fixture:
        return {"geojson": fixture_gfm_extent_geojson(bounds, event_date),
                "source": "gfm_fixture", "event_date": event_date, "live": False,
                "synthetic": True, "evidence_kind": "synthetic_fixture",
                "status": "synthetic", "validation_ready": False, "citation": ""}
    try:
        from aihydro_data.flood.gfm import fetch_gfm_extent as data_fetch
    except ImportError as exc:
        raise RuntimeError("GFM observations require aihydro-data; install the data package or supply an observed reference export.") from exc
    result = data_fetch(bounds, event_date, allow_network=allow_network, use_fixture=False)
    # Older installed data packages may still silently return a fixture.
    if result.get("live") is not True or result.get("synthetic") is True or "fixture" in result.get("source", ""):
        raise RuntimeError("GFM observation request returned non-observational data; upgrade aihydro-data or supply an observed reference.")
    return result


def gfm_validation_readiness(reference: dict[str, Any]) -> dict[str, Any]:
    """Extent geometry alone cannot establish a jointly valid comparison grid."""
    return {"status": "not_assessed", "reference_label": reference.get("reference_label", "GFM"),
            "reason": "GFM extent alone does not establish the joint valid observation footprint, model-grid alignment or acquisition-time suitability. No skill metrics calculated.",
            "source_status": reference.get("status", "unknown")}


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
    out["reference_label"] = "Synthetic GFM-like fixture" if out.get("synthetic") else "GFM"
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
    metrics = validate_extent_masks(model, ref, reference_label="Synthetic GFM-like fixture")
    return {
        **metrics,
        "event_date": "2023-07-15",
        "gfm_source": "gfm_fixture",
        "synthetic": True,
        "evidence_kind": "synthetic_fixture",
    }
