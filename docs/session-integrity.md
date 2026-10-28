# Session integrity: artifact health, status gate, metric conflicts

What `start_session` / `get_session_summary` (and the claim tools) tell you about a session you return to, and why.
Code: `ai_hydro/session/store.py` (`slot_health`), `ai_hydro/claims/status_gate.py`, `ai_hydro/mcp/tools_ledger.py`
(`_status_gate`), `ai_hydro/skeptic/checks.py` (`find_metric_conflicts`), and since 2.8.0 `ai_hydro/validators/series_binding.py`,
`ai_hydro/mcp/tools_validators.py`, `ai_hydro/science/camels_reference.py`. Tests: `tests/test_session_integrity_wave3.py`,
`tests/test_signatures_wave3_integration.py` (real aihydro-watershed 0.2.0 output), `tests/test_series_binding_pr4b.py`,
`tests/test_camels_reference_pr4b.py`.

## Slot health (`slot_health`, `degraded`)

| value | meaning |
|---|---|
| `ok` | nothing found wrong |
| `missing_artifact` | the slot's `_data_file` does not exist, is unreadable, or lies outside the session/workspace data dirs (`outside_session_dir`; symlinks that leave those dirs count as outside) |
| `digest_mismatch` | the file's digest differs from `meta.retained_series.digest` |
| `digest_missing` | the slot declares `meta.retained_series` (or is the streamflow slot) but records no digest, or another path than `_data_file`: the same rule the signatures tool applies before consuming the file |
| `legacy_convention` | a `signatures` result without `flow_quantile_convention` (q5/q95 may be swapped); or, from a producer that emits `signature_definitions` (aihydro-watershed >= 0.2.0), without a `_precipitation` report. A completeness of `None` is legitimate (precipitation unavailable, not attempted or skipped), so presence of the report is what counts |

A fresh slot from watershed 0.1.x (convention key, no `signature_definitions`) is `ok`.

Since 2.8.0 `extract_hydrological_signatures` retains the series it consumed (below) and records
`meta.retained_series = {path, digest}` with `data._data_file`, so a fresh signatures slot is checked like the streamflow
slot and reads `ok` while the file is intact.

`slot_health` maps every stored slot to its worst value across features. Unhealthy slots appear under `degraded`
(`slot`, `health`, `reason`, `fix`), are removed from `computed`, are listed in `pending` (so the user is prompted to
re-run them), and are not counted by `get_session_health.n_computed_slots` or the defensibility report
(`HydroSession.computed_healthy()`). Since 2.8.0 the other readers use it too: `get_session_raw_state` (slots and a `degraded`
list), `research.md` (a `Degraded (do not cite; re-run)` line instead of listing the slot as computed), the sync reminder,
`export_session` (`computed` is the healthy list, `degraded` is added; the capsule README and methods table mark a degraded
slot rather than omit it), project search and overview, and `bind_study`/`get_current_study`. Not changed on purpose: shard
merge (copies every slot) and `synopsis_for_llm` (no caller). Nothing is rewritten.
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
   - it is a comparison claim (`add_claim(comparison_ref="CAMELS-US v1.2")`: the reference the claim compares
     against) **and** a cited metric's CI declares `scope.excludes` containing `forcing_product`
     (`{"varies": [...], "assumes": [...], "excludes": [...]}`): `forcing_scope_gate`. A claim is a comparison only
     through its own `comparison_ref`; a run that merely carries a `reference_comparison` block (every signatures run
     does, from 2.8.0) does not make a plain claim citing it one; or
   - it is a comparison claim (`comparison_ref` set) and the `reference_comparison` entry for the cited metric says
     `can_claim_difference: false`: `comparison_gate`. A plain claim citing that metric ("mean flow was 2.0 m3/s") is not
     affected: the flag says a difference from the reference cannot be claimed, not that the value cannot be stated.

   `comparison_ref` is part of the sealed claim revision only when set.

   The rule reads scope and comparison metadata only: no tool or metric names, and no wording. Since 2.8.0 the producers emit
   both, and `tests/test_signatures_wave3_integration.py` runs the gate on real aihydro-watershed 0.2.0 output:

   - **Where it reads.** `reference_comparison` is read from exactly one place in the run row,
     `key_outputs.reference_comparison` (`{"table": {metric: {..., "can_claim_difference"}}, "context", "climate_context"}`).
     It is not searched for anywhere else. The interval scope is read from `evidence.uncertainty.<metric>.scope`; the run-row
     evidence keeps short string lists inside the uncertainty block (the scope lists), which earlier versions dropped.
   - **Which scope counts.** aihydro-core `scope_excludes(scope, "forcing_product")`: the forcing product is excluded unless the
     interval's scope lists it under `varies`. An interval whose scope is absent, free text or malformed therefore counts as
     excluding it (the 2.7.0 gate treated that as inert). A cited metric with no interval entry at all is not this gate's
     business: the uncertainty gate asks for the interval.
   - Every watershed interval declares `forcing_product` among its exclusions, so a *comparison* claim ("agrees with / differs
     from CAMELS") resting on any of them is refused; evidence whose scope varies the forcing product is the way through.

   Why: a CI that covers inter-annual sampling only is narrower than the forcing-product discrepancy, so "outside the CI" does
   not mean "different from the reference".

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

## Validators bound to the series a computation used (2.8.0)

`extract_hydrological_signatures` writes the daily series it consumed (`_served_series`: dates, m3/s, qualifier and approval
codes; about 7,300 entries x 4 lists) to a content-named session data file and removes it from the result before the result is
cached, written to the workspace or sealed. The file's digest is in `meta.retained_series`, the sealed record lists the file in
`retained_files`, and the series digest equals the result's `streamflow_digest` (aihydro-watershed's digest scheme, recomputed
by `series_binding.series_digest`). When the series came from the session `streamflow` slot, the slot's per-value USGS codes are
put on the retained series (only when the served dates are exactly the slot's dates). The slot's series is passed WITH its dates;
the slot drops days without a value, so earlier versions read its list as consecutive days and shifted later values onto the
wrong dates.

| validator | gains | notes |
|---|---|---|
| `check_record_length`, `check_usgs_qualification_codes`, `check_stationarity`, `check_regulated_basin` | `source="signatures"\|"streamflow"` (default `"streamflow"`: unchanged), `series_digest` | the series is resolved by digest; any failed check is `insufficient_data` with `metadata.series_mismatch: true`, never a verdict on another series |

Checks for `source="signatures"`: the retained file against the recorded file digest, the series against the result's
`streamflow_digest`, and the caller's `series_digest` when given (`source="streamflow"` checks the file digest when the slot records
one, and `series_digest`). A series that was never retained (a result from before 2.8.0 or a source that serves none) is
`series_not_retained` (unknown, not wrong). `check_regulated_basin` with `source="signatures"` applies only when the session's
delineated basin is the gauge the series describes. Standalone `source="signatures"` calls look up `session.get("signatures")`
(active feature, else legacy); a result stored under `__override__` (an explicit `geometry_geojson=`) is not found there.

