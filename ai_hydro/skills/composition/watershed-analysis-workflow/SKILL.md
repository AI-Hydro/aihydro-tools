---
name: watershed-analysis-workflow
description: >
  End-to-end basin characterisation pipeline: delineate watershed, fetch
  streamflow and forcing, extract signatures, compute TWI and CN grid,
  extract geomorphic parameters, and write a session synopsis. Use when
  the researcher says "analyse this basin", "characterise this watershed",
  or "run a full analysis".
when_to_use: >
  full basin analysis | watershed characterisation | complete analysis |
  basin study | end-to-end pipeline | all tools | characterise this site |
  analyse this gauge | run everything
domain: composition
tools_used: [delineate_watershed, fetch_streamflow_data, fetch_forcing_data, extract_hydrological_signatures, compute_twi, create_cn_grid, extract_geomorphic_parameters, write_research_interpretation]
citations: []
disable-model-invocation: false
allowed-tools: Read Bash
---

## Purpose

Orchestrate all core AI-Hydro analysis tools into a sequential basin
characterisation pipeline. After running this workflow, the session contains
a complete snapshot of the basin's physical, hydrological, and climatic
characteristics, and a research context document is written for future
conversations.

## When to use

- Researcher provides a gauge ID and asks for a full analysis.
- First-time basin study where all slots are empty.
- A systematic basin characterisation for a paper study area section.

## When NOT to use

- One specific analysis is needed → call the individual tool directly.
- Basin is ungauged (no USGS gauge ID) → delineation step will fail;
  substitute with custom polygon via `run_python`.
- Researcher has partial results → check session first, skip completed slots.

## Inputs

- **Required**: USGS gauge ID (8-digit).
- **Optional**: workspace_dir, date range for streamflow (default: full record).
- **Optional**: NLCD year for CN grid (default: 2019).

## Outputs

- Session slots populated: watershed, streamflow, forcing, signatures,
  geomorphic, twi, cn.
- `research.md` written with basin-storyline interpretation.
- Map layers pushed for watershed boundary and gauge location.

## Workflow

### Pre-check (always run first)

```
get_session_summary(session_id)
```
For each slot already computed: SKIP that tool call and note it was cached.
Only run tools for empty slots.

### Step 1 — Watershed delineation

```
delineate_watershed(session_id, gauge_id=<id>, workspace_dir=<ws>)
```
- Retrieves NHD basin polygon + gauge metadata.
- Pushes watershed boundary to map.
- **If error**: check gauge_id is a valid 8-digit USGS ID.

### Step 2 — Streamflow data

```
fetch_streamflow_data(session_id)
```
- Fetches the full NWIS daily discharge record.
- Note: gauge_id was set by Step 1; no need to pass again.

### Step 3 — Forcing data

```
fetch_forcing_data(session_id)
```
- Downloads GridMET daily P, Tmax, Tmin for the watershed centroid.
- May take 1–3 minutes for long records.

### Step 4 — Hydrological signatures

```
extract_hydrological_signatures(session_id)
```
- Computes BFI, runoff ratio, Q95/Q05, FDC slope, mean annual Q.
- Requires streamflow (Step 2) to be complete.

### Step 5 — Geomorphic parameters

```
extract_geomorphic_parameters(session_id)
```
- DEM-based: slope, relief ratio, elongation ratio, stream order.
- Typically takes 30–90 seconds.

### Step 6 — TWI (Topographic Wetness Index)

```
compute_twi(session_id)
```
- Raster TWI grid + statistics.
- May take 1–3 minutes for large basins.
- Pushes raster layer to map.

### Step 7 — Curve Number grid

```
create_cn_grid(session_id)
```
- NLCD land cover + Polaris soils → NRCS Curve Number distribution.
- CN grid pushed to map.
- May take 2–5 minutes.

### Step 8 — Interpretation

After all slots are complete:
1. Call `get_session_raw_state(session_id)` to read all computed slots.
2. Apply `signature-interpretation` skill to synthesise the flow regime.
3. Cross-reference geomorphic parameters with signatures.
4. Call `write_research_interpretation(session_id, site_name=<slug>, interpretation=<prose>)`.

## Parallel opportunities

Steps 5, 6, and 7 are independent after Steps 1–2 complete. If the harness
supports sub-agent delegation, consider running geomorphic, TWI, and CN grid
concurrently via parallel tool calls or sub-agents. Report which ran in parallel.

## Error recovery

| Error | Recovery |
|---|---|
| `delineate_watershed` fails | Verify 8-digit gauge ID; check NLDI availability |
| `fetch_forcing_data` timeout | Retry once; reduce date range if still failing |
| `compute_twi` / `create_cn_grid` fail | Often missing geo extras; note in response |
| Any MISSING_PREREQUISITES | Run required prerequisite tool first |

## Trigger examples

- "Run a full analysis of gauge 01031500."
- "Characterise this watershed for me."
- "I want to study the Piscataquis River basin — analyse it."

## Non-trigger examples

- "Just fetch the streamflow" → `fetch_streamflow_data` directly.
- "What are the signatures?" → `extract_hydrological_signatures` directly.
