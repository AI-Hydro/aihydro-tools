# Session integrity: artifact health, status gate, metric conflicts

What `start_session` / `get_session_summary` (and the claim tools) tell you about a session you return to, and why.
Code: `ai_hydro/session/store.py` (`slot_health`), `ai_hydro/claims/status_gate.py`, `ai_hydro/mcp/tools_ledger.py`
(`_status_gate`), `ai_hydro/skeptic/checks.py` (`find_metric_conflicts`). Tests: `tests/test_session_integrity_wave3.py`.

## Slot health (`slot_health`, `degraded`)

| value | meaning |
|---|---|
| `ok` | nothing found wrong (a `_data_file` with no recorded digest cannot be checked and is `ok`) |
| `missing_artifact` | the slot's `_data_file` does not exist |
| `digest_mismatch` | the file's digest differs from `meta.retained_series.digest` |
| `legacy_convention` | a `signatures` result without `flow_quantile_convention` (q5/q95 may be swapped) or without `_precipitation.completeness` (computed before precipitation completeness) |

`slot_health` maps every stored slot to its worst value across features. Unhealthy slots appear under `degraded`
(`slot`, `health`, `reason`, `fix`) and are removed from `computed`. Nothing is rewritten. Re-run the producing tool
(`clear_session([slot])` first when it answers from cache) before citing a degraded slot.

## Claim status gate

Every path that sets a status runs one gate, in this order:

1. `uncertainty_gate`: metric-scoped `supported` needs verified uncertainty (unchanged).
2. `evidence_gate`: `supported` / `weakly_supported` need at least one evidence span.
3. `forcing_scope_gate`: if a cited run metric's CI declares `scope.excludes` containing `forcing_product`, a claim
   asserting a difference from a reference cannot be `supported` / `weakly_supported`. The scope shape is
   `{"varies": [...], "assumes": [...], "excludes": [...]}`; a missing, free-text or malformed scope never triggers it.
   The rule reads scope metadata only: no tool or metric names. Why: a CI that covers inter-annual sampling only is
   narrower than the forcing-product discrepancy, so "outside the CI" does not mean "different from the reference".

Refusals are typed errors (`error` = gate name, `teaching_error` = `rule`, `explanation`, `how_to_fix`), never tracebacks.
Whether prose "asserts a difference from a reference" is a cheap word heuristic; it can miss paraphrases and errs
towards not blocking. Claims stored before a rule existed are listed under `claim_flags` at session start and left as is.

## Metric conflicts

Skeptic check `metric_conflicts` (also `metric_conflicts` in `start_session`): the same metric (aliases resolved,
`bfi` = `baseflow_index`), feature and period with values differing by more than 1% (relative; an engineering
threshold), taking the latest run per (tool, method). Values that agree across methods are not a conflict. It exists for
sessions that hold results from before a method was corrected, e.g. BFI 0.357 and 0.445 for one gauge and period.
