"""
RUSLE soil-loss estimation — pure-compute kernels.

The Revised Universal Soil Loss Equation (Renard et al. 1997) estimates mean
annual soil loss as the product of five factors:

    A = R · K · LS · C · P

    A  — soil loss (t · ha⁻¹ · yr⁻¹)
    R  — rainfall-runoff erosivity (MJ · mm · ha⁻¹ · h⁻¹ · yr⁻¹)
    K  — soil erodibility (t · ha · h · ha⁻¹ · MJ⁻¹ · mm⁻¹, SI units)
    LS — slope length-steepness (dimensionless)
    C  — cover-management (dimensionless, 0–1)
    P  — support-practice (dimensionless, 0–1; 1 = no practice)

These are pure functions — no I/O, no session, no network. The MCP wrapper
assembles the factors from session forcing/soil/geomorphic slots (or accepts
explicit values) and multiplies them.

Every input layer RUSLE needs is already fetchable through the platform's data
layer (precip → R, SOILGRIDS texture → K, DEM slope → LS, landcover → C), so this
is compute over data we already retrieve, not a new backend.

Default factor formulas (all overridable, following the same baked-in-standard-
table pattern as create_cn_grid's NRCS curve-number tables):
  R  — Renard & Freimund (1994) from the Modified Fournier Index (monthly precip).
  K  — EPIC / Williams (1990) from sand/silt/clay/organic-carbon fractions.
  LS — Wischmeier & Smith (1978) with the McCool slope-length exponent.
  C  — a documented per-land-cover-class lookup table (literature defaults).
  P  — 1.0 (no support practice) unless supplied.
"""
from __future__ import annotations

import logging
import math
from typing import Dict, Optional, Sequence, Union

import numpy as np

log = logging.getLogger(__name__)

Number = Union[float, np.ndarray]

# Default cover-management C factors by generic land-cover class. Literature
# typical values (Panagos et al. 2015; Wischmeier & Smith 1978; various global
# RUSLE studies). Overridable via the `table=` argument — same convention as the
# NRCS curve-number tables baked into create_cn_grid.
_DEFAULT_C_FACTORS: Dict[str, float] = {
    "water": 0.0,
    "snow": 0.0,
    "ice": 0.0,
    "urban": 0.0,
    "built": 0.0,
    "developed": 0.0,
    "forest": 0.003,
    "tree": 0.003,
    "wetland": 0.003,
    "shrubland": 0.014,
    "shrub": 0.014,
    "grassland": 0.01,
    "grass": 0.01,
    "savanna": 0.04,
    "herbaceous": 0.04,
    "cropland": 0.24,
    "crop": 0.24,
    "agriculture": 0.24,
    "bare": 0.45,
    "barren": 0.45,
    "sparse": 0.30,
    "moss": 0.05,
    "lichen": 0.05,
}


def rusle(R: Number, K: Number, LS: Number, C: Number, P: Number = 1.0) -> Number:
    """Soil loss A = R·K·LS·C·P (t·ha⁻¹·yr⁻¹). Scalars or broadcastable arrays."""
    return R * K * LS * C * P


# ── R: rainfall erosivity ────────────────────────────────────────────────────

def modified_fournier_index(monthly_precip: Sequence[float]) -> float:
    """Modified Fournier Index F = Σ(p_i²)/P (Arnoldus 1980).

    Parameters
    ----------
    monthly_precip : sequence of 12 floats
        Mean monthly precipitation totals (mm).
    """
    p = np.asarray(monthly_precip, dtype=float)
    if p.size != 12:
        raise ValueError(f"modified_fournier_index needs 12 monthly values, got {p.size}.")
    annual = p.sum()
    if annual <= 0:
        return 0.0
    return float((p ** 2).sum() / annual)


