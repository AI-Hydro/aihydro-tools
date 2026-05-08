---
name: snow-hydrology-trends
description: >
  Analyzes snowpack and snowmelt dynamics using SNOTEL or gridMET data.
  Computes Snow Water Equivalent (SWE) trends, melt timing (center of mass),
  and rain-on-snow event frequency.
when_to_use: >
  snow | SWE | snowpack | SNOTEL | melt timing | rain-on-snow | 
  snowmelt | winter precipitation | alpine hydrology
domain: interpretation
tools_used: [fetch_forcing_data, run_python]
citations: [mote2018snow]
---

## Purpose

Assess the state and trends of snowpack in a basin. Determine if the snow season is shortening,
if peak SWE is declining, or if melt timing is shifting earlier in the year.

## Workflow

1. **Fetch Snow/Forcing Data.**
   Call `fetch_forcing_data` (gridMET) or use `run_python` to fetch SNOTEL station data if 
   available in the study region.

2. **Compute Snow Metrics.**
   - **Peak SWE**: The maximum snow water equivalent recorded each year.
   - **Snowmelt Timing**: Day of year when 50% of total annual runoff (or melt) has occurred.
   - **Snow-to-Precipitation Ratio**: Fraction of annual precipitation falling as snow.

3. **Trend Analysis.**
   Apply Mann-Kendall or Theil-Sen regression to the metrics over the available record 
   (typically 1980–present).

4. **Interpret Climate Sensitivity.**
   - Relate peak SWE to winter temperature anomalies.
   - Identify years with significant rain-on-snow events (precip > 5mm and temp > 0C and SWE > 0).

5. **Author Summary.**
   Call `write_research_interpretation` describing the shift in snow dynamics and potential
   impacts on summer water availability (e.g., "The basin shows a 3-day/decade shift toward
   earlier melt timing").

## Common Failure Modes
- **Low elevation basins**: Little to no snowpack makes metrics unstable or irrelevant.
- **Data gaps**: SNOTEL stations in remote areas often have sensor failures during extreme cold.
- **Model uncertainty**: gridMET SWE is a modeled product; verify with station data where possible.
