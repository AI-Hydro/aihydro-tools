---
name: drought-indices-calculation
description: >
  Computes standardized drought indices (SPI, SPEI) from forcing data.
  Identifies drought onset, severity, and duration for impact assessment.
when_to_use: >
  drought | SPI | SPEI | standardized precipitation | water deficit |
  meteorological drought | dry spell
domain: frequency-analysis
tools_used: [fetch_forcing_data, run_python]
citations: [vicente2010spei]
---

## Purpose

Quantify meteorological drought severity by computing standardized indices. Compare current 
conditions to the historical record to identify extreme dry periods.

## Workflow

1. **Fetch Long-term Forcing.**
   Call `fetch_forcing_data` (gridMET) for at least 30 years. Need Precipitation (Pr) and 
   optionally Potential Evapotranspiration (PET) for SPEI.

2. **Compute Standardized Index.**
   Use `run_python` with `scipy.stats` to:
   - Aggregrate data to the desired scale (e.g. 1, 3, 6, or 12 months).
   - Fit a distribution (Gamma for SPI, Log-Logistic for SPEI).
   - Transform to the standard normal distribution ($Z$-score).

3. **Identify Events.**
   An index value ≤ -1.5 typically indicates "Severe Drought", and ≤ -2.0 indicates "Extreme Drought".

4. **Interpret Trends.**
   Analyze if the frequency or duration of drought events is increasing over the record.

5. **Author Summary.**
   Call `write_research_interpretation` noting the current index value and any significant 
   historical droughts identified.

## Common Failure Modes
- **Aggregration scale**: Short scales (1-3m) show meteorological drought; long scales (12-24m) 
  show groundwater/hydrological drought. Choose based on research question.
- **Record length**: < 30 years makes the standardization unstable for extreme values.
- **PET estimation**: SPEI sensitivity depends on the PET method used (Thornthwaite vs 
  Penman-Monteith).