The four validators run inside `extract_hydrological_signatures` on the in-memory series and are appended to `quality_flags`
(their `metadata.series` names the source, digests and period). Stationarity labels water years with aihydro-core `water_year`
(the ending year). Precipitation consumers key on `_precipitation.status` and reason codes: `used_with_gaps` is usable when its only
gap reason is `client_calendar_assumption` and no water year was excluded (`precipitation_usable`).

## CAMELS reference in the signatures output (2.8.0)

Every signatures result carries `camels_reference` (summary) and `reference_comparison` (aihydro-watershed's per-signature
table, see above):

- **Source.** The session `camels` slot (`fetch_camels_us`) when it holds this gauge; otherwise the packaged CAMELS attributes v2.0 tables
  in `ai_hydro/knowledge/reference/camels_us_v1_2` (hydro, topo for `area_gages2`, clim for `frac_snow`, `aridity`,
  `p_seasonality`; verbatim, CC-BY 4.0, `ATTRIBUTION.md`, provenance JSON; md5 re-checked before use). Not packaged: soil,
  vegetation and geology tables. `camels_reference.source` records which was used and its hash (packaged: sha256 over the file
  hashes, plus per-file md5/sha256; slot: sha256 of the attribute row), and `overrode` names the packaged table the slot replaced.
- **Block.** `available`, `period` (`used` read from the record, `ref`, `differs`), `area` (used, `area_gages2`, ratio),
  `forcing` (product, reference, the "GridMET vs Daymet" disclosure), `climate_context`, `can_claim_difference` per signature and
  the `blockers` behind each `false`. An unavailable reference says why (`available: false`) and never raises.
- A cached result keeps the reference it was computed with; `clear_session(["signatures"])` recomputes it after `fetch_camels_us`.

## Advisory: streamflow slot with missing days (tools 2.3.7-2.7.0)

Earlier versions read the streamflow slot's value list as consecutive days from `start_date`, but the slot drops days with no
value. In a run that consumed such a slot (same gauge and window, window containing null or missing days) every value after a gap
sat on an earlier date. Wrong: `runoff_ratio`, `stream_elas`, `hfd_mean`, `half_flow_date_std`, the water-year grouping,
`streamflow_end`, `streamflow_digest`. Unaffected: `q_mean`, `q5`, `q95`, `q_median`, `q_std`, `slope_fdc`, flow-frequency counts.
Detect an old sealed run by the slot's `missing_dates` / `n_missing_dates` > 0 together with `streamflow_n_days` below the days in the
window. From 2.8.0 the slot is passed with its dates, and a slot without usable dates raises the `streamflow_dates_unavailable`
quality flag instead of falling back silently.

## Gate scope (2.8.0 review)

The forcing-scope refusal applies where the forcing product matters: the run's `reference_comparison` entry for the metric has
`forcing_differs` other than false, or, with no entry, the metric's definition uses precipitation (`runoff_ratio`, `stream_elas`).
A comparison claim whose cited interval has no readable `scope` and for which forcing does not apply (for example a model-vs-model
NSE) is refused as `interval_scope_gate`: the interval's coverage is unknown. Standalone `source="signatures"` validators accept
`run_id` / `feature` / `series_digest`; with several stored signatures results and none of them the call is
`ambiguous_signatures_result` (teaching error, candidates listed).
