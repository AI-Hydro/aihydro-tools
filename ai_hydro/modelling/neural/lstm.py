"""
NeuralHydrology LSTM / EA-LSTM (adapter shim)
=============================================

The NeuralHydrology dataset writer + training wrapper now live in the standalone
``aihydro_modelling`` package (``aihydro_modelling.backends.neuralhydrology``).
This module keeps the original session-coupled entry points so the MCP runner is
unchanged: it resolves basin data from the HydroSession and delegates downward.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aihydro_modelling.backends.neuralhydrology import (
    train_neural_hydrology as _train_nh,
    write_nh_dataset,
)

from ai_hydro.modelling.metrics import extract_basin_data

log = logging.getLogger("ai_hydro.modelling")


def prepare_nh_data(gauge_id: str, session: Any, data_dir: Path) -> dict:
    """
    Write NeuralHydrology 'generic' dataset files from the AI-Hydro session.

    Resolves the session data, then delegates to
    ``aihydro_modelling.backends.neuralhydrology.write_nh_dataset``.
    """
    data, data_source = extract_basin_data(session, gauge_id, data_dir)
    return write_nh_dataset(
        gauge_id=gauge_id, forcing=data.forcing, q_by_date=data.q_by_date,
        static_attrs=data.static_attrs, data_dir=data_dir, data_source=data_source,
    )


def train_neural_hydrology(
    gauge_id:   str,
    session:    Any,
    output_dir: Path,
    model:      str   = "cudalstm",
    train_start: str  = "1980-10-01",
    train_end:   str  = "2000-09-30",
    val_start:   str  = "2000-10-01",
    val_end:     str  = "2005-09-30",
    test_start:  str  = "2005-10-01",
    test_end:    str  = "2010-09-30",
    hidden_size: int  = 64,
    epochs:      int  = 30,
    seq_length:  int  = 365,
    batch_size:  int  = 256,
    learning_rate: float = 0.001,
) -> dict:
    """
    Train a NeuralHydrology model for a session gauge.

    Resolves the session data, then delegates to
    ``aihydro_modelling.backends.neuralhydrology.train_neural_hydrology``.
    Return schema is unchanged.
    """
    data, data_source = extract_basin_data(session, gauge_id, output_dir)
    return _train_nh(
        gauge_id=gauge_id, forcing=data.forcing, q_by_date=data.q_by_date,
        static_attrs=data.static_attrs, output_dir=output_dir,
        data_source=data_source, model=model,
        train_start=train_start, train_end=train_end,
        val_start=val_start, val_end=val_end,
        test_start=test_start, test_end=test_end,
        hidden_size=hidden_size, epochs=epochs,
        seq_length=seq_length, batch_size=batch_size,
        learning_rate=learning_rate,
    )
