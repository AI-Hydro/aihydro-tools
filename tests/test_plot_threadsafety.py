"""Concurrent plot calls must each write their own figure (T1-R, B2)."""
from __future__ import annotations

import threading

import numpy as np
import pytest

pytest.importorskip("matplotlib")


def _arr(kind: int) -> np.ndarray:
    base = np.linspace(0, 1, 64)[None, :].repeat(64, 0)
    return base if kind == 0 else base[:, ::-1] if kind == 1 else base.T


def test_plot_raster_tile_threads_each_get_their_own_figure(tmp_path):
    import matplotlib.image as mi

    from ai_hydro.analysis.plots import plot_raster_tile

    bounds = [-86.0, 40.0, -85.0, 41.0]
    ref = {}
    for k in range(3):
        d = tmp_path / f"ref{k}"
        p, _ = plot_raster_tile(_arr(k), bounds, str(d), f"r{k}")
        ref[k] = mi.imread(p)

    out, errs = {}, []
    start = threading.Barrier(6)

    def work(i):
        k = i % 3
        try:
            start.wait()
            for rep in range(4):
                p, _ = plot_raster_tile(_arr(k), bounds, str(tmp_path / f"t{i}_{rep}"), f"t{i}")
                out[(i, rep)] = (k, mi.imread(p))
        except Exception as exc:                    # pragma: no cover
            errs.append(exc)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    assert len(out) == 24
    for (i, rep), (k, img) in out.items():
        assert np.array_equal(img, ref[k]), f"thread {i} rep {rep} holds another thread's figure"
