"""
Differentiable HBV-light (adapter shim)
=======================================

The HBV-light simulation kernel and calibration now live in the standalone
``aihydro_modelling`` package (``aihydro_modelling.backends.hbv``).  This module
keeps the original session-coupled entry point so the MCP runner is unchanged:
it resolves the basin data from the HydroSession (CAMELS-vs-session, unit
conversion) and delegates the math downward.

Re-exports ``_hbv_simulate`` / ``_HBV_BOUNDS`` for backward compatibility.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

# Backward-compat re-exports (kernel + bounds now owned by the package).
from aihydro_modelling.backends.hbv import (  # noqa: F401
    _HBV_BOUNDS,
    _hbv_simulate,
    train_hbv,
)

from ai_hydro.modelling.metrics import extract_basin_data

log = logging.getLogger("ai_hydro.modelling")


def train_hbv_light(
    gauge_id: str,
    session: Any,
    output_dir: Path,
    train_start: str = "2000-10-01",
    train_end:   str = "2007-09-30",
    test_start:  str = "2007-10-01",
    test_end:    str = "2010-09-30",
    epochs:      int = 500,
    n_restarts:  int = 3,
    learning_rate: float = 0.05,
    warm_up:     int = 365,
) -> dict:
    """
    Calibrate a differentiable HBV-light model for a session gauge.

    Resolves forcing + observed runoff from the session, then delegates to
    ``aihydro_modelling.backends.hbv.train_hbv``.  Return schema is unchanged.
    """
    data, _ = extract_basin_data(session, gauge_id, output_dir)
    dates, prcp, tmax, tmin, pet = data.forcing_arrays()

    result = train_hbv(
        dates=dates, prcp=prcp, tmax=tmax, tmin=tmin, pet=pet,
        q_by_date=data.q_by_date, output_dir=output_dir,
        gauge_id=gauge_id, data_source=data.data_source,
        train_start=train_start, train_end=train_end,
        test_start=test_start, test_end=test_end,
        epochs=epochs, n_restarts=n_restarts,
        learning_rate=learning_rate, warm_up=warm_up,
    )

    # Drop the private eval arrays the package exposes for CI — the legacy
    # result schema (and the session cache) must not carry full test-set arrays.
    result.pop("_eval_obs", None)
    result.pop("_eval_pred", None)
    return result
