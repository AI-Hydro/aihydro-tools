---
name: model-selection
description: >
  Decision guide for choosing a hydrological model: HBV-light (conceptual),
  LSTM/EA-LSTM (deep learning), or regionalization from CAMELS analogues.
  Use when researcher asks "which model should I use" or "is HBV or LSTM better".
when_to_use: >
  model selection | HBV vs LSTM | which model | gauged vs ungauged |
  conceptual model | deep learning hydrology | regionalization |
  parameter transfer | model choice | simulation vs prediction
domain: modelling
tools_used: [get_session_summary, fetch_camels_us]
citations: [nearing2021lstm, beck2020regionalization]
disable-model-invocation: false
allowed-tools: Read Bash
---

## Purpose

Guide the researcher to the most appropriate modelling approach for their
basin, record length, and research goal. The three pathways are:
1. **HBV-light** — differentiable conceptual model, built-in, no extra install.
2. **LSTM/EA-LSTM** — data-driven deep learning via NeuralHydrology.
3. **Regionalization** — parameter transfer from CAMELS analogue basins.

## When to use

- Researcher has not yet chosen a modelling approach.
- Conflicting advice between HBV and neural approaches.
- Ungauged basin problem — need to transfer parameters.
- Researcher wants to understand the trade-offs before investing compute time.

## When NOT to use

- Model is already chosen and training is in progress → use `calibration-diagnostics`.
- Researcher wants to compare two pre-trained models → `get_model_results` directly.

## Inputs

- Session summary (read via `get_session_summary`).
- Basin metadata available (area, climate zone, record length).
- Research goal (simulation, prediction, ungauged transfer, publication).

## Decision guide

### Step 1 — Is the basin gauged?

**Ungauged basin:**
→ Use regionalization (parameter transfer from CAMELS analogues).
→ Run `fetch_camels_us` to find the 5 most similar gauged basins by
  area, mean annual precipitation, and aridity index.
→ Apply HBV-light parameters from the median of the analogue set.
→ Report uncertainty as the spread across the analogue parameter sets.

**Gauged basin with streamflow in session:** → proceed to Step 2.

### Step 2 — Record length

| Record length | Recommended |
|---|---|
| < 5 years | HBV-light only (LSTM needs ≥ 10 years to generalise) |
| 5–15 years | HBV-light preferred; LSTM feasible with regularisation |
| ≥ 15 years + good forcing | LSTM / EA-LSTM competitive or better |

### Step 3 — Research goal

| Goal | Model |
|---|---|
| Interpretable parameters (recession, soil storage) | HBV-light |
| State-of-the-art performance on benchmark | LSTM (NeuralHydrology) |
| Process understanding, PUB | HBV-light + sensitivity analysis |
| Operational forecasting, data-rich | LSTM |
| Publication benchmark against Nearing et al. 2021 | LSTM |

### Step 4 — Forcing availability

| Forcing | Model |
|---|---|
| GridMET / ERA5 (T, P, PET or computable) | Both |
| Only precipitation available | HBV-light (can derive PET via Hargreaves) |
| Multi-variable (humidity, radiation, wind) | LSTM preferred |

## Recommended output

After running through the decision guide, tell the researcher:
1. **Recommended model** and the top 2 reasons.
2. **Expected NSE range** (HBV-light CONUS median ~0.65; LSTM median ~0.72 per Nearing 2021).
3. **What to prepare** before calling `train_hydro_model`.
4. If regionalization: which CAMELS analogues to pull and how to transfer parameters.

Do NOT promise specific NSE values — report literature ranges, not guarantees.

## Common pitfalls

- Choosing LSTM for a 3-year record → it will overfit; use HBV-light.
- Choosing HBV-light for a publication aiming to beat NeuralHydrology → bias the result.
- Forgetting PET for HBV-light → use Hargreaves (built into train_hbv_light).
- Regionalization from geographically distant analogues → validate with LOO cross-validation.

## Citations

- nearing2021lstm (Nearing et al. 2021 — LSTM vs conceptual models)
- beck2020regionalization (Beck et al. 2020 — regionalization strategies)

## Trigger examples

- "Should I use HBV or LSTM?"
- "My basin is ungauged — how do I model it?"
- "Which model is best for a 5-year record?"
- "I want to publish — which model performs best?"

## Non-trigger examples

- "My model NSE is 0.4 — what's wrong?" → `calibration-diagnostics`.
- "Train the model" → `train_hydro_model` directly.
