"""Units of the hydrological signatures, for every place that shows a bare number.

``q_mean``, ``q_std``, ``q5``, ``q95`` and ``q_median`` are basin-mean DEPTH rates in mm/day
(m3/s * 86.4 / area_km2), not discharge. A reader who sees ``q_mean = 2.017`` with no unit takes it
for m3/s: one study did, and its answer said "Q_mean 2.017 m3/s" (about 9x off). aihydro-watershed
>= 0.3 puts a ``units`` map in every signatures result; this table covers results produced before
that (older slots, run-log rows, workspace files) and tools-side digests that print a value.

The table mirrors ``aihydro_watershed.signatures.signatures.SIGNATURE_UNITS``; a test pins the two
together whenever the installed watershed exposes it.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

SIGNATURE_UNITS: Dict[str, str] = {
    "q_mean": "mm/day",
    "q_std": "mm/day",
    "q5": "mm/day",
    "q95": "mm/day",
    "q_median": "mm/day",
    "baseflow_index": "dimensionless",
    "runoff_ratio": "dimensionless",
    "stream_elas": "dimensionless",
    "high_q_freq": "days/year",
    "high_q_dur": "days",
    "low_q_freq": "days/year",
    "low_q_dur": "days",
    "zero_q_freq": "dimensionless",       # a fraction of days, not days/year
    "flow_variability": "dimensionless",
    "hfd_mean": "day of water year",
    "half_flow_date_std": "days",
    "slope_fdc": "dimensionless",
    "slope_fdc_sawicz2011": "dimensionless",
}

_NO_UNIT = {"", "-", "dimensionless"}


def is_signatures(slot: Optional[str] = None, tool: Optional[str] = None) -> bool:
    return slot == "signatures" or tool == "extract_hydrological_signatures"


def units_for(fields: Iterable[str], recorded: Any = None) -> Dict[str, str]:
    """``{field: unit}``: the result's own ``units`` map wins; the table fills gaps."""
    own = recorded if isinstance(recorded, dict) else {}
    out: Dict[str, str] = {}
    for f in fields:
        u = own.get(f) if isinstance(own.get(f), str) and own.get(f) else SIGNATURE_UNITS.get(f)
        if u:
            out[f] = u
    return out


def display_unit(unit: Optional[str]) -> str:
    """The unit to print after a value; empty for a dimensionless quantity."""
    return "" if unit is None or unit.strip() in _NO_UNIT else unit.strip()


def with_units(data: dict) -> dict:
    """``data`` with a ``units`` map covering its signature fields (unchanged when already complete)."""
    if not isinstance(data, dict):
        return data
    have = data.get("units") if isinstance(data.get("units"), dict) else {}
    want = units_for((k for k in data if k in SIGNATURE_UNITS), have)
    if all(k in have for k in want):
        return data
    return {**data, "units": {**have, **want}}
