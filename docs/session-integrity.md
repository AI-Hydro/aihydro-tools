# Session integrity: artifact health, status gate, metric conflicts

What `start_session` / `get_session_summary` (and the claim tools) tell you about a session you return to, and why.
Code: `ai_hydro/session/store.py` (`slot_health`), `ai_hydro/claims/status_gate.py`, `ai_hydro/mcp/tools_ledger.py`
(`_status_gate`), `ai_hydro/skeptic/checks.py` (`find_metric_conflicts`). Tests: `tests/test_session_integrity_wave3.py`.

## Slot health (`slot_health`, `degraded`)

| value | meaning |
|---|---|
| `ok` | nothing found wrong |
| `missing_artifact` | the slot's `_data_file` does not exist, is unreadable, or lies outside the session/workspace data dirs (`outside_session_dir`; symlinks that leave those dirs count as outside) |
| `digest_mismatch` | the file's digest differs from `meta.retained_series.digest` |
| `digest_missing` | the slot declares `meta.retained_series` (or is the streamflow slot) but records no digest, or another path than `_data_file`: the same rule the signatures tool applies before consuming the file |
| `legacy_convention` | a `signatures` result without `flow_quantile_convention` (q5/q95 may be swapped); or, from a producer that emits `signature_definitions` (aihydro-watershed >= 0.2.0), without a `_precipitation` report. A completeness of `None` is legitimate (precipitation unavailable, not attempted or skipped), so presence of the report is what counts |

A fresh slot from watershed 0.1.x (convention key, no `signature_definitions`) is `ok`.

`slot_health` maps every stored slot to its worst value across features. Unhealthy slots appear under `degraded`
(`slot`, `health`, `reason`, `fix`), are removed from `computed`, are listed in `pending` (so the user is prompted to
re-run them), and are not counted by `get_session_health.n_computed_slots` or the defensibility report
(`HydroSession.computed_healthy()`). Other readers of `computed()` are unchanged (follow-up). Nothing is rewritten.
Re-run the producing tool (`clear_session([slot])` first when it answers from cache) before citing a degraded slot.
File digests are hashed in chunks and cached by (path, size, mtime).

## Claim status gate

Every path that sets a status runs one gate, in this order:

1. `uncertainty_gate`: metric-scoped `supported` needs verified uncertainty (unchanged).
2. `evidence_gate`: `supported` / `weakly_supported` need at least one evidence span and every span must be real.
   - `run`: non-empty `source_id`; the run exists in the session run log; it is not hollow (no `error`, not a
     middleware-only `minimal` row, non-empty `key_outputs`); a given `metric_ref` is one of its outputs.
   - `paper` / `dataset`: non-empty `source_id` only. There is no local registry to resolve them against at status
     time; promotion checks further.
3. `forcing_scope_gate` / `comparison_gate` (structural). A `supported` / `weakly_supported` claim is refused when
   - it cites a run whose output carries a `reference_comparison`, or it is a comparison claim (`add_claim(comparison_ref=
     "CAMELS-US v1.2")`: the reference the claim compares against), **and** that metric's CI declares `scope.excludes`
     containing `forcing_product` (`{"varies": [...], "assumes": [...], "excludes": [...]}`): `forcing_scope_gate`; or
   - it is a comparison claim (`comparison_ref` set) and the `reference_comparison` entry for the cited metric says
     `can_claim_difference: false`: `comparison_gate`. A plain claim citing that metric ("mean flow was 2.0 m3/s") is not
     affected: the flag says a difference from the reference cannot be claimed, not that the value cannot be stated.

   `comparison_ref` is part of the sealed claim revision only when set. Follow-up (PR-4b): integration test against real
   watershed 0.2.0 output.

   The rule reads scope and comparison metadata only: no tool or metric names, and no wording. **It is inert until the
   producers (aihydro-watershed 0.2.0 / Wave 3 PR-4b) emit the scope and `reference_comparison` outputs**; a missing,
   free-text or malformed scope never triggers it. Why: a CI that covers inter-annual sampling only is narrower than the
   forcing-product discrepancy, so "outside the CI" does not mean "different from the reference".

A wording heuristic (a difference word and a reference word, negation-aware, including "does not agree with") adds a
non-blocking `FORCING_SCOPE_PROSE` warning when a supported claim's cited CI excludes the forcing product. It is a hint,
never the gate, and can miss paraphrases.

Refusals are typed errors (`error` = gate name, `teaching_error` = `rule`, `explanation`, `how_to_fix`), never tracebacks.

**Stored claims.** Claims saved before a rule existed are listed under `claim_flags` at session start and left as they
are. A stored `supported` claim without real evidence must first gain evidence (record it again with `add_claim` and real
`evidence_spans`) or move to another status: `update_claim_status` re-runs the gate whenever the status it sets is
`supported`, so even changing only its confidence is refused until then.

## Metric conflicts

Skeptic check `metric_conflicts` (also `metric_conflicts` in `start_session`): the same signature metric (allowlist: the
hydrologic signature set; aliases resolved, `bfi` = `baseflow_index`), feature and *known* period (`start_date`/`end_date`,
`years`, `water_year`) with values differing by more than 1% (relative; an engineering threshold). Per (tool,
method/model, container) only the latest run counts. Not compared: model-skill metrics (`nse`, `kge`, ...), generic keys,
runs with an unknown period, and nested outputs other than direct children of container dicts (`signatures`, `data`, ...),
so `obs`/`sim` outputs never collide. Values that agree across methods are not a conflict. It exists for sessions that hold
results from before a method was corrected, e.g. BFI 0.357 and 0.445 for one gauge and period.
