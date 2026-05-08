---
name: ungauged-basin-transcription
description: >
  Estimates streamflow for ungauged sites by transcribing data from hydro-chemically 
  or geomorphically similar donor basins (Regionalization).
when_to_use: >
  ungauged | regionalization | donor basin | transcription | 
  prediction in ungauged basins | PUB | proxy site
domain: composition
tools_used: [start_session, extract_camels_attributes, fetch_streamflow_data]
citations: [blöschl2013pub]
---

## Purpose

Provide streamflow estimates for a point of interest that lacks a physical gauge by "transcribing"
the scaled hydrograph from a nearby or similar gauged basin.

## Workflow

1. **Characterize the Target Site.**
   Call `delineate_watershed` and `extract_camels_attributes` (or equivalent) to get the 
   geomorphic and climate signatures of the ungauged site.

2. **Identify Candidate Donors.**
   Search for nearby USGS gauges. Select candidates based on:
   - **Proximity**: Basin centroid distance < 100km.
   - **Similarity**: Close match in drainage area, mean elevation, and land cover.

3. **Validate Donors.**
   For each candidate, call `get_session_summary` (or start a new session) to check 
   streamflow availability and quality.

4. **Perform Transcription.**
   Use `run_python` to:
   - Scale the donor streamflow by the drainage area ratio ($Q_{target} = Q_{donor} \times \frac{A_{target}}{A_{donor}}$).
   - (Optional) Apply a climate adjustment if mean annual precip differs significantly.

5. **Evaluate Uncertainty.**
   If multiple donors are available, report the range of estimates as a measure of uncertainty.

6. **Author Report.**
   Call `write_research_interpretation` explaining the choice of donor basin and the 
   assumptions made during transcription.

## Common Failure Modes
- **No similar donors**: If the target basin is unique (e.g. urban vs rural neighbors), 
  transcription will be highly biased.
- **Nested basins**: If target is inside the donor basin, transcription must account for
  the travel time and intervening area.
- **Karst geology**: Subsurface transfer makes area-scaling invalid.
