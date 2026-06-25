"""MERIT flowline camera path tests."""
from __future__ import annotations

import numpy as np

from ai_hydro.analysis.inundation_3d import (
    bench_merit_flowline_camera_path,
    build_camera_path_for_stack,
    primary_flowline_coords,
)


def test_primary_flowline_orients_to_outlet():
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"uparea_km2": 900.0},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [-68.0, 44.56],
                        [-68.05, 44.54],
                        [-68.1, 44.52],
                    ],
                },
            }
        ],
    }
    bounds = [-68.2, 44.5, -67.9, 44.6]
    outlet = (-68.0, 44.56)
    coords = primary_flowline_coords(geojson, bounds, outlet_lonlat=outlet)
    assert coords[-1] == outlet


def test_bench_merit_flowline_camera_path():
    out = bench_merit_flowline_camera_path()
    assert out["camera_path_source"] == "merit_flowline"
    assert out["n_keyframes"] == 4


def test_flowline_overrides_d8_stem():
    bounds = [-68.1, 44.5, -67.9, 44.6]
    stack = {
        "fdir": np.array([[1, 1, 1, 1]], dtype=np.int32),
        "acc": np.array([[5.0, 50.0, 500.0, 5000.0]], dtype=np.float64),
        "bounds": bounds,
        "crs": "EPSG:4326",
        "flowline_geojson": {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[-68.05, 44.52], [-67.95, 44.56]],
            },
        },
    }
    _, source = build_camera_path_for_stack(stack, bounds, 3)
    assert source == "merit_flowline"
