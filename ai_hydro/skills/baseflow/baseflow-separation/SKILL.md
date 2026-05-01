---
name: baseflow-separation
description: >
  Separates baseflow from total streamflow using a digital filter method.
  Computes the Baseflow Index (BFI) and provides climate-zone and geology
  interpretation. Use when the user asks about "baseflow", "BFI",
  "groundwater contribution", or "quickflow".
when_to_use: >
  baseflow | BFI | groundwater contribution | quickflow | recession |
  Lyne-Hollick | UKIH | baseflow index | subsurface flow | perennial
domain: baseflow
tools_used: [fetch_streamflow_data, separate_baseflow]
citations: [lyne1979baseflow, eckhardt2005baseflow]
disable-model-invocation: false
allowed-tools: Read Bash
---

## Purpose

Separate the total streamflow hydrograph into baseflow (groundwater + slow
subsurface contribution) and quickflow (direct runoff + rapid interflow).
Compute the Baseflow Index (BFI = mean baseflow / mean total flow) and
interpret what it implies about the basin's storage capacity, geology, and
flow regime.

## When to use

- Researcher asks about baseflow, BFI, groundwater contribution to streamflow.
- Diagnosing why a basin is perennial vs. ephemeral.
- Preparing for signature extraction (separate_baseflow enriches the session).
- Input for hydrological model parameterisation (baseflow recession constant α).

## When NOT to use

- For extreme-flow analysis → use `flood-frequency-analysis`.
- For long-term trend in BFI → Mann-Kendall on the BFI time series via `run_python`.
- For groundwater level prediction → outside current tool scope.

## Inputs

- **Required**: Streamflow in session (`fetch_streamflow_data` must have been called).
- **Optional**: Method ('lyne_hollick' or 'ukih'; default: 'lyne_hollick').
- **Optional**: Alpha parameter 0.9–0.95 (Lyne-Hollick only; default: 0.925).
- **Optional**: Number of forward-backward passes (1 or 3; default: 3).

## Outputs

- Daily baseflow and quickflow series.
- BFI scalar (0–1).
- Methods paragraph with citation.
- Session slot: `baseflow` (written by `separate_baseflow`).

## Workflow

1. **Check prerequisites.**
   Call `get_session_summary` — confirm `streamflow` slot. If missing: call
   `fetch_streamflow_data` first.

2. **Select method.**

   | Condition | Method |
   |---|---|
   | Standard analysis, want alpha control | Lyne-Hollick (default) |
   | Want parameter-free estimate | UKIH smoothed minima |
   | Want to compare both | Run Lyne-Hollick, then UKIH; report range |

   **Alpha guidance (Lyne-Hollick):**
   | Basin type | Recommended α |
   |---|---|
   | Highly permeable (karst, sand) | 0.95 |
   | Mixed geology | 0.925 (default) |
   | Flashy, low baseflow | 0.90 |

3. **Run `separate_baseflow`.**
   Call `separate_baseflow(session_id, method='lyne_hollick', alpha=0.925, n_passes=3)`.

4. **Interpret BFI.**

   | BFI range | Interpretation |
   |---|---|
   | ≥ 0.80 | Strongly groundwater-dominated (karst, deep aquifer) |
   | 0.60–0.79 | Significant subsurface storage (glacial till, fractured rock) |
   | 0.40–0.59 | Mixed regime (soil storage, moderate permeability) |
   | < 0.40 | Flashy, surface-dominated (thin soils, impervious, arid) |

   Cross-check with basin geology, soil type, and TWI if available in session.

5. **Author interpretation.**
   Call `write_research_interpretation` noting:
   - BFI value and what it implies about subsurface storage.
   - Whether BFI is consistent with reported geology/soil type.
   - Method used and alpha value (for Lyne-Hollick).
   - Any caveats (regulated flow, ice effects, data gaps).

## Common failure modes

- **Regulated river** → filter distorts baseflow; note that dam operations inflate apparent BFI.
- **Ephemeral stream** (many zero-flow days) → UKIH preferred; Lyne-Hollick can produce negative baseflow.
- **Short record** (< 5 years) → BFI estimate unreliable; flag to researcher.
- **Snowmelt-dominated** → spring BFI spike is melt, not groundwater; note seasonal pattern.

## Citations

After running this skill:
- lyne1979baseflow (Lyne & Hollick 1979)
- eckhardt2005baseflow (Eckhardt 2005 comparison)

## Trigger examples

- "What is the BFI for this basin?"
- "Separate baseflow from the streamflow record."
- "What fraction of flow comes from groundwater?"

## Non-trigger examples

- "Extract hydrological signatures" → `extract_hydrological_signatures` tool (BFI scalar already there).
- "Model groundwater levels" → outside scope.
