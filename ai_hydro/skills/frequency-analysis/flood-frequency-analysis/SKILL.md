---
name: flood-frequency-analysis
description: >
  Fits extreme-value distributions to annual peak flows and computes return
  periods (Q2, Q10, Q100). Use when the user asks about "return period",
  "100-year flood", "design flood", or "peak flow statistics".
when_to_use: >
  return period | N-year flood | FFA | design flood | Q100 | annual maxima |
  flood hazard | Gumbel | GEV | LP3 | Bulletin 17C | exceedance probability
domain: frequency-analysis
tools_used: [fetch_streamflow_data, separate_baseflow]
citations: [england2018b17c, usgs_bulletin_17c]
disable-model-invocation: false
allowed-tools: Read Bash
---

## Purpose

Fit an extreme-value distribution to annual peak flows, estimate design flood
quantiles at multiple return periods (Q2, Q10, Q25, Q50, Q100, Q500), and
produce a confidence-interval plot for the flood-frequency curve. Output
includes a methods paragraph ready for insertion into a technical report or
paper.

## When to use

- Researcher asks for return periods, N-year floods, or design flows.
- Flood hazard assessment or dam/bridge design.
- Evaluating the rarity of an observed event ("how rare was the 2011 flood?").
- Benchmarking simulated extremes against observed frequency.

## When NOT to use

- For low-flow analysis → use `signature-interpretation` (Q95, BFI).
- For trend detection in extremes → use Mann-Kendall first, then flag non-stationarity.
- For continuous streamflow simulation → use `model-selection` skill.

## Inputs

- **Required**: Streamflow loaded in session (`fetch_streamflow_data`).
  Minimum 10 years of daily data; at least 20 years recommended.
- **Optional**: Return periods list (default: [2, 10, 25, 50, 100, 500]).
- **Optional**: Distribution family ('gumbel', 'gev', 'lp3', 'auto').

## Outputs

- Annual peak flow series (extracted from daily streamflow).
- Best-fit distribution and AIC score.
- Quantile table: Q_T with 95% bootstrap confidence intervals.
- Methods paragraph citing USGS Bulletin 17C and the chosen distribution.
- Session slot: `flood_frequency` (written by the tool).

## Workflow

1. **Check prerequisites.**
   Call `get_session_summary(session_id)` — confirm `streamflow` slot is
   present. If missing, call `fetch_streamflow_data` first.

2. **Check record length.**
   Extract year-count from `session.streamflow.data.n_days / 365`.
   - < 10 years → warn researcher; proceed but note very high uncertainty.
   - 10–20 years → note wider confidence intervals.
   - ≥ 20 years → standard analysis.

3. **Select distribution family.**

   | Condition | Recommended distribution |
   |---|---|
   | Short record (< 20 yr), symmetric extremes | Gumbel (EV-I) |
   | Long record, heavy tail | GEV (L-moments) |
   | US regulatory context (Bulletin 17C) | LP3 (log-Pearson III) |
   | Unknown / let AIC decide | 'auto' (try all three, pick lowest AIC) |

4. **Compute via run_python.**
   Use `run_python` with `scipy.stats` to:
   - Extract annual maxima: `df.resample('A').max()`.
   - Fit distribution by MLE or L-moments.
   - Compute quantiles at requested return periods.
   - Bootstrap 95% CI (N=1000 resamples).

5. **Write results to session.**
   Store quantile table in `session.flood_frequency` slot via a session
   update. Record the distribution choice and record length.

6. **Flag non-stationarity.**
   Run Mann-Kendall test on the annual maxima series. If p < 0.05, flag
   to the researcher: "Trend detected — stationary FFA may underestimate
   future risk."

7. **Author methods paragraph.**
   Call `write_research_interpretation` with the following template filled in:
   ```
   Annual peak flows were extracted from [N] years of daily discharge
   ([start]–[end]) at USGS gauge [gauge_id]. A [distribution] distribution
   was fitted by [method] and quantiles estimated at six return periods
   (Q2–Q500) following USGS Bulletin 17C (England et al. 2018). The 100-year
   flood (Q100) is estimated at [Q100_value] m³/s (95% CI: [lo]–[hi] m³/s).
   [If trended: A Mann-Kendall test detected a significant trend (p=[p]) in
   annual maxima; stationary estimates may understate future extremes.]
   ```

## Common failure modes

- **Too few years** → AIC unreliable; recommend collecting more data.
- **Non-stationarity** → warn and still produce stationary estimate with caveat.
- **Highly skewed series** (skewness > 2) → LP3 preferred; note the skew.
- **Missing peaks** (regulated rivers, gaps) → note impact on frequency estimates.
- **Ties in annual maxima** → minor issue; LP3 handles ties naturally.

## Citations

After running this skill, add to session citations:
- england2018b17c (USGS Bulletin 17C)
- usgs_bulletin_17c

## Trigger examples

- "What is the 100-year flood at this gauge?"
- "Compute return periods for the session streamflow."
- "Estimate the Q10 and Q100 design flows."
- "How rare was the 2011 peak flow?"

## Non-trigger examples

- "Show me the flow duration curve" → use `signature-interpretation`.
- "Train a flood model" → use `model-selection` first.
- "Detect trends in streamflow" → Mann-Kendall via `run_python` directly.
