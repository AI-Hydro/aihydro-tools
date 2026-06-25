"""Stem-tracing tests for 3D camera paths."""
from __future__ import annotations

import numpy as np

from ai_hydro.analysis.inundation_3d import (
    bench_stem_camera_path,
    build_camera_path_for_stack,
    trace_main_stem_cells,
)


def test_trace_main_stem_east_chain():
    fdir = np.array([[1, 1, 1, 1]], dtype=np.int32)
    acc = np.array([[5.0, 50.0, 500.0, 5000.0]], dtype=np.float64)
    stem = trace_main_stem_cells(fdir, acc, stream_acc_threshold=5)
    assert stem[0] == (0, 0)
    assert stem[-1] == (0, 3)


def test_bench_stem_camera_path():
    out = bench_stem_camera_path()
    assert out["camera_path_source"] == "flowdir_main_stem"
    assert out["n_keyframes"] == 4
    assert out["stem_downstream"] is True