def r_factor_renard_freimund(monthly_precip: Sequence[float]) -> float:
    """R-factor from the Modified Fournier Index (Renard & Freimund 1994).

    Piecewise in F = MFI:
        F ≤ 55 mm : R = 0.07397 · F^1.847
        F > 55 mm : R = 95.77 − 6.081·F + 0.4770·F²
    Returns R in MJ·mm·ha⁻¹·h⁻¹·yr⁻¹.
    """
    F = modified_fournier_index(monthly_precip)
    if F <= 55.0:
        return float(0.07397 * F ** 1.847)
    return float(95.77 - 6.081 * F + 0.4770 * F ** 2)


# ── K: soil erodibility ──────────────────────────────────────────────────────

def k_factor_epic(
    sand_pct: float,
    silt_pct: float,
    clay_pct: float,
    organic_carbon_pct: float = 1.0,
) -> float:
    """Soil erodibility K via the EPIC / Williams (1990) equation.

    Parameters in percent. Returns K in SI units
    (t·ha·h·ha⁻¹·MJ⁻¹·mm⁻¹) — the US-customary EPIC result × 0.1317.
    """
    san, sil, cla, c = sand_pct, silt_pct, clay_pct, organic_carbon_pct
    sn1 = 1.0 - san / 100.0
    f_csand = 0.2 + 0.3 * math.exp(-0.0256 * san * (1.0 - sil / 100.0))
    f_clsi = (sil / (cla + sil)) ** 0.3 if (cla + sil) > 0 else 0.0
    f_orgc = 1.0 - (0.25 * c) / (c + math.exp(3.72 - 2.95 * c))
    f_hisand = 1.0 - (0.7 * sn1) / (sn1 + math.exp(-5.51 + 22.9 * sn1))
    k_us = f_csand * f_clsi * f_orgc * f_hisand
    return float(k_us * 0.1317)   # → SI


# ── LS: slope length-steepness ───────────────────────────────────────────────

def ls_factor_wischmeier(slope_pct: float, slope_length_m: float = 50.0) -> float:
    """LS factor (Wischmeier & Smith 1978, McCool slope-length exponent).

    LS = (λ/22.13)^m · (65.41·sin²θ + 4.56·sinθ + 0.065)
    where θ = arctan(slope_pct/100), and m depends on slope steepness.
    """
    theta = math.atan(slope_pct / 100.0)
    sin_t = math.sin(theta)
    if slope_pct >= 5.0:
        m = 0.5
    elif slope_pct >= 3.0:
        m = 0.4
    elif slope_pct >= 1.0:
        m = 0.3
    else:
        m = 0.2
    l_factor = (slope_length_m / 22.13) ** m
    s_factor = 65.41 * sin_t ** 2 + 4.56 * sin_t + 0.065
    return float(l_factor * s_factor)


# ── C: cover-management ──────────────────────────────────────────────────────

def c_factor_from_landcover(
    land_cover: str,
    table: Optional[Dict[str, float]] = None,
) -> float:
    """Look up a cover-management C factor for a land-cover class name.

    Uses the documented default table (literature typical values) unless an
    override `table` is supplied. Matching is case-insensitive and substring-based
    (e.g. "Deciduous Forest" → "forest"). Unknown classes default to 0.1 with a
    warning.
    """
    tbl = table or _DEFAULT_C_FACTORS
    key = str(land_cover).strip().lower()
    if key in tbl:
        return float(tbl[key])
    for name, val in tbl.items():
        if name in key:
            return float(val)
    log.warning("c_factor_from_landcover: unknown class %r → default 0.1.", land_cover)
    return 0.1


# ── Severity classification ──────────────────────────────────────────────────

def soil_loss_severity(a_t_ha_yr: float) -> str:
    """Classify mean annual soil loss (t·ha⁻¹·yr⁻¹) into a severity band."""
    a = float(a_t_ha_yr)
    if a < 2:
        return "low"            # ≤ tolerable soil-loss threshold (~1–2 t/ha/yr)
    if a < 10:
        return "moderate"
    if a < 50:
        return "high"
    return "severe"
