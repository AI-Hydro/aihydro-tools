"""
Shared Modelling Utilities (adapter shim)
=========================================

The pure-compute utilities (metrics, unit conversions, device selection,
forcing parsing) now live in the standalone ``aihydro_modelling`` package.
This module re-exports them so existing imports keep working, and retains the
*session-coupled* helpers that must stay in aihydro-tools (the package is
data-source agnostic and never reaches into a HydroSession or the network).

Carve-out boundary:
  • Down in aihydro-modelling : the math + array parsing (re-exported below).
  • Up here (session/fetch)    : ``_load_full_data``, ``fetch_camels_streamflow``,
                                 ``extract_basin_data`` (explicit ``streamflow_source``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

# Re-export the pure utilities from the package (single source of truth).
from aihydro_modelling.metrics import (  # noqa: F401
    _FORCING_MAP,
    _USEFUL_STATIC,
    _best_device,
    _compute_metrics,
    _hargreaves_pet,
    _load_forcing_arrays,
    _q_cfs_to_mm_day,
    _q_cms_to_mm_day,
    _safe_float,
    bootstrap_compute_metrics,
)

log = logging.getLogger("ai_hydro.modelling")

from ai_hydro.modelling.streamflow_source import (  # noqa: F401
    DEFAULT_STREAMFLOW_SOURCE,
    STREAMFLOW_SOURCES,
    normalise_streamflow_source,
    streamflow_record,
)

# ──────────────────────────────────────────────────────────────────────
# CAMELS streamflow loader (data fetch — stays up)
# ──────────────────────────────────────────────────────────────────────

def fetch_camels_streamflow(gauge_id: str, area_km2: float) -> dict[str, float]:
    """
    Fetch CAMELS streamflow for a gauge as a date→mm/day dict.

    Uses pygeohydro.get_camels() which returns a 35-year (1980-2014)
    continuous record for 671 CONUS stations.  Discharge is in cfs;
    converted to mm/day.  Returns empty dict if gauge_id is not in CAMELS.
    """
    try:
        import pygeohydro as gh
        import numpy as np
        _, flow_ds = gh.get_camels()
        station_ids = list(flow_ds.coords["station_id"].values.astype(str))
        if gauge_id not in station_ids:
            log.info("Gauge %s not in CAMELS (671 stations); using session streamflow.", gauge_id)
            return {}
        g = flow_ds.sel(station_id=gauge_id)
        cfs_vals = g["discharge"].values.astype(float)
        time_vals = [str(t)[:10] for t in g["time"].values]
        q_dict: dict[str, float] = {}
        for d, cfs in zip(time_vals, cfs_vals):
            if not np.isnan(cfs):
                q_dict[d] = _q_cfs_to_mm_day(cfs, area_km2)
        log.info("CAMELS streamflow: %d valid days for gauge %s", len(q_dict), gauge_id)
        return q_dict
    except Exception as exc:
        log.warning("CAMELS fetch failed (%s); will use session streamflow.", exc)
        return {}


# ──────────────────────────────────────────────────────────────────────
# Session data loaders (stay up)
# ──────────────────────────────────────────────────────────────────────

def _load_full_data(session: Any, slot: str, gauge_id: str) -> dict:
    """
    Return the full data dict for a session slot, including daily arrays.

    The MCP server strips large arrays from responses to save context, but the
    session JSON on disk always retains them.  Falls back to the workspace JSON
    file if arrays are missing from the in-memory session.
    """
    result = getattr(session, slot, None)
    if result is None:
        tool = {"streamflow": "fetch_streamflow_data",
                "forcing": "fetch_forcing_data"}.get(slot, slot)
        raise ValueError(f"No {slot} cached for gauge {gauge_id}. Run {tool} first.")

    data = result.get("data", {})
    if "dates" not in data and session.workspace_dir:
        fname = f"{slot}_{gauge_id}.json"
        ws_path = Path(session.workspace_dir) / fname
        if ws_path.exists():
            data = json.loads(ws_path.read_text())
    if "dates" not in data:
        raise ValueError(
            f"{slot} data is missing daily arrays for gauge {gauge_id}. "
            "Re-run the fetch tool with an explicit workspace_dir."
        )
    return data


# ──────────────────────────────────────────────────────────────────────
# Session → TrainingData bridge (the lsh/session→modelling wiring)
# ──────────────────────────────────────────────────────────────────────

def extract_basin_data(
    session: Any, gauge_id: str, output_dir: "Path", streamflow_source: str = DEFAULT_STREAMFLOW_SOURCE,
) -> Any:
    """
    Resolve a HydroSession + gauge into an aihydro_modelling.TrainingData bundle.

    This is the single place the data-source decisions live (observed-flow source,
    m³/s→mm/day conversion, static-attribute assembly).  The package downstream
    is data-agnostic and only sees the resolved plain arrays.

    ``streamflow_source`` is explicit and never falls back silently:
      * ``"session"`` (default) — the study's own fetched flow; no network call here.
        Missing flow raises ``MISSING_STREAMFLOW``.
      * ``"camels"`` — opt-in CAMELS-US download via pygeohydro.  A gauge that is not in
        CAMELS (or a failed download) raises ``CAMELS_STREAMFLOW_UNAVAILABLE``.

    Returns
    -------
    (TrainingData, data_source_str)
    """
    from aihydro_modelling import TrainingData

    from aihydro_core.primitives.errors import ToolError
    source = normalise_streamflow_source(streamflow_source)

    from ai_hydro.modelling.gauge import basin_consistency_error
    _bad = basin_consistency_error(session, gauge_id)
    if _bad:
        raise ToolError(code=_bad["code"], message=_bad["message"], recovery=_bad["recovery"])

    area_km2 = session.watershed["data"]["area_km2"]

    q_dict: dict[str, float] = {}
    using_camels = source == "camels"
    if using_camels:
        q_dict = fetch_camels_streamflow(gauge_id, area_km2)
        if not q_dict:
            raise ToolError(
                code="CAMELS_STREAMFLOW_UNAVAILABLE",
                message=(f"streamflow_source='camels' was requested but no CAMELS-US record "
                         f"could be loaded for gauge {gauge_id} (not one of the 671 CAMELS "
                         "gauges, or the download failed)."),
                recovery="Fetch the gauge flow into the study (fetch_streamflow_data) and use "
                         "streamflow_source='session'.",
            )
    else:
        try:
            sf_data = _load_full_data(session, "streamflow", gauge_id)
        except ValueError as exc:
            raise ToolError(
                code="MISSING_STREAMFLOW",
                message=f"streamflow_source='session' but the study has no usable flow: {exc}",
                recovery="Run fetch_streamflow_data for this gauge, or pass "
                         "streamflow_source='camels' to opt in to the CAMELS-US record.",
            ) from exc
        sf_idx = {d[:10]: i for i, d in enumerate(sf_data["dates"])}
        for d, i in sf_idx.items():
            q_mm = _q_cms_to_mm_day(sf_data["q_cms"][i], area_km2)
            if q_mm is not None:
                q_dict[d] = q_mm

    forcing = _load_full_data(session, "forcing", gauge_id)

    # Static attributes for (EA-)LSTM: CAMELS attrs + watershed geo trio.
    camels_data: dict = (session.camels or {}).get("data", {}) if session.camels else {}
    ws_data: dict = session.watershed["data"]
    static_attrs = dict(camels_data)
    static_attrs.setdefault("area_gages2", ws_data.get("area_km2"))
    static_attrs.setdefault("gauge_lat", ws_data.get("gauge_lat"))
    static_attrs.setdefault("gauge_lon", ws_data.get("gauge_lon"))

    # Label the forcing with the product(s) the forcing tool actually served
    # (its "product" field joins each variable's served product), not an
    # assumed "GridMET" — routing may have served DAYMET, ERA5-Land, CHIRPS…
    forcing_product = str(forcing.get("product") or "").strip() or "unknown-forcing"
    data_source = f"{'CAMELS' if using_camels else 'USGS'}+{forcing_product}"

    data = TrainingData(
        gauge_id=gauge_id,
        output_dir=Path(output_dir),
        forcing=forcing,
        q_by_date=q_dict,
        static_attrs=static_attrs,
        data_source=data_source,
    )
    return data, data_source
