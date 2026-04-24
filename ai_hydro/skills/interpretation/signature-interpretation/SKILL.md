---
name: signature-interpretation
description: >
  Interprets hydrological signatures (FDC shape, BFI, runoff ratio, Q95/Q05)
  to characterise a basin's flow regime and produce a basin-storyline paragraph
  for paper introductions or study-area descriptions. Use when the researcher
  asks "what do these signatures mean" or wants a basin characterisation.
when_to_use: >
  signature interpretation | FDC | flow duration curve | BFI interpretation |
  runoff ratio | basin characterisation | flow regime | flashy | groundwater |
  nival | Q95 | Q05 | seasonal variability | catchment hydrology
domain: interpretation
tools_used: [extract_hydrological_signatures, get_session_summary]
citations: [addor2018camels]
disable-model-invocation: false
allowed-tools: Read Bash
---

## Purpose

Translate the numerical output of `extract_hydrological_signatures` into a
basin-storyline narrative that explains the flow regime, storage characteristics,
and water balance. Output is a paragraph suitable for a paper introduction or
study-area section.

## When to use

- After `extract_hydrological_signatures` has been called and the researcher
  wants an interpretation.
- For study-area description in a methods section.
- To classify a basin's regime archetype before model selection.
- When a collaborator or reviewer asks "what kind of basin is this?"

## When NOT to use

- Computing numerical signatures → `extract_hydrological_signatures` tool directly.
- For flood-frequency characterisation → `flood-frequency-analysis` skill.
- For baseflow separation → `baseflow-separation` skill.

## Inputs

- Signatures from session (via `get_session_summary` → `signatures` slot).
- Optionally: watershed metadata (area, climate zone, geology if noted).

## Signature → regime mapping

### FDC shape (slope from Q10 to Q90)

| High FDC slope | Low FDC slope |
|---|---|
| High flow variability | Sustained, buffered flow |
| Flashy, thin soils, impervious | Deep storage, permeable geology |
| Arid or semi-arid | Humid or groundwater-fed |

### BFI (Baseflow Index)

| BFI | Regime archetype |
|---|---|
| ≥ 0.80 | Groundwater-dominated (karst, deep glacial aquifer) |
| 0.60–0.79 | Significant subsurface storage (fractured rock, till) |
| 0.40–0.59 | Mixed (moderate permeability, seasonal snowmelt) |
| < 0.40 | Flashy (thin soils, impervious cover, arid ephemeral) |

### Runoff ratio (Q_mean / P_mean)

| Runoff ratio | Water balance interpretation |
|---|---|
| ≥ 0.70 | Wet, low ET, high relief or humid climate |
| 0.40–0.69 | Moderate ET, balanced water balance |
| 0.20–0.39 | High ET demand, semi-arid or deep soil |
| < 0.20 | ET-dominated, arid or very deep water table |

### Q95/Q05 ratio (flow variability index)

Higher ratio = more variable flow (more flashy).
Q95 = flow exceeded 95% of the time (low flow).
Q05 = flow exceeded 5% of the time (near-peak).

| Q95/Q05 | Variability |
|---|---|
| < 0.05 | Very flashy (arid, impervious) |
| 0.05–0.20 | Moderate variability |
| > 0.20 | Sustained high flows (perennial, snowmelt) |

## Workflow

1. Read signatures from session:
   ```
   get_session_summary(session_id) → check 'signatures' slot
   ```

2. Apply the four mapping tables above.

3. Check for cross-signature consistency:
   - High BFI but high FDC slope → unusual; may indicate regulated flow or
     data quality issue.
   - Low runoff ratio but high BFI → deep water table recharging slowly;
     check if basin is in a losing-stream environment.
   - Nival regime: look for Q05 >> Q_mean, winter Q95 near zero.

4. Draft basin-storyline paragraph (3–5 sentences):
   - Sentence 1: Basin overview (area, climate zone if known).
   - Sentence 2: Flow regime archetype from FDC + BFI.
   - Sentence 3: Water balance from runoff ratio.
   - Sentence 4: Variability characterisation from Q95/Q05.
   - Sentence 5 (optional): Implication for modelling (storage parameterisation).

5. Call `write_research_interpretation` with the paragraph.

## Example output paragraph

"The Piscataquis River (1,788 km²) is characterised by a groundwater-
dominated regime typical of the glacially conditioned landscapes of Maine.
The Baseflow Index of 0.71 reflects substantial subsurface storage in glacial
till and thin outwash, sustaining perennial flow through the dry summer
months. A runoff ratio of 0.52 indicates that approximately half of annual
precipitation returns as streamflow, with the remainder consumed by
evapotranspiration, consistent with a humid continental climate. The Q95/Q05
ratio of 0.18 indicates moderate inter-annual flow variability, driven
principally by snowmelt in April–May and summer low-flow recession to
baseflow-sustained conditions."

## Citations

After running this skill:
- addor2018camels (Addor et al. 2018 — CAMELS catchment attributes)

## Trigger examples

- "What do the hydrological signatures tell us about this basin?"
- "Write a study-area characterisation paragraph."
- "Is this basin groundwater-dominated?"
- "What kind of flow regime does this catchment have?"

## Non-trigger examples

- "Compute the signatures" → `extract_hydrological_signatures` tool.
- "What is the 100-year flood?" → `flood-frequency-analysis`.
