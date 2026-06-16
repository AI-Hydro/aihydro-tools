"""
Event rainfall-runoff — SCS Curve-Number runoff + SCS synthetic unit hydrograph.

Pure-compute kernels — no I/O, no session, no network. The MCP wrapper assembles
basin area (watershed), Tc inputs (geomorphic), and CN (cn grid) from the session.

Given a design rainfall depth, these functions produce a design flood hydrograph:

  1. SCS Curve-Number method (NRCS 1986) converts rainfall depth P to direct-
     runoff depth Q via the basin's curve number:
         S = 25400/CN − 254   (potential maximum retention, mm)
         Q = (P − 0.2S)² / (P + 0.8S)   for P > 0.2S, else 0
  2. Time of concentration via Kirpich (1940), from the main flow-path length and
     channel slope.
  3. The SCS dimensionless **synthetic unit hydrograph** (approximated by its
     standard triangular form) scales the runoff depth into a discharge hydrograph
     with peak Qp at time Tp and base time Tb = 2.67·Tp:
         Tp = 0.6·Tc + D/2          (time to peak)
         Qp = 0.208·A·Q / Tp        (m³/s, A in km², Q in mm, Tp in hr)

Using the synthetic unit hydrograph (uniform excess over duration D) deliberately
avoids committing to a region-specific design-storm temporal distribution (SCS
Type I/IA/II/III), keeping the method globally applicable. The design rainfall
depth itself is supplied by the caller (or derived elsewhere from a precipitation
frequency analysis).
"""
from __future__ import annotations

import logging
import math
from typing import Dict, Optional

import numpy as np

log = logging.getLogger(__name__)


# ── SCS Curve-Number runoff ──────────────────────────────────────────────────

def storage_s_mm(cn: float) -> float:
    """Potential maximum retention S = 25400/CN − 254 (mm). CN in (0, 100]."""
    if not (0 < cn <= 100):
        raise ValueError(f"Curve number must be in (0, 100], got {cn}.")
    return 25400.0 / cn - 254.0


def scs_cn_runoff(rainfall_mm: float, cn: float) -> float:
    """Direct-runoff depth Q (mm) from rainfall depth P (mm) via SCS-CN.

    Q = (P − 0.2S)² / (P + 0.8S) for P > 0.2S (initial abstraction), else 0.
    """
    p = float(rainfall_mm)
    s = storage_s_mm(cn)
    ia = 0.2 * s
    if p <= ia:
        return 0.0
    return float((p - ia) ** 2 / (p + 0.8 * s))


# ── Time of concentration ────────────────────────────────────────────────────

def time_of_concentration_kirpich(length_km: float, slope_m_per_m: float) -> float:
    """Time of concentration Tc (hours) via Kirpich (1940), SI form.

    Tc = 0.0663 · L^0.77 · S^(−0.385), L in km, S dimensionless (m/m).
    """
    if length_km <= 0 or slope_m_per_m <= 0:
        raise ValueError("length_km and slope_m_per_m must be positive.")
    return float(0.0663 * length_km ** 0.77 * slope_m_per_m ** -0.385)


# ── SCS synthetic (triangular) unit hydrograph ───────────────────────────────

def scs_triangular_hydrograph(
    area_km2: float,
    tc_hr: float,
    runoff_mm: float,
    dt_hr: Optional[float] = None,
) -> Dict[str, object]:
    """Direct-runoff hydrograph from the SCS synthetic triangular unit hydrograph.

    Parameters
    ----------
    area_km2 : float
        Basin area (km²).
    tc_hr : float
        Time of concentration (hours).
    runoff_mm : float
        Direct-runoff depth (mm) — e.g. from scs_cn_runoff.
    dt_hr : float, optional
        Output time step (hours). Defaults to Tp/10.

    Returns
    -------
    dict: time_hr, discharge_cms, peak_discharge_cms, time_to_peak_hr,
          base_time_hr, runoff_volume_m3.
    """
    if area_km2 <= 0 or tc_hr <= 0:
        raise ValueError("area_km2 and tc_hr must be positive.")

    duration = 0.133 * tc_hr                 # SCS excess-rainfall duration D
    tp = 0.6 * tc_hr + duration / 2.0        # time to peak
    tb = 2.67 * tp                           # triangular base time
    qp = 0.208 * area_km2 * runoff_mm / tp   # peak discharge (m³/s)

    dt = dt_hr if dt_hr else tp / 10.0
    n = max(int(math.ceil(tb / dt)) + 1, 3)
    t = np.linspace(0.0, tb, n)
    q = np.where(
        t <= tp,
        qp * (t / tp),                       # rising limb
        np.maximum(qp * (tb - t) / (tb - tp), 0.0),  # falling limb
    )

    runoff_volume = runoff_mm / 1000.0 * area_km2 * 1e6   # m³
    return {
        "time_hr": [round(float(x), 4) for x in t],
        "discharge_cms": [round(float(x), 4) for x in q],
        "peak_discharge_cms": round(float(qp), 4),
        "time_to_peak_hr": round(float(tp), 4),
        "base_time_hr": round(float(tb), 4),
        "runoff_volume_m3": round(float(runoff_volume), 2),
    }


def design_hydrograph(
    area_km2: float,
    tc_hr: float,
    cn: float,
    rainfall_mm: float,
    dt_hr: Optional[float] = None,
) -> Dict[str, object]:
    """End-to-end design hydrograph: SCS-CN runoff → triangular unit hydrograph."""
    runoff = scs_cn_runoff(rainfall_mm, cn)
    hyd = scs_triangular_hydrograph(area_km2, tc_hr, runoff, dt_hr=dt_hr)
    hyd.update({
        "rainfall_mm": round(float(rainfall_mm), 3),
        "runoff_mm": round(float(runoff), 3),
        "runoff_coefficient": round(float(runoff / rainfall_mm), 4) if rainfall_mm > 0 else 0.0,
        "curve_number": round(float(cn), 1),
    })
    return hyd
