---
name: calibration-diagnostics
description: >
  Diagnoses model calibration quality from NSE, KGE, and RMSE metrics.
  Identifies common pathologies (equifinality, cold-start bias, volume error)
  and recommends next tuning steps. Use when the researcher has model results
  and asks "why is NSE low" or "how can I improve the calibration".
when_to_use: >
  NSE low | KGE poor | calibration | model performance | equifinality |
  volume error | timing error | cold-start | peak underestimation |
  improve model | model diagnostics | recalibrate
domain: modelling
tools_used: [get_model_results]
citations: [gupta2009kge, clark2021pitfalls]
disable-model-invocation: false
allowed-tools: Read Bash
---

## Purpose

Interpret model performance metrics from a completed training run, identify
the failure mode, and recommend concrete next steps. This skill covers the
three principal metrics (NSE, KGE, RMSE) and maps typical values to
calibration pathologies.

## When to use

- After `train_hydro_model` completes and the researcher wants to understand
  the results.
- When metrics are below acceptable thresholds and the researcher asks why.
- When comparing multiple calibration runs.

## When NOT to use

- Model has not yet been trained → use `model-selection` first.
- Researcher is asking which model to use → use `model-selection`.
- Researcher wants to see raw metric values → `get_model_results` directly.

## Inputs

- Model results from session (via `get_model_results`).
- Optionally: per-epoch loss curve (from log file in artifact_dir).

## Performance thresholds

### NSE (Nash-Sutcliffe Efficiency)

| NSE | Assessment |
|---|---|
| ≥ 0.75 | Excellent — publishable, operational quality |
| 0.65–0.74 | Good — typical for well-calibrated conceptual models |
| 0.50–0.64 | Satisfactory — acceptable for research; flag limitations |
| 0.36–0.49 | Poor — only better than mean flow; investigate pathology |
| < 0.36 | Unsatisfactory — model worse than climatology |

NSE is dominated by peak flows. A good NSE can mask poor low-flow simulation.

### KGE decomposition (Gupta et al. 2009)

KGE = 1 − √[(r−1)² + (α−1)² + (β−1)²]

| Component | What it measures | Pathology if far from 1 |
|---|---|---|
| r (correlation) | Timing | Timing/phase errors |
| α (variability ratio) | Flow variability | Flashiness over/under-estimated |
| β (bias ratio) | Volume bias | Systematic over/under-prediction |

Target: KGE > 0.5; KGE > 0.7 for publication.

### RMSE

Interpret in units of m³/s relative to mean discharge.
RMSE / Q_mean < 0.5 is generally acceptable.

## Common pathologies and fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| NSE ≥ 0.7 but β << 1 (volume deficit) | ET too high, S_max too small | Reduce CET or increase FC in HBV |
| NSE ≥ 0.7 but α << 1 (low variability) | Routing too slow | Reduce K0/K1 in HBV; increase PERC |
| NSE < 0.5 and r < 0.6 | Timing error — forcing mismatch | Check precipitation dates, UTC offset |
| Cold-start bias (low NSE first 30 days) | Initialisation | Use warmup period: skip first water-year in loss |
| Peak underestimation (high NSE, poor floods) | Baseflow dominates loss | Use log-NSE or weight peak months |
| Equifinality | Too many free parameters | Reduce to 6–8 parameters for short records |
| Diverging loss after early epochs | Learning rate too high | Reduce by 10×; add cosine annealing |

## Workflow

1. Call `get_model_results(session_id)` to fetch metrics.

2. Apply threshold table to assess overall quality.

3. Compute KGE decomposition if available:
   - If only NSE/KGE/RMSE reported: estimate r, α, β from KGE formula
     by matching available values.
   - If model_dir is accessible: load predictions via `run_python` and
     compute decomposition directly.

4. Map metrics to the pathology table above.

5. Recommend next steps:
   - Concrete parameter adjustments for HBV-light.
   - Hyperparameter changes for LSTM.
   - Data quality checks if correlation (r) is very low.

6. Report findings to researcher in plain language with the metric values
   and a one-sentence diagnosis. Do NOT just restate the numbers — interpret
   them.

## Common failure modes in diagnosis itself

- Comparing NSE across very different basins or seasons without noting the
  comparison is unfair (NSE is harder to achieve in flat basins).
- Treating NSE=0.5 as "good" without noting it means 50% of variance unexplained.
- Ignoring KGE bias component — a model can have NSE=0.7 and still overestimate
  mean annual runoff by 30%.
- Clark et al. 2021: never evaluate only on calibration period; always split.

## Citations

- gupta2009kge (Gupta et al. 2009 — KGE decomposition)
- clark2021pitfalls (Clark et al. 2021 — evaluation pitfalls)

## Trigger examples

- "My NSE is 0.45 — what's wrong?"
- "How can I improve the KGE?"
- "The model keeps underpredicting peaks."
- "Is 0.68 NSE good enough to publish?"

## Non-trigger examples

- "Train the model" → `train_hydro_model` directly.
- "Which model should I use?" → `model-selection`.
