# HAND + Synthetic Rating Curve — Method Card

> Phase 0 knowledge card for agents, skeptic, and claims. Canonical decisions: `DECISION_LOG.md`.

## Method

1. **Condition DEM** — fill pits, depressions, resolve flats (same as TWI pipeline).
2. **Flow direction & accumulation** — D8 via pysheds; convert to pyflwdir.
3. **HAND grid** — `flw.hand(drain, elev)` where `drain = acc >= threshold`.
4. **Synthetic rating curve (SRC)** — per reach: invert Manning's equation for stage `h` at discharge `Q`.
5. **Inundation** — depth `= max(0, h - HAND)`; extent where depth > 0.
6. **Uncertainty** — sweep Manning's `n` (and optional SRC params) → low / likely / high extent band.

## Scope (must appear on every map layer)

- Fluvial, steady-state, level-pool
- Not pluvial, coastal, dam-break, or backwater-dominated
- **Not for life-safety decisions** without independent validation

## Known error sources

| Source | Mitigation (planned) |
|--------|----------------------|
| No channel bathymetry in DEM | Bathymetry correction hook (Phase 3+) |
| SRC from terrain geometry only | Hindcast calibration; GFM validation |
| Confluence under-prediction | Level-path / GMS HAND (Phase 1) |
| Bridges/culverts | National bridge layer (future) |
| 30 m DEM | Finer 3DEP where available; caveat chip |

## Validation protocol

For hindcast mode: pick event date + known discharge → model extent → fetch Sentinel-1 **GFM** SAR mask for same date → report CSI, POD, FAR → attach to claim evidence.

## Citations

- NOAA OWP: https://github.com/NOAA-OWP/inundation-mapping
- Aristizabal et al. (2023), *Water Resources Research* — GMS HAND
- Zheng et al. (2018), *JAWRA* — HAND rating curves
