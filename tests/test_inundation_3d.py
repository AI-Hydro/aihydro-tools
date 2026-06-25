"""Tests for Phase 4 inundation 3D mesh export."""
from __future__ import annotations

import json

import numpy as np

from ai_hydro.analysis.inundation_3d import (
    bench_inundation_3d_mesh_contract,
    build_water_surface_mesh,
)


def test_mesh_has_triangles_when_depth_present():
    elev = np.full((4, 4), 50.0)
    depth = np.zeros((4, 4))
    depth[1:3, 1:3] = 2.0
    mesh = build_water_surface_mesh(
        elev,
        depth,
        bounds=[-68.1, 44.5, -68.0, 44.6],
        crs="EPSG:4326",
        max_dim=8,
    )
    assert mesh["vertex_count"] > 0
    assert mesh["triangle_count"] > 0
    assert mesh["bounds_wgs84"] is not None


def test_mesh_empty_when_no_depth():
    elev = np.full((3, 3), 50.0)
    depth = np.zeros((3, 3))
    mesh = build_water_surface_mesh(elev, depth, bounds=[0, 0, 1, 1], crs="EPSG:4326")
    assert mesh["vertex_count"] == 0


def test_mesh_json_serializable():
    elev = np.full((4, 4), 50.0, dtype=np.float64)
    depth = np.zeros((4, 4), dtype=np.float64)
    depth[1:3, 1:3] = 2.0
    mesh = build_water_surface_mesh(
        elev,
        depth,
        bounds=[-68.1, 44.5, -68.0, 44.6],
        crs="EPSG:4326",
        max_dim=8,
    )
    json.dumps(mesh)


def test_mesh_sparse_ribbon_uses_per_cell_quads():
    """Isolated inundated cells along a diagonal should still produce visible geometry."""
    elev = np.full((6, 6), 100.0)
    depth = np.zeros((6, 6))
    for i in range(6):
        depth[i, i] = 1.5
    mesh = build_water_surface_mesh(
        elev,
        depth,
        bounds=[-68.1, 44.5, -68.0, 44.6],
        crs="EPSG:4326",
        max_dim=8,
    )
    assert mesh["vertex_count"] >= 4
    assert mesh["triangle_count"] >= 4


def test_bench_contract():
    out = bench_inundation_3d_mesh_contract()
    assert out["vertex_count"] > 0
    assert out["has_bounds"] is True
