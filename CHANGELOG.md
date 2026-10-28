# Changelog — aihydro-tools

All notable changes to the `aihydro-tools` Python package are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [2.8.2] - 2026-10-28 (UX program Wave 0: study binding v2 and the study recorder; T-B1, T-B2)

Fixes the 2026-10-28 owner failure: a chat launched from the map called the read-only `measure_feature` and
ended in `SESSION_RESOLUTION_FAILED` with no recovery path. The recovery tools read no chat id and bound
nothing, the binding file was one-chat-per-study (a second chat stole the first's binding), and no tool
recorded which study owns which file. No tool is renamed; one optional argument is added (`include_hidden` on `aihydro_rebind_chat`). Read `docs/context.md`.

### Changed (behaviour)

- **The global "most recent study on disk" guess is OFF by default** (owner decision 2: it picked `test-*` and
  unrelated studies). `AIHYDRO_RECENT_SESSION_FALLBACK=on` (also `1`/`true`/`yes`) turns it back on for this release
  only; it stays off in an evaluation run. The guess no longer picks from hidden (test, archived) studies. The
  project default (T-P1) replaces it.
- **One study in the workspace resolves** (new rule `workspace_single_study`): exactly one visible study whose
  `workspace_dir` is the call's workspace. The result carries a `session_note`; a read-only tool does not bind the
  chat, a writer does. Closed to `audit_interpretation`, evaluation runs, and state-changing admin tools
  (`clear_session`, `archive_session`, `add_note`, ...), which need a named or bound study.
- **`SESSION_RESOLUTION_FAILED` lists candidates**: at most 5 `{study_id, label, last_run}` (the workspace's studies,
  else the most recent visible ones) and `next_tools` naming `aihydro_rebind_chat`.
- **`aihydro_rebind_chat(study_id)` and `aihydro_chat_status()` read the injected chat id** and actually bind. An explicit
  `chat_id` must equal it (`CHAT_ID_MISMATCH`); without a chat context it must be a ULID and not a study id. Rebinding to a
  study that does not exist no longer creates it: `STUDY_NOT_FOUND` with candidates. The `aihydro_chat_status` output keys are
  frozen by a contract test (`bound`, `study_id`, `chat_id`, `computed_slots`, `notes`, `message`; unbound: `bound`, `study_id`,
  `chat_id`, `message`); candidates for an unbound chat are named in `message`.
- **`start_session(session_id)` binds the chat** (not with `shard_id`, not in an evaluation run, not when `session_id` is omitted).
- **`active_workspace.json` is ignored when the call carries its own context** (`_meta` or the legacy hidden arguments). It was
  machine-global, written by the last-focused VS Code window.
- Results gain `session_binding` (`{chat_id, study_id, label, previous_study_id, bound_by}`) when the call changed a chat's
  binding (an auto-create on delineation, a rebind, an explicit `session_id` on a writer). Not added in evaluation runs.

- **Review fixes (same release).**
  - `aihydro_rebind_chat` gains `include_hidden` (default false). Archived and test studies are refused with `STUDY_HIDDEN`
    unless it is true; the tool is unavailable in evaluation runs (`NOT_AVAILABLE_IN_EVALUATION`).
  - An explicit `session_id` / `_meta.study_id` binds only a study that already exists. `start_session(session_id)` binds an
    existing study unless it belongs to a different workspace, and then returns `session_binding_skipped` with the reason, binds
    nothing and does NOT change the study's `workspace_dir` (re-homing is never a side effect). A new study binds only when the
    call carries a workspace. With no call context, `workspace_single_study` serves read-only calls only.
  - The binding file is written under a cross-process lock (`chat_bindings.v2.json.lock`) with temp + fsync + rename; reads
    never write. A corrupt file is quarantined as `chat_bindings.v2.json.corrupt-<ts>` with a warning and never overwritten.
  - Recorder: files over 64 MB are listed with `hash_deferred: true` and no `sha256`; shard writes are not recorded.

### Added

- **Binding store v2** (`session/chat_binding.py`): `chat_bindings.v2.json`, many chats to one study, no stored inverse, `bound_at`
  and `bound_by` per binding. Chat ids must be ULIDs and not equal a study id (`bind("basin_x", "basin_x")` raises). The v1 file
  `chat_studies.json` is imported once (original kept as `chat_studies.json.v1.bak`; entries that are not valid chat ids are
  skipped and counted), then never read. A derived v1-shaped projection (active studies only) is still written for one release
  so the installed extension's header chip keeps working. The pinned 2.3.7 server reads and writes only v1: give it its own
  `AIHYDRO_HOME` (OD-1), since the projection overwrites a shared v1 file.
- **Study recorder** (`session/artifact_recorder.py`, one call site in `run_records._record_call`): every file a tool writes
  through `HydroSession.write_workspace_file` and every layer it pushes through `map_events.push_*` is recorded in the owning
  study's `artifact_manifest` as `{artifact_id, kind, type, path, sha256, size_bytes, study_id, run_id, tool, recorded_at}`;
  `run_id` is the call's run row. Before this the manifest was empty in all 25 live studies.
- **Study labels** (`session/study_index.py`): user label, else site name plus gauge alias, else outlet coordinates, else
  "<id> (unnamed study)"; never a bare id.
- **Resources**: `aihydro://study/list{?workspace,include_hidden}` (labels, no full session loads; archived studies and
  `test-*` / `e2e-proof-*` studies with no workspace are hidden by default, never deleted),
  `aihydro://study/by-artifact{?path,workspace}` (rule `manifest`, or `artifact_name_heuristic` returning candidates only for
  files written before the recorder), and `aihydro://capabilities` (flags such as `binding.v2`, `study.by_artifact`;
  hosts detect features by flag, not by version).

### Tests

- New: `test_binding_store_v2.py`, `test_study_resolution.py`, `test_study_recorder.py`, `test_binding_acceptance.py` (the
  failure replayed: host binds, read-only `measure_feature` resolves by `chat_binding`; an unbound two-study folder returns
  labelled candidates and the recovery tool works; a one-study folder resolves).
- Updated for the new defaults: the recent-study guess is opt-in, chat ids are ULIDs, a study may have many chats.

## Also in 2.8.2 (Wave 0: context diet T-C1, annotation vocabulary T-A1; tools#13)

### Changed

- `list_available_tools` no longer dumps the registry. With no arguments it returns one deterministic page (sorted by name,
  name plus one-line summary) capped at about 4,000 characters (it was 135,896 characters on 162 tools; measured as the length of the text block of the `tools/call` result as the in-memory
  FastMCP client returns it, the same method before and after, in an environment without the private lsh/modelling plugins; other
  installs differ, e.g. 142,556 with more tools). New optional arguments:
  `domain=` (that domain's tools, same domains as `aihydro_describe_capability`), `name=` (one tool in full: description and
  parameters), `offset=` / `limit=` (paging; the answer carries `next_offset`, `None` on the last page). `n_tools` is now the total
  that matches, not the length of `tools`. The server instructions, the hot-mode note and the unknown-tool hint no longer send the
  agent to the full listing.
- `write_research_interpretation` no longer rewrites `.aihydrorules/research.md` (it is part of the agent's prompt; the rewrite
  busted the provider's prompt cache mid-task). The interpretation is stored in the session as before; `research.md` is regenerated at
  `start_session` (and archive, shard merge, export) only. `written_path` is now the session file, and `_note` says so.
  `_write_tools_md` leaves `tools.md` untouched when the content is identical.

- `start_session` returns the last 5 notes (each trimmed to 240 characters) plus `notes_total`, so a long study cannot push it past
  about 4,000 characters; `get_session_summary` still returns every note. `list_available_tools`: `limit` of 0, `None` or below 1 means no
  limit beyond the size cap (a negative `limit` used to mean 1); `offset` below 0 is 0.
- `map_add_layer_from_run` resolves its session file through the session store's path rule, so a `session_id` such as `../x` cannot
  read outside the sessions directory.

### Added

- Annotation vocabulary in `tool_annotations.toml` (data only, inert until a host reads it), served in `tools/list` `_meta`:
  `effect = "ui"` -> `aihydro/effect` (13 UI-only map and preview tools), `requiresConsent = true` -> `aihydro/requiresConsent`
  (`log_researcher_observation`, `update_researcher_profile`), `audience = "host"` -> `aihydro/audience` (mechanism only; no tool uses it
  yet). No `readOnlyHint`, `destructiveHint`, `idempotentHint` or `openWorldHint` value changed. The mechanism is generic
  (`tool_surface.META_KEYS`); no code branches on a tool name.
- Tests: `tests/test_context_diet.py` (cap, paging, `start_session` result size and no package map, `research.md` untouched);
  vocabulary, static and runtime `ui` checks in `tests/test_tool_surface.py`.

### Not done (follow-up)

- Hot-set trim (45 tools, 58.8k characters of schema and description) needs the A/B against task success; it is deliberately not
  done here.

---

## [2.8.1] - 2026-10-28

### Changed

- Reworded the slope_fdc definition provenance to describe the definition neutrally; values unchanged. The `slope_fdc` caution in
  `knowledge/camels_metadata.json` (both copies) and the reference ATTRIBUTION note now describe the definition only. (The earlier
  wording remains in git history; that is accepted.) The `signature_definitions` provenance strings are emitted by
  aihydro-watershed, whose neutral wording ships in 0.2.1, so the dependency floor is now `aihydro-watershed>=0.2.1,<0.3`.
- Corrected the label of the packaged/compared attribute file `camels_hydro.txt`: it is CAMELS attributes v2.0 ("CAMELS-US v1.2" is the time-series version). Wording only; values unchanged. The machine-readable `CAMELS-US v1.2` reference name (`camels_reference.REFERENCE_NAME`, `comparison_ref`) and the `reference/camels_us_v1_2` directory are unchanged.

## [2.8.0] - 2026-10-22 (Science Wave 3, PR-4b: series-bound validators, CAMELS reference, gates on real producer output)

Requires **aihydro-core >= 0.2.11, aihydro-data >= 0.2.5, aihydro-watershed >= 0.2.0** (pins `>=...,<0.3`). All three are on
PyPI (core 0.2.11, data 0.2.5, watershed 0.2.0; watershed#4 merged as 96feff2). CI installs them from PyPI through the pyproject pins;
`.github/ci/install-siblings.sh` (pinned git SHAs, added while they were unpublished) and its workflow steps are removed.
`pygeoglim`, which that script also installed, is not a dependency and nothing in the code or tests imports it.

### BREAKING-VALUE (inherited from aihydro-watershed 0.2.0; sessions that hold older results should be re-run)

- `slope_fdc` is the CAMELS attributes v2.0 file definition `(Q66 - Q33) / (mean(Q) * 0.33)` (01013500: 1.528, was 2.697); the old log-space
  Sawicz et al. (2011) value is `slope_fdc_sawicz2011`. `separate_baseflow` and `baseflow_index` use the Ladson et al. (2013) kernel
  (01031500: BFI 0.36 -> 0.445). Intervals are water-year grouped (`method: bootstrap_group`, no `block_size`; no interval below 10
  complete water years). `runoff_ratio` / `stream_elas` use complete water years only. Sealed records gain `slope_fdc_sawicz2011`,
  `signature_definitions`, `qualifier_exposure` (when watershed fetches the series itself) and `camels_reference` /
  `reference_comparison` (below). `knowledge/camels_metadata.json` (both copies) and `knowledge/tools/camels_tools.json` are updated
  to the file definition; the `todo` is gone.
- `separate_baseflow` on a series of 30 days or fewer is refused with a typed teaching error (`SERIES_TOO_SHORT_FOR_BASEFLOW`);
  31-364 days is computed and the result and the `baseflow` slot meta carry `warmup_bias` (watershed's `warmup_bias_disclosure`).
- The 4 validators below, and `extract_hydrological_signatures`, now return more `quality_flags`; `check_stationarity` labels water
  years by their ending year (aihydro-core `water_year`; it labelled by start year).

### Added

- **The series signatures consumed is retained and digest-bound.** `_served_series` (dates, m3/s, qualifier/approval codes) is written to
  a content-named session data file (`write_session_data_file`), recorded as `meta.retained_series = {path, digest}` with
  `data._data_file`, declared in the sealed record's `retained_files`, and removed from the result before caching, workspace write or
  sealing. The 2.7.0 slot-health check reads such a slot as `ok`. When the series came from the streamflow slot, the slot's per-value
  USGS codes are put on it (served dates must equal the slot's dates).
- **Validators resolve their series by digest.** `check_record_length`, `check_usgs_qualification_codes`, `check_stationarity` and
  `check_regulated_basin` gain `source="signatures"|"streamflow"` (default `"streamflow"`: unchanged) and `series_digest`. A retained
  file that changed, a series that is not the one the signatures recorded (`streamflow_digest`) or that the caller named returns
  `insufficient_data` with `series_mismatch`; a series that was never retained returns `series_not_retained`. They never validate a
  different series. They run automatically inside `extract_hydrological_signatures` (on the in-memory series) and are appended to
  `quality_flags`; the old post-run `check_stationarity` registration on the slot is removed.
- **`camels_reference` + `reference_comparison` in the signatures output.** aihydro-watershed's `reference_comparison` against the
  packaged CAMELS attributes v2.0 tables (hydro, `area_gages2`, `frac_snow`, `aridity`, `p_seasonality`; CC-BY 4.0, attribution, md5 and
  sha256 provenance JSON, md5 re-checked before use), overridden by the session `camels` slot (`fetch_camels_us`) when it holds the
  gauge. The block carries the source and its hash (S6), `period` used/ref/differs (the CAMELS signatures are WY1990-2009), area and
  rescaling, the "GridMET vs Daymet" forcing disclosure, climate context and `can_claim_difference` with blockers per signature. The
  table is at one place in the run row: `key_outputs.reference_comparison`. Not packaged (not available locally, not fetched): the
  soil, vegetation and geology tables.
- `tests/test_signatures_wave3_integration.py`: the recorded USGS body for 01013500 WY1990-2009 through the real fetch -> signatures
  path with aihydro-watershed 0.2.0 (precipitation synthetic). Plain claim accepted; `comparison_ref` claim on a forcing-excluded CI
  refused; `can_claim_difference=false` refused; a comparable metric without an interval passes the structural gates.

### Changed

- **Status gate.** `reference_comparison()` reads exactly `key_outputs.reference_comparison` (the depth-4 search is removed). The
  forcing test is aihydro-core's conservative `scope_excludes`: a cited interval whose scope does not vary `forcing_product`, absent
  or malformed scopes included, counts as excluding it (2.7.0 treated those as inert). A metric with no interval entry is left to the
  uncertainty gate.
- **Run-row evidence keeps interval scope.** Short lists of strings inside `evidence.uncertainty` survive compaction. Before, the
  scope lists were dropped from every real run row, so the 2.7.0 forcing gate could not fire on real output.
- **Precipitation consumers key on reason codes.** `check_water_balance_consistency` accepts `used_with_gaps` when its only gap
  reason is `client_calendar_assumption` and no water year was excluded (`precipitation_usable`).
- **Readers of `computed()` use slot health** (the follow-up named in 2.7.0): `get_session_raw_state` (healthy slots + `degraded`),
  `research.md`, the sync reminder, `export_session` (`computed` healthy, `degraded` added; capsule README and methods table mark a
  degraded slot), project search and overview, `bind_study` and `get_current_study`. Not changed: shard merge, `synopsis_for_llm`.
- **Capsule BFI** (`ai_hydro/capsule/data_artifacts.py`) is re-ported to the Ladson 2013 kernel (stdlib: 30-day reflection, 3 passes,
  runs of more than 30 days, dropped rows read as gaps via dates). Cross-checked against the watershed kernel to 1e-12 in
  `tests/test_camels_reference_pr4b.py`. When the signatures used another series than the exported slot (`streamflow_digest` differs)
  the check reports `not_comparable` instead of `differs`.
- `ai_hydro/analysis/signatures.py` no longer imports the private `_lyne_hollick_baseflow`; the kernel is the public
  `aihydro_watershed.signatures.baseflow.lyne_hollick_ladson2013`.
- `slope_fdc_sawicz2011` is a valid experiment metric and a metric-conflict signature.
- Bench: B-013 gains `slope_fdc_sawicz2011` in range and a rewritten rationale (slope_fdc 1.46, Sawicz 3.53 on the synthetic series);
  B-009's rationale is updated for the Ladson kernel (BFI 0.2915, same bounds). No assertion weakened.
- Tests updated for values that changed: `TestBaseflowFilter` (constant flow 0.986 not 1.0 because of the start-up transient, storm
  series 0.867 -> 0.8476, flashy 0.203 -> 0.2064, each cross-checked against the independent stdlib port), the capsule BFI oracle,
  precipitation `used` -> `used_with_gaps` where the window starts inside a water year, the experiment table window (12 water years so
  intervals exist), and the 2.7.0 scope-shape cases (absent or malformed scope now refuses a comparison claim).

### Fixed

- A streamflow slot with dropped days is passed to the signatures function with its dates (it was passed as a bare list, which the
  function reads as consecutive days from `start_date`, shifting every value after a gap onto an earlier date).
- Package version 2.8.0 (`pyproject.toml`, `ai_hydro/__version__`, `CITATION.cff`).

### Advisory: earlier versions mis-dated a streamflow slot with missing days (tools 2.3.7-2.7.0; earlier versions not checked)

- **Trigger.** `extract_hydrological_signatures` consuming the session `streamflow` slot (same gauge and window) when the
  window contains null or missing days. The slot drops days without a value, so its `q_cms` list is not consecutive from
  `start_date`; the list was read as consecutive daily values, which moved every value after a gap onto an earlier date.
- **Wrong in such runs:** `runoff_ratio`, `stream_elas`, `hfd_mean`, `half_flow_date_std`, the water-year grouping (and so the
  interval counts), `streamflow_end` and `streamflow_digest`.
- **Unaffected:** `q_mean`, `q5`, `q95`, `q_median`, `q_std`, `slope_fdc` and the flow-frequency counts (they do not use dates).
- **Detecting an old sealed run.** The slot's `missing_dates` / `n_missing_dates` is greater than 0 and the run's
  `streamflow_n_days` is less than the number of days in its window. Re-run those signatures on 2.8.0.
- **2.8.0.** The slot's series is passed with its dates. If the slot has no dates, or its dates and values differ in length,
  the call still runs on the bare list but now says so: a `streamflow_dates_unavailable` warning in `quality_flags` (never silent).

### Review fixes (on the same branch)

- Comparison gate no longer over-refuses. The forcing-scope refusal is skipped when the metric's comparison entry reports `forcing_differs: false`; with no entry it applies only to metrics whose definition uses precipitation (watershed's `FORCING_DEPENDENT`: `runoff_ratio`, `stream_elas`; fallback list kept in `status_gate`). A comparison on an interval with no readable scope where forcing does not apply (a model-vs-model NSE) is refused as `interval_scope_gate` (scope unknown), without talking about forcing. The "forcing-spread CI" advice now says no tool produces one yet.
- The workspace `signatures_<gauge>.json` carries `_data_file` as a `session-data:` ref, not an absolute path.
- Standalone `source="signatures"` validators take `run_id` / `feature`; `series_digest` also selects. With several stored signatures results and no selector the call returns `ambiguous_signatures_result` with a teaching error and the candidates.
- `evidence._compact`: the list branch uses the keepable-string-list guard (a long list uncertainty is dropped).
- Packaged reference: `.gitattributes` `ai_hydro/knowledge/reference/** -text`; sha256 of the three files pinned in a test; the Newman et al. data citation (2014) and paper (2015, HESS 19:209) are in `ATTRIBUTION.md`, copied verbatim from the Zenodo record's description.
- Output honesty: `qualifier_exposure_unavailable` (with reason) on the slot path; the "no codes" validator message states the real reason (`codes_reason` on the retained series); `session_slot_ignored` for a multi-gauge or empty `camels` slot.

### Follow-ups

- `separate_baseflow` and the FDC `seven_q10` splice across gaps (BFI effect about 0.002 on the test series).
- Slot consumption does not check the slot's `interval`.
- `ProjectSession.search` re-digests slot files on every call.
- `qualifier_exposure` is only produced when watershed fetches the series itself (it needs the flags frame); a series taken from the
  slot has none. Accepting flags with `q_cms_series` in watershed would close it.
- Standalone `source="signatures"` validators find the session's active/legacy signatures result, not one stored under `__override__`
  or another feature; `series_digest` guards against a wrong resolution.
- `fetch_camels_us` caches no hash of its own table; the slot source hash is of the attribute row used.

## [2.7.0] - 2026-10-22 (Science Wave 3, PR-4a: session artifact health, one status gate, metric conflicts)

No new package pins. Minor bump: additive response fields (`slot_health`, `degraded`, `claim_flags`,
`metric_conflicts`), a new skeptic check, and **new refusals** on claim status (below).

### Added

- **Session artifact health.** `HydroSession.summary()` (so `start_session` and `get_session_summary`) adds
  `slot_health` (`{slot: ok | missing_artifact | digest_mismatch | digest_missing | legacy_convention}`):
  - `missing_artifact`: the slot's `_data_file` is gone, unreadable, or outside the session/workspace data dirs
    (reason `outside_session_dir`; relative paths resolve against those dirs, symlinks are followed and must stay inside).
  - `digest_mismatch`: the file's digest differs from `meta.retained_series.digest`.
  - `digest_missing`: the slot declares `meta.retained_series` (or is the streamflow slot) with no digest, or a different
    path than `_data_file` (same rule as the consumer in `extract_hydrological_signatures`).
  - `legacy_convention` (signatures only): no `flow_quantile_convention`; or, from a producer new enough to emit
    `signature_definitions` (aihydro-watershed >= 0.2.0), no `_precipitation` report. A `completeness` of `None`
    (precipitation unavailable / not attempted / skipped) is legitimate. A fresh slot from watershed 0.1.x that has the
    convention key reads `ok`.
  Unhealthy slots are listed under `degraded` (`slot`, `health`, `reason`, `fix`), leave `computed`, **appear in `pending`**
  and are not counted by `n_computed_slots` or the defensibility report (`HydroSession.computed_healthy()`). Digests are
  hashed in chunks and cached by (path, size, mtime). Stored results are never rewritten. Other `computed()` readers
  (research.md, exports, tool listings) still see the raw list: follow-up.
- **One status gate** (`ai_hydro/claims/status_gate.py`, applied by `_status_gate` in `tools_ledger.py` to `add_claim` and
  `update_claim_status`). After the existing `uncertainty_gate`:
  - `evidence_gate`: `supported` and `weakly_supported` need at least one evidence span, and every span must be real. For
    `run` spans: non-empty `source_id`; the run exists in the session run log; it is not hollow (no `error`, not a
    `minimal` middleware row, non-empty `key_outputs`); a given `metric_ref` is among its outputs. `paper` and `dataset`
    spans: non-empty `source_id` only (nothing local to resolve them against at status time). Typed, traceback-free
    refusal with a `teaching_error` and `span_problems`.
  - `forcing_scope_gate` / `comparison_gate` (structural, keyed on scope metadata only, no tool or metric names): a
    `supported`/`weakly_supported` claim is refused when it is a comparison claim and a cited metric's CI declares `scope.excludes` containing `forcing_product`
    (documented shape `{"varies":[...],"assumes":[...],"excludes":[...]}`). A comparison claim is one that sets the new
    optional `add_claim(comparison_ref=...)` / `ScientificClaim.comparison_ref` (the reference compared against, e.g.
    "CAMELS-US v1.2"; sealed in the revision digest only when set). A run that merely carries a `reference_comparison`
    block does not make a plain claim citing it a comparison. A `reference_comparison` entry with
    `can_claim_difference: false` is honoured directly, **for comparison claims only** (a plain claim citing the same
    metric is unaffected). **The gate is inert until
    aihydro-watershed 0.2.0 / PR-4b supply the scope and `reference_comparison` outputs**; absent, free-text or malformed
    scopes never trigger it. A wording heuristic ("differs from the reference ...", negation-aware) only adds a
    non-blocking `FORCING_SCOPE_PROSE` warning.
  - Stored claims that break either rule are **flagged, never rewritten**: `start_session` returns them as `claim_flags`.
    A stored `supported` claim must gain real evidence (re-record it with `add_claim`) or move to another status before
    `update_claim_status` can save it again as `supported`, even to change only its confidence.
- **Skeptic check 5, `metric_conflicts`.** Same signature metric (allowlist: the hydrologic signature set; aliases resolved,
  e.g. `bfi` = `baseflow_index`), same feature, same *known* period (`start_date`/`end_date`/`years`/`water_year`), different
  values (relative difference above 1%, an engineering threshold), latest run per (tool, method/model, container). Model
  skill (`nse`, `kge`, ...) and generic keys are never compared; runs with an unknown period are not flagged; only top-level
  outputs and direct children of container dicts (`signatures`, `data`, ...) count, so `obs`/`sim` outputs of one run never
  collide. Reported by `run_skeptic` (`metric_conflict`, warning) and by `start_session` as `metric_conflicts`. It stays for
  legacy sessions after the BFI kernel is fixed upstream.

### Fixed

- `knowledge/concepts/camels_metadata.json`, `ai_hydro/knowledge/camels_metadata.json` and `knowledge/tools/camels_tools.json`:
  `high_q_freq` and `low_q_freq` are days per year, not events per year; `zero_q_freq` is the fraction of days with Q <= 0.
  The `slope_fdc` entry is untouched apart from a `todo` pointing at PR-4b / the Wave 3 plan.

### Changed

- **Behaviour change:** `add_claim` / `update_claim_status` now refuse `supported` and `weakly_supported` without real
  evidence (previously accepted anything). Bench tasks B-034 and B-059 and the test modules that built such claims now
  create and cite real runs; defensibility fixtures use current-convention signature slots.
- Follow-up (PR-4b): an integration test of the gates against real aihydro-watershed 0.2.0 `reference_comparison` and CI
  `scope` output; until then they are tested with fixtures of the documented shape only.
- Package version 2.7.0 (`pyproject.toml`, `ai_hydro/__version__`, `CITATION.cff`).
- **A tool with no `tool_annotations.toml` row is no longer served unannotated.** `apply_tool_annotations`
  now stamps explicit conservative hints (`readOnlyHint=false`, `destructiveHint=true`,
  `idempotentHint=false`, `openWorldHint=true`) on any registered tool without a row (a third-party
  plugin tool, or a tool added without its row), keeping any hint the tool declares itself. Hosts that
  fold read-only tool activity therefore never see such a tool as "unknown", and it is never treated as
  read-only. First-party tools are unchanged: all 173 (154 builtin, 11 `aihydro-lsh`, 8 `aihydro-data`)
  already have rows; 76 are `readOnlyHint=true`. New tests: every served tool has explicit
  `readOnlyHint`/`destructiveHint`, the unrowed fallback, and representative tools of each class.

---

## [2.6.0] - 2026-10-21 (Wave V: one AIHYDRO_HOME, provenance/units, token-typed auditor, NumPy 2.5)

Requires `aihydro-core>=0.2.10`, `aihydro-data>=0.2.4`, `aihydro-watershed>=0.1.6`; the `modelling` extra requires
`aihydro-modelling>=0.6.1,<0.7`. Minor bump: new behaviour (AIHYDRO_HOME everywhere, reuse run records, auditor
violation kinds) with additive response fields.

### Fixed (NumPy 2.5 / pysheds `np.in1d`)

- `ai_hydro/analysis/inundation.py` and `inundation_spike.py` import pysheds directly and call `accumulation`
  (an `np.in1d` site, missing from NumPy 2.4+). Both now call `aihydro_watershed.ensure_numpy_compat()` after the
  import; the spike's local `np.in1d = np.isin` alias (non-flat) is replaced by it. Requires
  `aihydro-watershed>=0.1.6` (pin raised).

### Fixed (session study 01031500: provenance, units, schema hygiene)

Requires `aihydro-watershed` with the signature `units` map (see its changelog).

- **S1 units.** `extract_hydrological_signatures` passes through the `units` map that
  `aihydro-watershed` now emits for every signature (`q_mean`, `q_std`, `q5`, `q95`, `q_median` are
  **mm/day**, not m3/s; `high/low_q_freq` days/year; `hfd_mean` day of water year; ...). It rides in the
  tool response and in the run log row's `key_outputs.units` and `evidence.data.units`.
- **S2 reuse records.** A tool that answers from the session cache (`_cached` / `_cache_hit`, found
  generically in the recording middleware, no per-tool code) now gets a first-class *reuse* run record
  instead of an empty minimal row: it copies the original run's `key_outputs`/`evidence`, carries
  `derived_from` (also a sealed `parents` edge), the ORIGINAL `computed_at` and `reused: true`. The response
  gets `reused`, `reused_from`, `computed_at`, its `_run_id` becomes the reuse row, and `_note` starts with
  "REUSED, NOT RECOMPUTED ... Disclose the reuse". The original is found by the id the result names, else by
  `meta.computed_at`, else the latest row of the tool; an unresolvable origin is disclosed as such.
  A cached `delineate_watershed` slot without a `basin_ref` returns `basin_ref_missing` and says to
  `clear_session(['watershed'])` first; the `BASIN_REF_REQUIRED` / `BASIN_REF_UNKNOWN` recoveries say the same,
  so the agent no longer loops on a slot that can never bind. (Identity is not back-filled from a legacy label.)
- **Reuse origin must agree with the response (review B1).** A `computed_at` / latest-row candidate is linked only
  when its identity agrees with the cached response (shared `basin_ref` / `gauge_id` / `feature_id` / index equal;
  with no shared identity, every shared scalar equal and at least one), so an NDVI cache hit can no longer inherit the
  NDWI row. Otherwise the origin is `unresolved` and disclosed as such (the note no longer says "copied from the
  original"). The response exposes `origin_match` (`run_id` | `computed_at` | `latest_by_tool` | `unresolved`);
  an inferred (`latest_by_tool`) origin says so in `_note`.
- **Cache vs request is refused, not served.** `delineate_watershed` with a cached watershed of another gauge
  (ids normalised: `USGS-` stripped, zero-padded to 8) returns `CACHED_WATERSHED_GAUGE_MISMATCH` with no slot data;
  `compute_spectral_index` with a cached index computed for another sensor/dates (the request is now stored with the
  result) returns `CACHED_INDEX_PARAMS_MISMATCH`. Neither seals a reuse row. `clear_session` now accepts
  `index_<name>` slots so the recovery works.
- **Tighter identity rule.** If either side carries identity fields they must match exactly (a field on one side
  only is unresolved, never a fallback to value matching); with no identity on either side at least two equal
  scalars that are evidence (not bool, 0, None, empty) are required and none may differ.
- `UNCERTAINTY_ASSERTED_NOT_EVIDENCED` is a heuristic (word match, not meaning): it ignores negated clauses
  ("no confidence interval was computed", "not quantified") and also recognises standard error, sigma and
  "95% interval". Warning only.
- `skeptic` hint no longer suggests the unaccepted `claim_type='assumption'`. `ErrorHygieneMiddleware` logs the
  dropped traceback at debug level. Pin: `aihydro-watershed>=0.1.6,<0.2`.
- **One run id per execution.** A tool that stores its result in a slot and then calls `post_run` used to
  record two rows (`signatures.<hash>` from the slot write and `sigs.<date>...` from `post_run`); the response
  cited one and the slot named the other. `post_run` now adopts the slot row written during the same recorded
  call and merges its fields into it, so `_run_id`, the slot's `meta.run_id` and the run log agree.
  Outside a recorded call nothing changes.
- **S3 ledger.** `add_claim` returns a non-blocking `warnings[]` (`UNCERTAINTY_ASSERTED_NOT_EVIDENCED`, plus a
  `_note`) when the statement or confidence rationale mentions CIs / bootstrap / uncertainty but a cited
  run-backed `metric_ref` has no entry in that run's `_uncertainty`. `claim_type`, `status` and `confidence`
  of `add_claim`, and `status`/`confidence` of `update_claim_status`, are `Literal` enums, so the JSON schema
  lists the allowed values (a guessed `claim_type` is refused by the schema, not by a traceback).
- **Error hygiene.** `_traceback` is gone from `UNEXPECTED_ERROR` envelopes (the traceback is logged
  server-side), and absolute local paths in error text are scrubbed. A new `ErrorHygieneMiddleware`
  (inside `RunRecordMiddleware`) applies the same rule to error dicts a tool built itself, before sealing.
- **M3 research.md.** `HydroSession.save()` no longer regenerates `.aihydrorules/research.md`. That file is
  part of the agent's system prompt: rewriting it on every save changes the agent's instructions mid-task and
  busts the provider's prompt cache. It is written by `save(sync_context=True)` at `start_session`, `archive`,
  `write_research_interpretation`, shard merge, and by `export_session`. (The project-level digest in
  `session/project.py` is unchanged.)
- **M4 payloads.** `start_session` no longer returns `available_packages` (~12k characters, and a slow
  `pip list`) unless `include_packages=True`. `fetch_camels_us` returns each attribute once (flat
  `attributes`, plus `attribute_group_index` = group -> names; the duplicated `attribute_groups` is gone), and
  a cached single-gauge answer is decided before the 671-gauge dataset is loaded (it took 29.6 s) and only for
  the same gauge.

### Changed

- **Answer auditor: token-typed number grammar, citation resolution and severities (session study
  S4).** The 01031500 session answer produced 29 "numbers", 8 "cited" (all through a +/-60-character
  year whitelist), 21 violations, 0 of 5 markers resolved, the gauge id flagged 4 times, and the one
  real sample size (7,305) excused by a nearby "1989". The auditor now tokenises before matching:
  digits in code spans, URLs, file paths, marker brackets and identifier-shaped tokens (run/claim ids,
  versions, `EPSG:5070`) are not numbers; the number regex no longer splits `.` `-` `:` `_` neighbours.
  Each remaining number is classified: exempt (session/gauge ids and 8-15-digit ids near
  USGS/gauge/COMID/HRU, HUC/EPSG/FIPS/NLCD codes, year/date tokens, `Figure N`, list indices,
  meta-counts such as "17 signatures", bare integers with no unit), **sample sizes** ("7,305 daily
  observations") that must be cited, **method parameters** ("bootstrap 90% CIs") that only draw an
  advisory, and **quantities** (a decimal, a unit, or a metric name nearby) that must be cited. A
  marker binds only inside its own sentence / bullet / table cell. `data.X`, `key_outputs.X` and
  `evidence.data.X` resolve to the same stored field (canonical: `key_outputs.<field>`); the grammar
  shown to the model in `research.md` now says so. New violation kinds `hollow_evidence` (marker ->
  run with no recorded outputs; suggests the `derived_from` run) and `unit_mismatch` (prose unit
  differs from the run's `units` map). Every violation carries `severity` (`hard` | `advisory`),
  `category`, `suggested_marker`; reports are sorted hard-first, `passed` means no hard violation,
  and `hard_count` / `advisory_count` / `exempt_by_reason` are added (fields only added, none renamed).
  Behaviour change: a percent now only matches a stored fraction at the scaled tolerance ("5%" no longer
  matches 0.5). Regression: `tests/test_audit_grammar.py` + `tests/fixtures/audit_session_01031500/`.

### Fixed

- **Multi-basin experiments no longer return the session's basin for every row (WP-1a, S1).**
  `run_experiment` over several gauges used to fall back to `"__legacy__"` for any id that was not a
  registered feature, so every row shared one cache key and the second and later rows were the first
  row's numbers; even for a registered feature the gauge (`session.site_id`) and the area
  (`session.watershed`) came from the session. Now: an unregistered id fails closed with
  `FEATURE_NOT_RESOLVED` (per feature in an experiment, and in `extract_hydrological_signatures` /
  `extract_geomorphic_parameters` when `feature=` is passed); a registered feature needs
  `gauge_id` (new optional argument of `register_feature`, with `area_km2`, `outlet_lat`,
  `outlet_lon`) or the tool returns `FEATURE_GAUGE_UNKNOWN`; the session streamflow slot is consumed
  only when it is that feature's own gauge and period; the area is the feature's (registered
  `area_km2`, else geodesic from its polygon); the cache key now includes the gauge (and, for a
  registered feature, its geometry and area). `basin_gauge_id` / `basin_area_km2` are sealed in the
  result. Per-feature workspace files (`signatures_<canonical>_<feature>.json`) replace one shared
  name, and no FDC plot is drawn from the session's series for a feature.
- **Experiment table is complete and honest.** Default signature metrics were `fdc_slope` and
  `streamflow_elasticity`, which the tool never emits (real keys `slope_fdc`, `stream_elas`); the
  geomorphic defaults were all invalid (real keys `DA_km2`, `Re`, `H_m`, ...). Defaults now come from
  the real output keys and `define_experiment` rejects a metric the tool cannot emit. Every defined
  feature is a row with a `status` (`ok`, `error`, `no_metrics`, `no_run_id`, `not_run`) and a `reason`;
  a feature that matched no metric is no longer dropped. Each row carries its own `run_id` and
  `run_id_origin` (`fresh` or `cache`); cache hits name the run that produced them (the run id is now
  persisted on the cached entry, and `source_run_id` is returned on a hit). Null metrics list the
  reason in `missing_metrics`. Rows without a run id are excluded from `aggregate_stats`.
  `extract_geomorphic_parameters` now records a run id (it did not call `post_run`).
- **`separate_baseflow` is no longer a supported experiment tool.** It has no `feature` argument, reads
  the session's single streamflow slot and returns `bfi`, so every row errored. `define_experiment`
  refuses it and points to `baseflow_index` from `extract_hydrological_signatures`.
- **`session/list`, `session/{id}` and `session/{id}/experiments` MCP resources** counted the
  `{data, meta}` envelope (always 2) instead of the experiments, and sorted on a key that does not exist.
- **Retained streamflow files are content-addressed and never overwritten (H0).**
  `persist_streamflow_slot` wrote `streamflow_<gauge>.json` into the shared workspace with a plain
  overwrite, so a re-fetch (any window, any session in the folder) rewrote a file an earlier sealed run
  had digest-bound, and capsule export then reported that run's binding as "differs". The file is now
  `streamflow_<gauge>_<sha256[:12]>.json` (full digest on a prefix collision); an existing file is
  never replaced and identical content reuses the same file. Records and slots written before this
  change keep working: consumers read by recorded path and digest, not by name. The capsule copy is
  named the same way (`data/streamflow_<gauge>_<sha12>.json`).

- **Modelling jobs never use the session id as a gauge.** `search_runner` read a non-existent
  `session.usgs_gauge_id` and fell back to the session id; `modelling/runner` used
  `session.site_id or session_id`. Both now use `site_id` only; an empty one is `GAUGE_NOT_SET`
  (`ai_hydro/modelling/gauge.py`). `train_hydro_model`, `propose_and_train` and `run_autoresearch`
  refuse with that error before spawning a job.
- **`get_leaderboard` serves the episode facts.** New `episode` key from
  `aihydro_modelling.autoresearch_summary` (0.6.1): `n_trials`, `promotions[*]` (`ci_excludes_zero`,
  `p_adjusted_holm`, `significant_after_multiplicity`), `seeds_recorded`, `prereg`. No score or grade is
  returned. No other tools consumer of the old score/grade exists. Pin raised to
  `aihydro-modelling>=0.6.1,<0.7`.

- **Experiments reject basin-selecting params (review B1).** `geometry_geojson`, `feature` and
  `session_id` in an experiment's shared `params` are refused by `define_experiment` (and by
  `run_experiment` for definitions stored earlier): `geometry_geojson` overrode every feature's polygon,
  so each row held the session basin's numbers under its own gauge's label.
- **The active feature is scoped like an explicit one (review C1).** With `feature=None` and an active
  registered feature, `extract_hydrological_signatures` / `extract_geomorphic_parameters` now use that
  feature's gauge, area, outlet and slot rules (or return `FEATURE_GAUGE_UNKNOWN`); they no longer file the
  session gauge's numbers under the active feature's id. With no registered feature the session-basin
  behaviour is unchanged.
- **`get_leaderboard` on aihydro-modelling < 0.6.1** omits `episode` and returns a `version_note`
  instead of presenting the old structure as facts. **Release note:** aihydro-modelling 0.6.1 must be
  uploaded to PyPI before this aihydro-tools release (the pin is `>=0.6.1,<0.7`).
- `get_experiment_table` returns a `units` map; `run_experiment` no longer counts `no_run_id` rows in
  `n_success` (new `n_no_run_id`).

### Added

- **`aihydro-approve` refuses in an agent context (exit 6).** `approve` and `enroll --user-trust` refuse,
  before the TTY check, when `AIHYDRO_AGENT_CONTEXT` is truthy or `AIHYDRO_ACTIVE` is present (the
  extension's agent terminals set it). `revoke` and plain `enroll` stay allowed. There is no override flag
  or env var. This is a speed bump, not a boundary (same-user code can unset the variables); the real
  boundary is a touch-required `sk-` key. `python -m ai_hydro.approval` is not an entry point (no
  `__main__.py`; the console script is `aihydro-approve`). See DECISIONS.md 2026-10-19.
- **One home: `AIHYDRO_HOME` is honoured end to end (WP-0).** Only approvals and the claim registry used
  to follow `AIHYDRO_HOME`; sessions (and their run-log and claim-revision SQLite files), ledger events,
  caches, data jobs, map and preview bridges, comments, chat studies, citations, skills, the knowledge
  index, projects, persona, exports, course state and the model/inundation output fallbacks were fixed at
  `~/.aihydro`. Every one now goes through `ai_hydro/registry/paths.py`: `aihydro_home()` for inline
  paths and `home_path(...)` for module-level constants (`store.SESSIONS_DIR`/`_SESSIONS_DIR`,
  `ledger_commands._LEDGER_EVENTS_DIR`, ...), both evaluated at call time; an unset variable still means
  `~/.aihydro`. A test that assigns a plain `Path` to such a constant still overrides it.
- `mcp_server.py` and `setup_mcp.py` take the cache/working directory from the same resolver. `aihydro_home()` returns an absolute path, and `pin_aihydro_home()` rewrites a relative `AIHYDRO_HOME` in the environment before the server `chdir`s into the cache directory, so the home cannot move after startup.
- Not covered here: `aihydro-core` (`jobs`: `~/.aihydro/jobs`, `subagents`), `aihydro-data` (disk cache,
  `cds_glofas`/`geoglows_retro` fallbacks) and `aihydro-watershed` (`delineation` cache, `merit`
  manager and `config.json`) keep their own defaults; only `identity.py` there reads `AIHYDRO_HOME`.
- `tests/test_aihydro_home.py`: a guard that fails if any module outside the resolver spells the
  `.aihydro` directory, and tests that put sessions, the run log, ledger events, approval-store reads and
  the other state directories under a scratch `AIHYDRO_HOME` (with `HOME` also scratch, so a leftover
  hard-code is caught).

---

## [2.5.0] - 2026-10-18 (Wave U, U5a tool surface)

No tool is added or removed (U5b, retiring `fetch_streamflow_data` / `fetch_forcing_data`, is not done:
see DECISIONS.md 2026-10-18). HRB fixture bench: 78/78 passed at 2.4.1 and at 2.5.0 (no delta).

### Added

- **MCP tool annotations.** `ai_hydro/mcp/tool_annotations.toml` has one row per registered tool
  (173, including the `aihydro-data` and `aihydro-lsh` entry-point tools) with `readOnlyHint`,
  `destructiveHint`, `idempotentHint`, `openWorldHint` and an `evidence` column (a grouped
  justification plus the guard result for the tool). `ai_hydro/mcp/tool_surface.py` stamps them after
  registration and they are served in `tools/list`. "Read-only" means no session or state mutation; the call may still append sealed run-log rows, and the
  series tools mint citable `_run_id`s. The classification was made by reading the code;
  it is checked, not proved: a static deny-list scan (`tests/test_tool_surface.py`) and a runtime
  `sys.addaudithook` guard that calls every read-only tool against a fixture session
  (`tests/test_readonly_runtime_guard.py`), plus a mutation test that relabels all 97 non-read-only
  tools as read-only and requires a guard to flag each. A read-only call may append run records (the
  middleware's sealed record, `post_run`'s run-log row) and create an empty cache directory, nothing
  else. Rules and limits: `docs/tool-surface.md`.
- **`AIHYDRO_TOOL_SURFACE=full|hot`** (default `full`; always `full` in an evaluation run, i.e. when
  `AIHYDRO_EVAL_NONCE` is set). `hot` makes `tools/list` return the hot tools (every Tier 1 tool plus
  `HOT_TOOL_ALLOWLIST`, which includes the discovery tools) and nothing else, for hosts that, unlike
  the VS Code extension, show whatever `tools/list` returns. Other tools stay callable by name and
  are found through `aihydro_describe_capability`, `list_available_tools` and `describe_tool`.
- **`session_warning`.** A result whose session came from the recent-study guess carries a top-level
  `session_warning` ("No study was named or bound; used most-recent study <id> ..." for a read-only tool, "wrote to" for a writer) in addition
  to `extra.session_resolution = "recent_fallback"`.

### Changed

- **Read-only calls resolve their session without side effects.** For a tool whose annotation row has
  `readOnlyHint = true`, `_resolve_session` no longer rebinds the chat to the session it read and no
  longer backfills `workspace_dir` (a save that bumped the session's mtime and so fed the
  recent-study guess). `get_model_results` now echoes the `session_id` it read, like `list_features`.
- **Recent-study fallback.** `AIHYDRO_RECENT_SESSION_FALLBACK` accepts `off`/`0`/`false`/`no`, and the
  fallback is always off in an evaluation run (`AIHYDRO_EVAL_NONCE`, via `in_evaluation_run()`). Default stays on for interactive use.
  `audit_interpretation` also passes `allow_recent_fallback=False`: hardening only, it already
  refused to guess (`allow_auto_create=False`).
- `list_cached_citations` no longer creates its cache directory; `get_data_fetch_result` rejects a
  `job_id` with path separators or `..`.
- `draft_claim_from_run` and `preview_get_pending_changes` are annotated read-only (they only read).
- `tools/list` is sorted by tool name, so the order no longer depends on import or entry-point
  iteration order.
- Nine tool descriptions over 1500 characters were tightened to at most 1500 (`add_claim`,
  `compute_spectral_index`, `data_fetch`, `create_cn_grid`, `map_flood_inundation`,
  `run_autoresearch`, `show_on_map`, `fetch_forcing_data`, `export_session`); parameters, error codes
  and return keys are kept. Total description size 90,057 -> 83,938 characters.

## [2.4.1] - 2026-10-17 (Wave U patch, proof 3 findings E6/E7)

### Changed (precedence; see DECISIONS.md 2026-10-17)

- **`RunRecord.tool_version` is now always `ai_hydro.__version__`** (`version_source = "package"`), no longer
  `meta.version` ("result_meta"). Distribution metadata is a last-resort fallback only. The result's
  `meta.tool` / `meta.version` move to `record.extra.producer` / `extra.producer_version`. Records sealed
  before 2.4.1 keep their old semantics; `verify` and replay check seals and digests only and are unaffected.

### Fixed

- A record's version is the running code's, never `dist-info`. `streamflow`, `tools_docs`, `mcp.app`,
  `mcp.__main__` and the environment.yml pin list (`tools_session`) use `ai_hydro.__version__`.
  `run_records.version_check` records, in `extra.version_check` with `version_mismatch: true`, a stale
  `aihydro-tools` dist-info and any producer `meta.version` that disagrees with its imported module.
  The environment digest still lists installed distribution versions, by design (it describes the environment).
- Floors: `aihydro-data>=0.2.4` (E6 content digests in upstream provenance), `aihydro-watershed>=0.1.4` (E7).

## [2.4.0] - 2026-10-17 (Wave U)

- **USGS Water Data API consumers (Wave U, U3).** Requires aihydro-data >= 0.2.3 and
  aihydro-watershed >= 0.1.3; `hydrofunctions` is no longer a dependency.
  - `ai_hydro/data/streamflow.py` routes to `aihydro_data.sources.usgs_waterdata`; its own NWIS
    fetcher, site-info lookup and ft3/s constant are gone (the adapter converts once, exact
    0.028316846592; values shift by <= 1.6e-6 relative from the old 0.0283168). A refusal is a
    `ToolError` with `details["terminal"]` (True for GAUGE_NOT_FOUND, USGS_NO_DATA, ...).
  - `fetch_streamflow_data` has one fetcher and no fallback; `_legacy_data_shim` re-raises
    `TerminalSourceError` instead of returning `(None, err)`, and the capsule re-query is pinned
    to `NWIS_STREAMFLOW` and records terminal refusal vs transient failure.
  - `data_fetch(variable="streamflow", session_id=...)` writes the session `streamflow` slot
    (single writer, `mcp/streamflow_slot.py`): per-value `qualification_codes` (A/P/e or raw
    qualifier tokens), `qualifiers_other`, `quality_summary` (n_negative, n_missing,
    provisional_fraction), `site_resolution`, `upstream`, retained-file digest. The streamflow
    validators now fire on this path. New `gauge_id` argument on `data_fetch` and
    `data_fetch_background`; a session BasinRef with verified `usgs:` aliases for the same
    geometry is passed as `basin_ref`. The A2 thresholds are not exposed.
  - `check_usgs_qualification_codes` reads the counts; qualifiers outside A/P/e are a warning with
    their raw values, never "unreadable". Before: always `insufficient_data` on the data_fetch path.
  - Signatures: `_streamflow_source` (observation, product, site, quality summary, upstream and
    any refusal code/message/terminal) is mirrored into non-underscore `streamflow_*` fields so
    it reaches the sealed row and the run-log key_outputs.
  - Gauge search/in-view layers use `get_monitoring_locations` (id, bbox, EXACT name; partial name
    is matched client-side inside the map bbox only). Library cards: `usgs_waterdata`,
    `dataretrieval_waterdata`; NWIS removed from `pygeohydro`; `hydrofunctions` retired.
  - **U3 review fixes.**
    - A named gauge (`gauge_id`, or a session BasinRef with verified `usgs:` aliases) is a strict
      observation request: `data_fetch` / `data_fetch_background` call aihydro-data pinned to
      `NWIS_STREAMFLOW` with `fallback=[]`, so a 429 is a transient error and never modelled flow.
      A conflicting `product` or a non-CONUS geometry is refused (`GAUGE_ID_NOT_APPLICABLE`).
      Refusal envelopes carry `terminal`. The session BasinRef is reused only when it verifies
      and the geometry is the session watershed (digest equality, else IoU >= 0.99 with areas
      within 1 %); a near miss declines with `basin_ref_hint`.
    - `fetch_streamflow_data` reuses its cached slot only for the same gauge, period, interval
      and backend. It honours `AIHYDRO_USGS_BACKEND=legacy_waterservices` through the data
      path's own function (daily only).
    - Both slot writers drop null-value rows and record them (`n_null_values`, null/missing
      dates). Session meta holds counts and run-length-encoded codes (`qualification_codes_rle`);
      the per-value list lives in the digest-bound retained file.
    - `data_fetch(cache=False)` returns `validators_not_run` instead of an empty `quality_flags`.
    - Gauge search / in-view layers report an outage or an oversized view (`problems`) apart from
      "no gauges". Removed dead `ai_hydro/data/fetch_runner.py` and `async_dispatch.py`.
    - Evaluation arm C1 strips advisory/gate output only (`validators_not_run` joins
      `quality_flags`, `promotion_check`, `next_steps`, `skeptic*`); a data tool's provenance about
      its own data (quality/approval/qualifier counts, site_resolution, upstream, streamflow_*
      mirrors, area-check values) stays visible in every arm (DECISIONS.md).
    - `map_set_time_range` accepts strict ISO-8601 only (date, or date-time with Z/offset), with
      end >= start.
  - Behaviour change: `fetch_streamflow_data` slot `data.units` is `m3/s` (was `m^3/s` on the
    direct path, which the unit validator flagged).

- **Error contract (Wave U, U4).** A failed tool call is an MCP error (`isError` true) whose
  text is the JSON envelope.
  - `ArgRepairMiddleware` raises `StructuredToolError(help_payload)`; the fastmcp-4-fragile
    `ToolResult` import is gone.
  - New `ErrorContractMiddleware` (innermost) turns any result dict with `error is True` into
    `StructuredToolError(envelope)`. One rule, no per-tool carve-outs.
  - `RunRecordMiddleware` seals `failure.envelope`: the output digest of a dict-error call is
    identical to 2.3.7 and status stays `error`. No `_run_id` is injected on an error.
  - Any `ToolError` whose text is a JSON dict with `error` true and a `code` counts as an
    envelope (`errors.envelope_from_exception`), for arg-repair, the run record and the eval
    layer alike. aihydro-data's queued redirect is one, so its sealed row is the envelope
    (digest = digest(envelope), `error_summary` = `USE_ASYNC_TOOL`).
  - Eval arm behaviour change: C1 now strips advisory fields (`quality_flags`,
    `promotion_check`, `next_steps`, `skeptic*`) from every structured error, not only from
    returned dicts. An error dict that names a hidden tool now reaches the client as the
    envelope with the name replaced by "[tool unavailable in this configuration]", instead of
    a plain "Unknown tool" refusal.
  - Failed calls seal with the envelope's byte size, so `MAX_DIGEST_BYTES` still applies to
    large error bodies. A `_record_error` on a failed call is logged, not injected (an error
    call has no result to carry it).

## [2.3.7] - 2026-10-16

It requires aihydro-core 0.2.10 or later.

- **Approvals.** Each approval is verified against the revision it
  signed. A later revision with a different digest makes it `superseded`
  (reported as a NOTE).
- **Exporter.** It refuses the crate when a carried approval fails.
- **Partial outcomes.** "Partial" is derived from content:
  - privacy stubs give `withheld`;
  - any unsealed run-log row gives `unsealed`.
  - **Behaviour change:** a capsule with any unsealed legacy row now
    exits 3.
- **VER-HEAD.** It covers deleted projected keys.
- **Registry staleness.** `check_registry_staleness` reports
  `claim_revised_since_promotion`.

Requires aihydro-core 0.2.10 or later.

### Fixed
- Approval currency (T-M1): an approval is *current* when the head revision's digest equals the digest it signed (promotion
  appends a bookkeeping revision with the same digest, so a promoted claim is not "superseded"); the stronger recomputation
  from the current claim then runs. It is reported `superseded` only when a LATER revision with a DIFFERENT digest exists, and
  the note names that revision. For an approval written before `evidence_seals` existed the superseded branch re-derives the
  evidence fingerprints against the run log. A bound run row whose digest differs is reported as "present but its record digest
  (or fingerprint) differs from the one the approval bound", distinct from "not in this capsule".
- `check_registry_staleness` returns `claim_revised_since_promotion` (informational, not stale evidence): promoted claims whose
  latest revision digest is no longer the registry row's.

### Changed
- (core C-S2) any unsealed run-log row makes the result partial with reason `unsealed`, whatever else verified; plain `ok`
  needs every run-log row to be sealed or a counted privacy stub. A session whose run log still holds unsealed legacy rows
  therefore exits 3 under `replay.py`.
- Partial results derive from content (core C-S1): any run-log row with the privacy-stub shape makes the result partial
  (`withheld`) whatever the bundle declares, and `VER-RUN-ROWS` fails if `run_rows.withheld_for_privacy` undercounts them.
  A capsule where nothing was verified (`records_verified == 0` and some non-stub row has no sealed record, or
  `run_rows.legacy_no_record > 0`) is partial with reason `unsealed`: outcome `ok_partial_unsealed`. Both partial outcomes
  exit 3; `replay.py` prints `partial_reasons: ...` and `replay_outcome: ...`, `export_session` returns
  `crate_status` (`ok` / `ok_partial_withheld` / `ok_partial_unsealed`), `crate_partial` and `crate_partial_reasons`.
  **Behaviour change:** an export of a session with no sealed run records now exits 3 under `replay.py` (it was 0).
- A chained claim that is absent from `session.json` fails `VER-HEAD`; under a withheld head a missing enumerated key is
  unverifiable (partial) and a missing string key fails.

### Fixed
- Approve, promote, then revise the claim: `replay.py` no longer fails the approval with "recomputed claim_revision_digest
  differs". An approval is verified against the sealed revision whose digest it signed (carried in
  `records/claim_revisions.json`); if the claim has since moved on it is reported as `superseded` (a NOTE; the registry row
  still refers to the approved revision). It fails only when no carried revision matches, the approved revision does not
  verify, or the run rows it was bound to are not in the capsule. For the head revision the stronger recomputation from the
  current claim is kept. `export_session` refuses the crate when a carried approval fails verification, so it no longer
  reports an ok crate for a capsule replay rejects.
- A projected key deleted from `session.json` (for a claim with a sealed chain) is now a difference in the head comparison
  (`VER-HEAD` in the mirror; core needs the same change for exact parity).

## [2.3.6] - 2026-10-16

The fault-matrix-3 follow-ups. It requires aihydro-core 0.2.9 or later.

- **Behaviour change.** `replay.py` now has three outcomes:
  - `ok` exits 0;
  - `ok_partial_withheld` exits 3;
  - `failed` exits 1.

  A capsule with privacy-withheld records now exits 3 instead of 0.
- `VER-HEAD` parity with core.
- Export results carry `status` and `partial`.

Requires aihydro-core 0.2.9 or later.

### Changed
- Three-way outcome (F13) in `replay.py` and in `export_session`: `ok` (exit 0), `ok_partial_withheld` (exit 3: verified
  except privacy-withheld records or an unverifiable withheld head) and `failed` (exit 1); `--live` with nothing comparable
  stays exit 2. `replay.py` prints a `replay_outcome: <status>` line. `export_session` returns `crate_status` and
  `crate_partial` (and `export_crate` returns `status` / `partial`) from core's `VerifyResult`. **Behaviour change:** a
  capsule with privacy-withheld records now exits 3, not 0; the capsule-replay CI job accepts 3 with a notice. A consumer
  that wants to reject withholding should treat 3 as failure.
- The stdlib mirror gains `VER-HEAD` (session claim vs sealed head, identical semantics to core's) and keeps `VER-BAGIT`
  for a missing `manifest-sha256.txt` (F11/F12 parity tests).

## [2.3.5] - 2026-10-16

- The `LOCAL_PATH_IN_TEXT` recovery is field-aware:
  - evidence ids: cite a run id, DOI or named dataset;
  - scope values: use names and identifiers;
  - prose: rephrase.
- The S9 residual is documented.

## [2.3.4] - 2026-10-16

Claim path handling. It requires aihydro-core 0.2.8 or later.

- Concrete local prefixes are normalised at write time: workspace, home,
  AI-Hydro home and temp.
- A claim write whose revision would be withheld at export is refused
  with `LOCAL_PATH_IN_TEXT`, and nothing is recorded.
- New revisions are never stubbed.
- An interim reader rule applies to legacy stub heads.
- A run stub that is not listed is refused by the exporter.

Requires aihydro-core 0.2.8 or later.

### Changed
- `add_claim` and `update_claim_status` replace the concrete local prefixes of this machine in claim prose (statement,
  rationale, limitations) before the revision is sealed: the session workspace, `AIHYDRO_HOME`, the home directory and
  the system temp directory (absolute spellings only) become `<workspace>`, `<aihydro-home>`, `<home>` and `<tmp>`.
  Nothing else is rewritten, evidence and other identifiers are never touched, and the stored claim, the sealed revision
  and the capsule's `session.json` hold identical text. The result carries `paths_scrubbed` (fields changed). A revision
  written because of this carries `cause.path_scrub`.
- If prose still holds something that looks like a local file path after that (one shared test, also used by the capsule
  exporter: core's leak pattern, the run-log scrub, case-insensitive `/users/...`, `~/` and `$HOME`), the tool returns a
  structured error `LOCAL_PATH_IN_TEXT` naming the field and the offending text, and records nothing. Rephrase it, or cite
  the file through `evidence_spans`. The test runs on the whole projected revision, so a path-like evidence id is refused
  too (field named, for example `evidence_spans[2].source_id`), ids are never rewritten, and an accepted write is never
  stubbed by the exporter. Consequence: text such as `HDF /var/streamflow` or `A:/B ratio` is refused; write
  `HDF streamflow files` or `the ratio of A to B` (table in docs/evidence-integrity.md). Tilde approximations such as
  `~mm/day` are accepted (only `~/` and `$HOME` are home). Institutional mounts such as `/data/...` are outside the heuristic.
  A legacy claim whose stored text holds an un-normalisable path takes `add_claim` (same id, rephrased text) first.
- Replay: a legacy scrub marker excuses a difference only if it STARTS its token, and the write-time placeholders do not
  excuse anything.
- `claim_revision_fields` stays a pure projection (no scrub), so claims stored before this change keep reading in sync with
  their approvals and are normalised at their next write.
- Replay compares `session.json` claims with the last non-withheld revision when the head is a privacy stub: a differing
  claim text fails unless only marker-bearing tokens differ; a differing status, confidence or claim type is reported as
  "unverifiable: head withheld" (partial coverage), not a failure.
- The exporter and converter refuse a privacy stub (claim revision or run row) that the manifest does not list as redacted
  at export, and any capsule that fails its own integrity checks.

## [2.3.3] - 2026-10-03

The fault-matrix-2 follow-ups. It requires aihydro-core 0.2.6 or later.

- A crate marker in the manifest: deleting the crate files now fails replay.
- Per-row session ids are checked in replay.
- Only proven privacy stubs count as partial coverage.
- The session claim is compared with its sealed head revision.

### Added
- `extract_hydrological_signatures(precipitation="auto"|"skip")`. `"skip"` makes
  no precipitation request (offline / network-guarded runs: some backends read
  through the netCDF C library, invisible to Python-level network guards);
  `runoff_ratio`/`stream_elas` are None and the run record declares
  `precipitation.status = "not_attempted"`. Default unchanged; a non-default
  value is part of the feature-cache key. Requires aihydro-watershed with the
  `precipitation` parameter.

### Fixed
- D4 sweep, "a validator never passes on a missing or non-physical input":
  `check_record_length` (NaN/inf/negative/non-numeric `n_days` passed),
  `check_temporal_alignment` (equal unparseable or inverted dates passed),
  `check_usgs_qualification_codes` (unreadable or unknown codes were reported
  as approved), `check_stationarity` (NaN/negative discharge gave p = nan, which
  passed; undefined statistic now `insufficient_data`),
  `check_uncertainty_present` (an empty `_uncertainty` block passed).
  `check_unit_consistency` and `check_regulated_basin` were reviewed: no
  violation.
- D4: `check_water_balance_consistency` never passes on a missing or
  non-physical runoff ratio. `None`, non-numeric, non-finite, <= 0 or outside
  [1e-3, 3.0] (engineering bound; > 1 still warns) reports `insufficient_data`,
  as does a ratio whose signatures record marks the precipitation as not used.
  Previously `runoff_ratio = 2.3e-33` passed ("Runoff Ratio: 0.00").
- D4: the signatures run record declares the precipitation the signatures
  received (`status`, `product`, `data_digest`, `reason`) instead of a fixed
  `product: None, data_digest: None`. Requires aihydro-watershed with the
  `_precipitation` result field; older watershed records `status: not_reported`.

## [2.3.1] - 2026-10-03

The q5/q95 fix release (defect P2-D0).

- It requires aihydro-watershed 0.1.1 or later for the corrected values.
- Records produced at `tool_version` 2.3.0 or earlier, or without a
  `flow_quantile_convention` key, predate the fix.

### Fixed

- **`q5` / `q95` signatures follow CAMELS (defect P2-D0).** `extract_hydrological_signatures` (engine in aihydro-watershed) returned `q5` as the 95th percentile of daily flow and `q95` as the 5th, the opposite of CAMELS (Addor et al. 2017, Table 3: `q5` = 5% flow quantile, low flow; `q95` = 95% flow quantile, high flow). It was a computation error, so the value under each key changed (proof-1 gauge 01013500: before q5 6.357 / q95 0.240; CAMELS 0.241 / 6.373; after 0.2405 / 6.3566 mm/day). Results now carry `flow_quantile_convention = "camels_nonexceedance_v1"`; **a run record or session whose signature result lacks that key predates the fix and has `q5`/`q95` swapped.** The key is deliberately not underscore-prefixed, because the evidence capture drops `_` keys; it is retained in the sealed row's `evidence.data` (test: `test_marker_reaches_the_sealed_row`). Requires aihydro-watershed >= 0.1.1 (whose version also moves `env_digest`). Pre-fix = no `flow_quantile_convention` key AND watershed < 0.1.1; `tool_version` <= 2.3.0 is pre-fix, and the release carrying this fix must bump it again. Sealed run records, `session.json` and capsules are never rewritten; the claim `metric_ref` of any pre-fix `q5`/`q95` claim should be treated as referring to the opposite tail (see `docs/vision-2040/findings/defect-q5-q95.md`). Metadata (`camels_metadata.json` x2, `camels_tools.json`), the tool docstring and the FDC plot labels now state the convention; the exceedance flows of `compute_flow_duration_curve` (`Q5` high, `Q95` low) are a different documented convention and are unchanged. Tests: `tests/test_q5_q95_convention.py`; `tests/test_hydrology_tools.py` now asserts `q5 < q95`.

## [2.3.0] - 2026-10-03

Released after e2e proof 2. It contains:

- the neutral analysis tools;
- `data_fetch` retention and `_run_id` on every recorded result;
- seal binding (`evidence_seals`);
- the replay mirror follow-ups;
- stable promotion refusal codes and one shared uncertainty gate;
- carry-through of provider-declared units.

It requires aihydro-core 0.2.5 or later.

### Added

- **Five deterministic series/geometry tools, identical in every evaluation arm** (P1 design ruling, packet P1-T). `summarize_series(series, start?, end?, quantiles?)`, `detect_threshold_runs(series, threshold | threshold_relative={stat, factor}, comparison="gt", gap_policy="break")`, `compare_series(series_a, series_b, convert_to=None)`, `bootstrap_statistic(series, statistic, method, n_resamples, level, seed, block_length?)` and `measure_feature(feature)`. Each `series` argument is the `run_id` of a prior run that retained a series (`data_fetch`, `fetch_streamflow_data`); the producer is declared as a parent of the call, so lineage and the retained file's digest are sealed in the new record, and every success returns `_run_id`. Results echo recorded metadata (units, product, variable) in neutral fields and name their method (quantile method, KGE sd convention, bootstrap interval, geodesic method). Neutrality contract: no warnings, recommendations, `next_steps` or adequacy judgements; the only refusal is a plain `{error, code, message}` for malformed input (unknown run, aggregate-only record where values are needed, bad arguments, a retained file whose digest no longer matches). `detect_threshold_runs` works on the dates actually present and never re-indexes (`gap_policy="break"` ends a run at a missing date or a date with no value; `"skip"` is an explicit choice that bridges them); it does not use the `q.dropna()` event detector in aihydro-lsh. `bootstrap_statistic` has no default `method` (iid vs block must be chosen). `compare_series` converts units only when `convert_to` is given and never refuses on mismatched units. `measure_feature` was chosen over filling `area_km2` in `register_feature` because it works on any registered feature or inline GeoJSON, does not change an existing tool's output in every arm, and leaves its own sealed record. Maths lives in `ai_hydro/analysis/series_ops.py` and wraps `aihydro_core.science._bootstrap`, `aihydro_modelling.search.comparison._kge`, `aihydro_watershed.signatures.signatures._consecutive_event_lengths` and `aihydro_data.geometry.measures.geodesic_area_km2`. Tests (synthetic series only): `tests/test_series_tools.py`; the per-arm tool differences are unchanged (C3-C2 and C2-C1 are the same sets, the new tools are in all arms): `tests/test_series_tools_arms.py`.

### Fixed

- **Wider unit-spelling normalisation for `compare_series(convert_to=...)`.** Spelled-out and exponent forms (`cubic meters per second`, `m\u00b3 s\u207b\u00b9`, `m3 s^-1`, `cubic feet per second`, `mm d-1`, `deg_C`, `\u00b0C`, degrees Celsius/Fahrenheit) fold to the table's units; unrecognised spellings are unchanged.

- **Declared units are carried into the retained series.** The `data_fetch` wrapper writes `units_spec` and `units_declared` (from an aihydro-data that provides them; `vision2040/payload-units`) beside `units`, and the series tools echo all three; unit spellings such as `ft\u00b3/s`, `m\u00b3/s`, `m3 s-1` are recognised by `compare_series(convert_to=...)`.

- **Series tools refuse a producer whose sealed record fails verification** (`RETAINED_SERIES_RECORD_INVALID`; legacy unsealed producers unchanged). Inline GeoJSON in `measure_feature` is capped (depth 12, 5,000,000 vertices, unparseable nesting) with a plain `INVALID_INPUT`. `compare_series` documents that zero-variance inputs give r/sd_ratio/kge = None. Tests assert the `data_fetch` wrapper signature is a superset of aihydro-data's `_data_fetch` and that `data_fetch` results carry `_run_id`.

- **`data_fetch` time series now leave a sealed run record with a retained, addressable series.** Root cause, three compounding gaps: the tool took no `session_id` (passing one failed validation), so a call without chat or study context resolved no session and was never recorded (`no_session`); a call that did resolve got only a minimal row and no `_run_id` in its result; and the result kept a 5-row head only, so the series itself was retained nowhere a later tool could address. `ai_hydro/mcp/tools_data_fetch.py` registers a wrapper under the same name that calls aihydro-data's implementation unchanged, accepts `session_id`, writes the served series (read back from aihydro-data's own cache entry for the call) next to the session, binds its digest to the record (`extra.retained_files`) and returns `_run_id` plus `data.retained_series`. With `cache=False` there is no cache entry, nothing is retained and the record is aggregate-only (`data.series_retained: false`). Calls with no resolvable session are still not recorded.
- **Every recorded tool result now carries `_run_id`.** Tools that did not go through `post_run` (`compute_flow_duration_curve`, `compute_flood_frequency`, `compute_twi`, the delineation tools, ...) were sealed by the middleware but never told the caller the id. `RunRecordMiddleware` now adds `_run_id` (the id of the row it sealed) to a successful dict result that lacks one. Mechanism, not per tool: tools that return `_run_id` already are untouched, error results and non-dict results are untouched, and `_run_id` is a transport key excluded from the output digest, so sealed records do not change. The contract "the middleware never alters the result except `_record_error`" becomes "except `_record_error` and `_run_id`" (`tests/test_run_record_middleware.py`, `tests/test_series_tools.py`).
### Fixed
- **Promotion policy refusals now carry their own stable code.** `promote_claim_to_registry`
  surfaced `EVIDENCE_REQUIRED`, `LIMITATIONS_REQUIRED`, `STATUS_NOT_ELIGIBLE`,
  `UNCERTAINTY_NOT_VERIFIED` and `MODELLED_LIMITATION_REQUIRED` (and a missing claim) as
  `UNEXPECTED_ERROR` with a `_traceback`, because the policy raised bare `ValueError`s. They
  now raise `PolicyRefusal` (same messages) and the envelope's `code` is the first blocking
  violation's code, with every violation in `violations` and no traceback; a missing claim is
  `CLAIM_NOT_FOUND`. `tests/fixtures/promotion_refusals_golden.json` pinned the old
  `UNEXPECTED_ERROR` envelopes; that pin recorded the defect and was updated deliberately.
  Policy refusals from the other families also gain `violations`.
- **The uncertainty gate no longer depends on which tool sets the status.** `add_claim`
  accepted `status="supported"` for a metric-scoped empirical claim without verified
  uncertainty while `update_claim_status` refused it. Both (and redefinition of an existing id
  through `add_claim`) now call one shared check and return the same `uncertainty_gate`
  teaching error; `add_claim` cannot assert `uncertainty_verified`, so record the claim with
  another status and call `update_claim_status(uncertainty_verified=True)`. The gate is in
  every evaluation arm. Claims already stored as supported without verified uncertainty are
  not rewritten: `promotion_check` flags them and promotion refuses them as before.

## [2.2.0] - 2026-10-03

This release is the pin for e2e proof 2.

- It covers everything listed under "Unreleased" below, which was merged
  since 2.1.0: the P1 evaluation layer, structured errors, the run-log
  re-seal, sha256-v3 evidence fingerprints, `EVIDENCE_SEAL_INVALID`, ledger
  revision events, and the slice-5 Bundle/RO-Crate export.
- **Version bump.** Before this bump, `tool_version` stayed at `2.1.0`
  across all of those merges, so sealed run records could not tell the code
  apart.
- **Environment digest.** `ENV_DISTRIBUTIONS` now includes
  `dataretrieval`, the NWIS client.


### Added

- **Fixed: `list_claims` / `list_assumptions` no longer report a missing or unreadable session as "no claims".** They swallowed every exception and returned `[]`. A session with no saved file now fails with `SESSION_NOT_FOUND`, a corrupt one with the load error, both as a `ToolError` whose text is the JSON error envelope (an array-output tool cannot return the error dict); a genuinely empty saved session still lists `[]`. `ArgRepairMiddleware` now re-raises such a deliberate structured `ToolError` instead of turning it into an input-schema "teaching" success (which for these tools was then rejected by output-schema validation). Also `_list_tools_sync` no longer depends on `asyncio.get_event_loop()` (it returned `[]` in processes where an earlier `asyncio.run` had cleared the loop, silently skipping `tools.md`). Tests: `tests/test_ledger_list_errors.py`.
- **Fixed: generated `.aihydrorules` files are no longer written beside the code checkout.** `research.md` (session and project digests) and `tools.md` were written relative to the package checkout (`_REPO_ROOT`, four levels above `store.py`) whenever a session had no `workspace_dir`, so a save wrote outside `$HOME`/`$AIHYDRO_HOME`, concurrent runs and worktree servers overwrote one shared file, and a read-only install failed the save. One resolver, `ai_hydro.registry.paths.rules_dir(workspace_dir=None)`, now serves every writer and reader: `<workspace>/.aihydrorules` when the session has a workspace (the extension reads that directory as user rules, unchanged), else `<AIHYDRO_HOME>/.aihydrorules`. `tools.md` (server start, `write_research_interpretation`) is always under `AIHYDRO_HOME`, never a workspace, because the extension injects every file in a workspace `.aihydrorules`. `store._REPO_ROOT` stays only as an unused name so existing test patches resolve. The unused `persona._RESEARCH_MD` constant is removed. Test: `tests/test_rules_dir.py`. Files already written by older versions (`<checkout parent>/.aihydrorules/`) are stale and can be deleted.
- **Eval condition: refusals raised by the layer are MCP errors; the hidden-name scrub uses live tool names.** Hidden-tool, context-mismatch, invalid-marker and sanitise-failure refusals are now `ToolError(json.dumps(envelope))` instead of shaped successes, so output-schema validation on `-> list[...]` tools (`list_claims`, `list_assumptions`) can no longer replace the `EVAL_CONTEXT_MISMATCH` envelope with "Output validation error" (runner reads `code` from the error text). The C1 `check_*` scrub is built from the live registered hidden tool names instead of a prefix regex, so tokens such as `acquisition_policy="check_only"` are untouched. Test: `tests/test_eval_condition.py`.
- **Evaluation condition layer (P1 / W2, ADR-007).** `ai_hydro/mcp/eval_condition.py` (the only place evaluation-arm logic lives) adds `EvalConditionMiddleware`, registered in `mcp/app.py` after the context middleware and BEFORE `RunRecordMiddleware`, so the sealed run record is built from the unstripped result and stripping happens only on the way out. It is inert unless `$AIHYDRO_HOME/eval_home.json` exists and its `nonce` equals `AIHYDRO_EVAL_NONCE`; the arm (`condition`: C1/C2/C3) comes from the marker, never from a free environment variable. All arms hide `write_research_interpretation`; C1 and C2 also hide the registry tools; C1 also hides every `check_*` tool, `run_skeptic`, `audit_interpretation`, `register_research_plan` and strips `quality_flags`, `promotion_check`, `next_steps` and `skeptic*` fields from agent-visible results (`_run_id` kept). Every call must carry `_meta["aihydro/context"]["client"] = "p1eval/<arm>/<sha256(nonce)[:16]>"` (sealed by the existing mechanism as `extra.context_client`); anything else is refused with `EVAL_CONTEXT_MISMATCH` and not recorded. A matching nonce with an invalid arm fails closed. Production and no-marker runs are unchanged. Test: `tests/test_eval_condition.py`.
- **Evaluation-only approval principal is refused in production (ADR-007, P1 / W3).** The verifier refuses any approver or enrolled principal matching `eval-approver@` when the trust root is `system` (so the writer will not store it either), and `aihydro-approve enroll` refuses to emit an enrolment line for it, with or without `--user-trust`. Under a user-writable trust file it verifies as before, integrity only (`ssh_sig_user_trust`). Nothing else in verification changes. Test: `tests/test_approval_eval_principal.py`.
- **Promotion policy as one pure function (P1 / W1).** `ai_hydro/claims/promotion_policy.py::promotion_violations(session, claim)` returns every reason a claim would be refused, as `Violation{code, message, family, blocking}` (families `id`, `basin_identity`, `evidence`, `limitations`, `status`, `uncertainty`, `observed_modelled`, `metric_binding`); it reads only the claim and retained records. `promote_claim_to_registry` now raises the exception of the first blocking violation, so every refusal code and message is byte-identical to before (`tests/test_promotion_refusal_golden.py`, captured from `5bf155e`); the approval and revision-chain checks stay after it. `add_claim` and `update_claim_status` responses gain advisory `promotion_check: [violations]` from the same function (`tests/test_promotion_policy.py` proves each family appears there and blocks promotion). New non-blocking `EVIDENCE_QUALITY_WARNING` for cited run evidence whose validator flags are `warning`/`insufficient_data` (a `fail`/`error` flag already blocked as `EVIDENCE_CHECK_FAILED`); it never changes a refusal. The former private helpers in `tools_ledger.py` moved to the policy module and remain importable under their old names.
- **Privacy scrubber narrowed and redaction hardened (review of 33cc509).** The scrubber now rewrites only demonstrably local paths (session dir, workspace, home, an allowlist of local roots with >= 2 segments, Windows drive/UNC) and never units, `+/-`, group/endpoint paths or URIs. `declare_lineage` notes and refs are scrubbed before sealing. Export verifies a sealed row before redacting it: a mismatch exports as `seal_mismatch_at_export` (replay FAIL); verified stubs carry `session_id`, `timestamp`, `record_digest`, `entry_digest`, `record`; replay status becomes `archive_integrity_partial`; manifest lists redacted run ids. `.html`/`.svg` exports are scrubbed; workspace lookup is cached.
- **Privacy: run-log row bodies and capsule exports.** Row bodies (`key_outputs`, `evidence`, `inputs`, ...) are path-scrubbed at the store writer before they are sealed (`entry_digest`), so `geometry_geojson_path` etc. become `session-data:` refs. `export_session` scrubs every other exported text/JSON file; legacy sealed rows containing a path are exported as `redacted_for_privacy` stubs (carry `record_digest`), counted in manifest `privacy.legacy_paths_scrubbed_on_export`, and reported by replay as redacted, not PASS/FAIL. `refs.scrub_value`, `capsule/privacy.py`.
- **Canonical place identity in claims (ADR-003, slice 3 P5).** `ClaimScope.basin_refs` (`[{id, label}]`, `aihydro:basin:sha256:<hex>`) is omitted from every dump while unset, so existing claim revision digests and approval bindings are byte-identical (golden test). `add_claim(basin_refs=...)` verifies full BasinRef dicts and auto-binds the watershed slot's `basin_ref` when its usgs alias or gauge label matches `basins` (response `auto_bound`). `promote_claim_to_registry` refuses a basin-scoped claim without `basin_refs` with `BASIN_REF_REQUIRED` (fail closed; older sessions must re-delineate, labels are never back-filled). New `ai_hydro/identity.py`: one USGS site-id rule (8-15 digits) and alias equivalence used by the skeptic scope check (alias ids count as covered; it now also recognises 9-15 digit ids) and the signature-metric gate; the 7-digit legacy acceptance in `session/store.py` stays label-only. `aihydro-approve` shows the claim's basin refs. `add_claim` and promotion refuse session/claim ids outside `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` with `INVALID_ID` (stored legacy ids stay readable but cannot be promoted). Requires `aihydro-core>=0.2.3`. Review fixes: bare digit labels bind and count as covered only through `usgs` aliases (other schemes only as `scheme:id`), so a COMID can no longer launder a USGS site id; a bound basin id must equal a verified ref the session retains (any watershed result, or the full ref passed to `add_claim`, kept as the claim's `basin_ref_records`), else `BASIN_REF_UNKNOWN` at `add_claim` and again at promotion; `replay.py` recomputes `scope.basin_refs` with the same omission-when-None rule.
- **Fixed: silent run-log data loss under concurrency.** Concurrent writers could lose a run-log row: the journal-mode PRAGMA run on every connect raised `database is locked` under contention and the writer swallowed it, returning `error` while the tool call succeeded. The writer now sets WAL only when needed and retries lock contention with backoff for up to 30 s, for both connection setup and the transaction. Measured on the old writer: 8 of 25 runs of the concurrency test lost a row; the fixed writer lost 0 of 800 writes under CPU load.
- **Capsule approvals and external verification (ADR-002b, packet A3).** `export_session` writes `approvals/` (the approval record each promoted claim consumed, signatures included, plus `index.json` with the registry row stamp; sha256 in the manifest, counts under `approvals`); claims with no approval are listed as `no_approval`, never implied approved. The standalone `replay.py` gains `--allowed-signers FILE`: each approval is verified with `ssh-keygen -Y verify` (stdlib subprocess) against the supplied file, namespace `aihydro-approval@v1`, principal = approver id, plus sealed-body digest and `claim_revision_digest` = registry stamp; prints PASS/FAIL per approval and `approvals: N verified against supplied signers, M failed, K unsigned (cli_same_user/opt-out)`; any FAIL exits 1. Without the flag signed approvals are "not verified (no signer file supplied)", never PASS. New `ai_hydro/capsule/approvals.py`; test: `tests/test_capsule_approvals.py`. Review fixes: replay requires the record's `claim_id` to equal the index entry's, one session id across record, index and `session.json`, and recomputes `claim_revision_digest` in stdlib from `session.json` + `run_log.json` (claim edits, cited run-row edits, stamp swaps and cross-session replay now FAIL; non-run-backed evidence FAILs as not reproducible); approval files are confined to `approvals/<64 hex>.json` named by the record digest; approver must be human; index status `approved` renamed `record_carried`.
- **Location-independent file refs in sealed records and capsules (privacy).** Run records no longer store absolute local paths: `extra.retained_files[].path` and served-data `input_refs` use `session-data:<file name>` / `workspace:<relative path>` (`ai_hydro/session/refs.py`: `to_ref`, `resolve_ref`, `ref_name`, `portable`); the digest remains the identity. Capsule `session.json` (and `export_session` JSON) rewrite session-dir/workspace/home paths to refs or `~/`. Legacy records with absolute paths still resolve and capsule matching by name; sealed rows are never rewritten. Test: `tests/test_record_privacy_paths.py`.
- **Explicit request context (ADR-004, P2).** `_ContextInjectionMiddleware` reads `_meta["aihydro/context"] = {study_id?, workspace?, chat_id?, client?}` first and the legacy hidden `_chat_id`/`_workspace` arguments second (still stripped before validation; one deprecation warning per process). `study_id` is the `session_id`. Run records gain `extra.context_source` (`meta`/`legacy_args`/`none`) and `extra.session_resolution` (`explicit_arg`/`meta`/`chat_binding`/`result`/`writer_row`/`auto_create`/`recent_fallback`), plus `extra.context_study_id` and `extra.context_mismatch` when the record's session differs from the requested study; new `helpers._resolve_session_with_rule`. The chat binding file now resolves via `aihydro_home()` (`$AIHYDRO_HOME`). See `docs/context.md`.
- **Fix: tool-registry tests no longer fail when the optional `aihydro-lsh` plugin is installed.** Commit 9a81960 deliberately removed the `lsh_*` tiers and domain from the built-in registry (aihydro-lsh is an optional `aihydro.tools` plugin, not installable in public CI; its tools default to tier 2), but the three registry tests still required every registered tool to be in `TOOL_TIERS`. `tests/_plugin_tools.py` identifies tools contributed by `aihydro.tools` entry points whose distribution is not a declared dependency of aihydro-tools (read from `pyproject.toml`), and the tests exclude only those (a declared dependency such as aihydro-data is never exempt, covered by a negative test); built-in and aihydro-data tools stay strictly checked in both directions.
- **Location-independent file refs in sealed records and capsules (privacy).** Error text sealed in minimal rows (`error_summary`) is path-scrubbed at write time (`refs.scrub_paths`: home to `~/`, session dir to `session-data:`, workspace to `workspace:`, other absolute to `<abs>/basename`); Windows drive/UNC paths handled. Run records no longer store absolute local paths: `extra.retained_files[].path` and served-data `input_refs` use `session-data:<file name>` / `workspace:<relative path>` (`ai_hydro/session/refs.py`: `to_ref`, `resolve_ref`, `ref_name`, `portable`); the digest remains the identity. Capsule `session.json` (and `export_session` JSON) rewrite session-dir/workspace/home paths to refs or `~/`. Legacy records with absolute paths still resolve and capsule matching by name; sealed rows are never rewritten. Test: `tests/test_record_privacy_paths.py`.
- **Claim revisions (ADR-001/002, slice 2).** New sealed, insert-only `aihydro.claim_revision_record/1` store (`ai_hydro/session/claim_revisions.py`, `<session_id>.claims.sqlite3`) written by `add_claim`, `update_claim_status`, `promote_claim_to_registry` and `check_registry_staleness`. One revision per authority-bearing change, with `cause.reason` `created`/`redefined`/`status_update`/`promotion`/`staleness`/`evidence_drift`/`out_of_band_edit`/`legacy_unrecorded`. Claims that predate the store get a `legacy_unrecorded` revision 0 on first touch; nothing is back-filled. See `docs/evidence-integrity.md`.
- Registry rows gain `claim_revision_digest` and `claim_revision`.
- `check_registry_staleness` returns `revision_chain_mismatches` (`revision_chain_mismatch`: `missing_revision`, `different_digest`, `chain_corrupt`) by comparing stamped registry rows with the claim revision chain. This is the only external anchor of the chain; the chain alone cannot detect tail truncation or edit-and-reseal of the latest row.
- `claim_revisions.history()` returns per-claim `{ok, rows | error}`; new `get_revision()` and pure `revision_drift()` helpers.

- **Signed approvals (ADR-002b, packet A1).** `aihydro-approve` signs the confirmed approval with an SSH key (`ssh-keygen -Y sign -n aihydro-approval@v1`, stdlib subprocess, no new dependency) and writes `aihydro.approval/2` (`signer` sealed, `signature` over `record_digest` outside the seal). `ai_hydro/approval/trust.py` verifies against `allowed_signers` (`/etc/aihydro/allowed_signers` = `system`, else `$AIHYDRO_HOME/trust/allowed_signers` = `user_writable`; `valid-after`/`valid-before` honoured), derives a channel that names key class and trust root (`ssh_sig[_sk]_system_trust`, `_user_trust`, `_supplied`, `cli_same_user`; `system` only for a file and directory the user can neither write nor owns) and refuses missing/invalid signatures, unenrolled, expired and revoked keys. New CLI subcommands `enroll` (prints the line and sudo command, never runs sudo) and `revoke` (sealed revocation line). Fails closed: with no allowed_signers nothing verifies; unsigned v1 approvals need an explicit `AIHYDRO_REQUIRE_SIGNED=0` opt-out (logged, stamped `policy: unsigned_opt_out`). `approver.id` must equal the enrolled principal. Local revocation is advisory (deletable); real revocation is `valid-before` in the system file. `approval_stamp(record)` and `verified_channel(record)` exported for the registry stamp (A2). Signing code is confined to `approval/signing.py`; the authority test now also fails if anything under `ai_hydro/mcp` can sign.
- **Run records for every tool call (ADR-001).** A FastMCP `RunRecordMiddleware` attaches a sealed `aihydro.run/2` record (`aihydro_core.records`) to the run-log row of every tool call that resolves a session: tool and version, input and output digests, environment digest, parents, and an explicit `record_error` when a digest is missing (the tool result gains `_record_error`; the middleware never fails a call). Failed calls are recorded. Catalog, read-only view, UI and lifecycle tools are exempt, each with a reason, in `ai_hydro/session/run_records.py::RECORD_EXEMPT`; `tests/test_run_record_coverage.py` fails when a registered tool is neither recorded nor exempt. Requires an `aihydro-core` that provides `aihydro_core.records`.
- **Insert-only run-log rows.** A row with a sealed record cannot be changed by a same-id write: identical writes are no-ops, a stale legacy snapshot never drops the record, anything else is refused and logged. Legacy rows and `set("_run_log")` keep working.
- **First lineage edge.** `extract_hydrological_signatures` records the streamflow run it consumed (`parents`, plus `served_data` input refs with the producer's output digest and the digest of the series read). Streamflow slots carry `meta.run_id`, stamped when stored; older slots carry none and get no edge.
- **Fix: the lineage edge now holds on the real fetch -> signatures path.** In a session without a workspace the lean session JSON kept only `q_cms_n`, so `extract_hydrological_signatures` never saw the series, silently refetched its own NWIS data, and recorded `parents=[]`. `fetch_streamflow_data` now writes the series next to the session file when there is no workspace (`store.write_session_data_file`); signatures uses the slot only when its recorded gauge, start and end explicitly equal the request (exact equality; empty params or a superset period do not match) and the retained file's digest, recorded at fetch time in the slot (`meta.retained_series`) and the fetch record (`extra.retained_files`, role `artifact`), still matches the file read. A mismatch is never consumed: no edge, `parent_unresolved` says why (`retained_series_digest_mismatch`), and signatures refetches. Acquisitions made inside signatures (its own streamflow fetch, and always the precipitation fetch) are listed in `extra.internal_acquisitions`; the precipitation product and series digest are not observable from aihydro-tools, so only request period and cited sources are recorded. Covered by `tests/test_run_record_lineage_real_tools.py` (real tools, NWIS client mocked).
- **Fix: `tool_version` comes from the package.** `ai_hydro.__version__` is now defined in source (kept equal to `pyproject.toml` by `tests/test_package_version.py`) and records use `version_source: "package"`; installed distribution metadata is only a fallback (a stale editable-install dist-info reported 2.0.0 for 2.1.0 source).
- **Fix: a missing prerequisite is `MISSING_PREREQUISITES`, not `UNEXPECTED_ERROR`.** "No watershed cached for session ... Run delineate_watershed first." now returns `MissingPrerequisiteError` -> code `MISSING_PREREQUISITES` with `next_tools=["delineate_watershed"]`.
- **Capsule retains the served streamflow series.** `export_session` writes `data/served_streamflow_<gauge>.csv` (`date,q_cms`) and `capsule_manifest.json` gains `data_artifacts` (sha256, producing run id and record digest, retrieval mechanism and cache-hit flag, export-time consistency checks, or an explicit `unavailable` reason). Previously no discharge series survived export, so no claim could be recomputed outside the platform. Consumes the series the fetch tool retained (`_data_file`, copied verbatim and also written as CSV), checks its digest against the consumer's recorded `<run_id>#q_cms` ref, and falls back to `aihydro_data.fetch` only for sessions written before retention; `replay_status` stays `archive_integrity` and `recomputation` stays `not_performed`.
- **Capsule series binding.** `data_artifacts[].binding` is `producer_sealed` when the producer's sealed record (`extra.retained_files`) names the retained file's digest and the export matches, else `self_attested`; `replay.py` verifies this from `run_log.json` and fails on a swap even with a regenerated manifest. Re-queries are no longer attributed to the run (`produced_by_run_id` null, `requested_by_run_id`, status `exported_requeried`/`exported_from_cache`, product-vs-slot check). `capsule_manifest.json` drops the absolute `capsule_dir`.
- Research snapshot: additive `record_coverage` and `record_errors` fields.
- Research snapshot (P3, additive, `schema_version` stays 1): run rows carry `minimal` and `record_error` and minimal rows always have `key_outputs: {}`; claims carry `revision`, `revision_digest`, `history_len`, `revision_drift`, `revision_error` and a fail-closed `approval` state (`none|approved|consumed|stale_evidence|stale_revision|evidence_unchecked|unverifiable`). Run-span evidence is re-fingerprinted read-only from the snapshot's run log, so `approved`/`in_sync` mean what the promotion gate means; dataset/paper spans read `evidence_unchecked`. One corrupt claim chain sets that claim's `revision_error` only. The read path never writes (`mode=ro`, no PRAGMA/DDL; read-only directories fall back to `immutable=1`, reported as `run_log_source`/`revision_source` `sqlite_immutable`); new read-only `claim_revisions.history_readonly()`. See `docs/research-snapshots.md`.
- `capsule_manifest.json`: `replay_status: "archive_integrity"`, `recomputation: "not_performed"`, and a `run_records` summary including the exporting environment.

### Changed

- **`replay.py --live` no longer passes vacuously.** The generated verifier (now `ai_hydro/capsule/standalone_replay.py`, written verbatim into each capsule) reads the real export shape (slots are top-level `session.json` keys, not `session["slots"]`), verifies every v2 record and its binding to its run-log row, prints `replay_status` and the comparison count, and exits 2 when `--live` finds nothing comparable. It never claims recomputation. `capsule.manifest.verify_live` delegates to the same code.
- `post_run` run ids carry 32 bits of entropy (`{hex8}` suffix, was 4 hex digits) and are checked for uniqueness within the session. Older ids remain valid; no consumer parses the suffix.
- `post_run` and the legacy `_record_run_log_entry` write one run-log row directly instead of re-sending the whole log.

### Changed (behaviour change)

- `promote_claim_to_registry` requires the approval's `claim_revision_digest` to equal the latest stored revision's digest. If the claim's retained evidence changed since that revision, a revision with cause `evidence_drift` is written and promotion is refused with `APPROVAL_REQUIRED` unless an unused approval already binds the new revision.
- Requires `aihydro-core>=0.2.2` (`ClaimRevision`).
- `promote_claim_to_registry` now requires a **human approval record** bound to the claim's current revision: text, `claim_type`, status, confidence and rationale, scope, evidence spans, limitations, `prereg_id`, `uncertainty_verified`, and the `sha256-v2` fingerprints of the retained evidence it cites (revision schema `aihydro.claim_revision/2`). `researcher_approved=True` is only a request flag and no longer suffices: without a matching record the tool returns `APPROVAL_REQUIRED` and the exact `aihydro-approve` command (not the digest). Editing the claim, or mutating retained evidence after approval, invalidates the approval. An approval is **single use**: the registry refuses a second row citing the same record. The check runs after the evidence/limitation gates. (ADR-002a)
- Approval records and registry stamps carry `channel: "cli_same_user"`. This blocks unintended self-approval through tools; it does not stop a same-OS-user process from forging a sealed record or driving the CLI through a pty. Closing that needs ADR-002b signing.
- Registry entries gain `approval: {"record_digest": ..., "channel": ...}`. Rows written before this change are not rewritten; `list_registry_claims` (new `n_self_asserted` count) and the defensibility report label them `approval: self_asserted`. The defensibility report gains a "Registry promotions and approval" table and `n_promoted_claims` / `n_self_asserted_promotions` summary keys; `build_defensibility_report` takes an optional `registry_entries` argument.
- The registry path honours `AIHYDRO_HOME` (default `~/.aihydro`), resolved when used rather than at import. `registry.store.REGISTRY_DIR` / `CLAIMS_FILE` are now `None`-by-default overrides; use `registry_dir()` / `claims_file()`.
- Registry read-modify-write (`append`, `mark_stale`, `mark_retracted`) runs under a cross-process lock (`fcntl.flock`; `msvcrt.locking` on Windows; logged no-op if neither exists).

### Added

- `aihydro-approve <session_id> <claim_id>` console script and `ai_hydro.approval` package: the only writer of the append-only approval store at `$AIHYDRO_HOME/approvals/`. Interactive terminal only; requires typing the claim revision digest prefix. A test fails if any MCP tool or module imports or calls the writer.
- Test and bench isolation: `tests/conftest.py` gives every test its own `AIHYDRO_HOME`; `aihydro-bench --run` sets a temporary one. Bench promotion tasks B-016 and B-045 declare `setup.approve_claims` and previously wrote promoted fixtures into the user's real `~/.aihydro/registry`.
- Golden test pinning one legacy `sha256-v2` evidence fingerprint.
- Requires `aihydro_core.records` (`digest`, `Actor`, `utc_now`) from the records-v2 core contract; the `aihydro-core` dependency pin needs to move to the release that ships it.

### Fixed

- Curve number: NLCD 81 (Pasture/Hay) now uses TR-55 pasture, good condition (39/61/74/80 for groups A-D). It previously used the row-crop values (67/78/85/89), which overstated CN on pasture by 9 to 28 points. (Fix lives in `aihydro-watershed`.)
- `create_cn_grid` and `fetch_lulc_data` default to NLCD 2021, the latest release `pygeohydro` serves (was 2019).
- `compute_soil_loss_rusle` read the session soil slot through an attribute that does not exist, so the "K from session soil texture" branch never ran. It now reads the slot correctly.

### Added

- `fetch_soil_attributes_ssurgo` tool (CONUS): SSURGO hydrologic soil group as recorded per component, surface-horizon erodibility Kw (US customary and SI), and sand/silt/clay, area-weighted over the watershed. Data: gNATSGO map units (Planetary Computer) + USDA Soil Data Access. `compute_soil_loss_rusle` uses the recorded Kw as K when present.
- `create_cn_grid` uses SSURGO recorded hydrologic groups by default in CONUS (POLARIS/SoilGrids texture inference remains the fallback and the route elsewhere). New `dual_hsg` parameter picks the drained or undrained condition for A/D, B/D, C/D soils; the CN mean for the other condition, `hsg_method`, and `kw_mean` are returned.
- `delineate_watershed_from_point(method="small_catchment")` (alias `3dep`) for CONUS catchments under ~5 km2 such as road culverts: USGS 3DEP 10 m, road-embankment notch, 40 m snap. `auto` uses it first when `expected_area_km2 < 5`.

---

## [2.1.0] — 2026-06-25

### Added

- Production-hardened HydroResearch-Bench / `aihydro-bench`: packaged benchmark code/data, schema validation, machine-readable certification JSON, installed CLI smoke support, and CI artifacts.
- Defensible flood inundation and meta-modelling tool surfaces from the `feat/result-contract` branch.
- `ARCHITECTURE.md` documenting the meta-package boundaries, `aihydro-data`-first live benchmark rule, and optional modelling dependency policy.

### Fixed

- Tightened base install dependency bounds to the compatible published line: `aihydro-core[contracts]>=0.2,<0.3`, `aihydro-data>=0.2.1,<0.3`, and `aihydro-watershed>=0.1,<0.2`.
- Kept `aihydro-modelling` behind the `modelling` extra until it is published on PyPI, preserving base install resolution.

---

## [2.0.0] — 2026-06-15

This major release cuts the accumulated Phase 1–5 work (defensibility core, fleet
experiments, skeptic, literature grounding, HydroResearch-Bench, headless
resources, certification) plus the agent-efficiency and context-injection fixes
below. It supersedes the never-published `[2.0.0] — 2026-04-24` doc entry further
down this file (PyPI latest was 1.7.0).

### Added

- **HydroResearch-Bench production hardening**: `bench/schema.py` validates the benchmark catalog (`schema_version`, suite id, task ids, marks, call styles, required rationale, target package defaults, and oracle assertions); `tests/test_bench_schema.py` makes schema validity a CI gate. `bench/gen_scorecard.py` now validates the catalog before rendering and can emit machine-readable certification JSON via `--json-out`; CI uploads both `hrb_scorecard.html` and `hrb_certification.json`. `aihydro-bench` console entry point exposes the scorecard/certification generator from installed wheels. `bench/BENCHMARK.md` updated to the current B-001–B-079 suite and data-access policy.
- **Install-resolution hardening**: base dependencies now require the compatible `aihydro-core[contracts]>=0.2,<0.3`, `aihydro-data>=0.2.1,<0.3`, and `aihydro-watershed>=0.1,<0.2` line. `aihydro-modelling` is no longer a base dependency until published on PyPI; it remains behind the `modelling` extra.
- **Token-efficient long-job waiting — `wait_for_job(job_id)`** (`tools_modelling.py`, Tier 3): blocks server-side, polling the job's `status.json` every few seconds at zero token cost, and returns only when the job reaches a terminal state (or after a ~280 s budget, just under the MCP transport timeout — then the agent calls once more). Replaces the previous pattern of calling `get_job_status` in a loop, where every poll was a full LLM turn re-reading the whole context. Returns a terse status plus a `retrieve_with` hint; `train_hydro_model` and `data_fetch_background` now point at it. Added to the `execution` discovery domain.
- **Phase 5.2 — HydroResearch-Bench artifact**: `bench/gen_scorecard.py` — self-contained HTML scorecard generator over the 60 benchmark tasks (B-001–B-060). `--run` mode executes pytest with JUnit XML and overlays per-task pass/fail; catalog mode renders the task inventory. Tasks grouped by 16 categories with color-coded status badges and a summary grid (total/passed/failed/skipped/live/pass-rate). CI (`bench.yml`) generates the scorecard after every fixture run and uploads it as the `hrb-scorecard` artifact (90-day retention). `bench/BENCHMARK.md` — standalone benchmark documentation (category table, oracle-operator reference, task format, usage).
- **Phase 4.4 — Agent-native map control**: three new Tier-2 MCP tools in `tools_map.py` — `map_fly_to` (smooth camera navigation to a lon/lat/zoom via deck.gl `FlyToInterpolator`), `map_add_layer_from_run` (reads a session's `_run_log`, auto-detects GeoJSON in `key_outputs`, pushes it as a provenance-tagged layer with `run_id`/`session_id` metadata), and `map_set_time_range` (drives the map time slider / `BrushContext`). New push helpers in `map_commands.py`: `push_fly_to`, `push_add_layer_from_run`, `push_set_time_range`.
- **Phase 3.3 — Marketplace certification + citable credit**: `Gallery/scripts/certify_manifests.py` — schema-specific certification functions for Gallery (citation/trustLevel/badges/license), Skills (description/when_to_use/tools_used/tags), and Modules (citation.text/description/tags/estimatedMinutes). Certification injected into generated `api/*.json` in all three surface `build-api.yml` CI workflows (advisory, non-blocking). `CITATION.cff` (CFF 1.2.0) created in aihydro-tools, aihydro-core, and aihydro-data. Recognition worker gains `cite` event type (fires on formal citation export, distinct from `copy_citation`). 23 tests.
- **Phase 3.2 — Headless platform**: `ai_hydro/mcp/resources.py` — 4 new session-level MCP resources: `aihydro://session/list` (enumerate all sessions), `aihydro://session/{id}` (summary), `aihydro://session/{id}/claims` (full ledger), `aihydro://session/{id}/evidence_board` (kanban by status), `aihydro://session/{id}/experiments` (experiment slot). All resources are read-only, degrade gracefully on missing session, and require only an explicit `session_id` — no VS Code or chat binding needed. `tests/test_resources.py` — 26 tests including headless-mode verification.
- **Phase 2.5 — HydroResearch-Bench**: `bench/oracle.py` — 4 new operators: `ge`, `len_gte`, `len_eq`, empty-path support for bare-list results. `tests/test_bench.py` — `run_log` setup key in `_build_session` (seeded run-log entries for provenance tests). `bench/tasks.yaml` GROUP O — 11 end-to-end tasks B-050/B-060 covering: audit pass/fail, `[lit:tag]` and `[run:id#path]` resolution, value mismatch, `write_research_interpretation` gate, claim lifecycle, `list_claims`, `export_session(defensibility_report)`, `get_session_health`.
- **Phase 2.4 — Literature grounding**: `ai_hydro/knowledge/embeddings.py` — passage-level index (SHA-256[:16] `passage_hash`, 200-word sliding chunks, TF-IDF search, no ML model). Three new MCP tools: `index_passages` (Tier 2), `search_passages_tool` (Tier 2), `resolve_passage` (Tier 3). Auditor upgraded: `[lit:<hash>]` tags resolve against passage index; `lit_unresolvable` advisory (non-blocking); `AuditReport` gains `lit_span_count`, `lit_resolved_count`, `lit_advisories`. Bench B-048/B-049.
- **Phase 2.3 — Skeptic/referee agent**: `ai_hydro/skeptic/` package — four deterministic checks (stale citations, scope overreach, unvalidated high-risk assumptions, registry conflicts). `run_skeptic` MCP tool (Tier 1). Advisory pass integrated into `write_research_interpretation` — findings are advisory, never blocking.
- **Phase 2.2 — Global claim registry + living claims**: `ai_hydro/registry/store.py` — append-only JSONL registry at `~/.aihydro/registry/claims.jsonl` with `append`, `find_by_*`, `mark_stale`, `mark_retracted`, `build_registry_id`, `snapshot_evidence_versions`, and `check_evidence_staleness`. `promote_claim_to_registry` now writes real JSONL entries with evidence version hashes. New tools: `check_registry_staleness` (detects data drift), `list_registry_claims` (query). Evidence Board shows `stale` status column.
- **Phase 2.1 — Fleet-scale experiments**: `ai_hydro/experiments/` package, `tools_experiments.py` (`define_experiment`, `run_experiment`, `get_experiment_table`). VS Code `ExperimentTable` panel reads session JSON directly. Bench tasks B-043/B-044/B-045.

- **Pour-point delineation** (`ai_hydro/analysis/delineation/`): tiered `delineate_from_point` (`auto`, `fast`, `merit_basins`), cloud DEM + pysheds fast tier, NLDI COMID for CONUS.
- **`delineate_watershed_from_point`** MCP tool and **`hydro_map_cli delineate-point`** for the VS Code map Quick delineate button.
- **`resolve_comid_for_quick`** — walks `downstreamMain` when nearest COMID is a tiny tributary reach.
- **`scripts/profile_delineation.py`** and **`tests/test_delineation.py`**.

### Fixed

- **CRITICAL: total MCP outage for the VS Code extension — `_chat_id` / `_workspace` injection (2026-06-13).** Every `aihydro-tools` tool call from the extension failed with `ValidationError: Unexpected keyword argument _chat_id` (and `_workspace`). Root cause: the interceptor that strips these injected identity params was installed by monkeypatching `mcp._call_tool_mcp` *after* `FastMCP.__init__`, but FastMCP 3.x binds that method into its low-level request handler during construction — so the patch was dead code and the injected keys reached Pydantic validation on every call. The agent could not work around it (the keys are server-injected, not agent-supplied), and would drift to unrelated servers/pipelines. Replaced the monkeypatch with a proper FastMCP `_ContextInjectionMiddleware` (registered via `add_middleware`) whose `on_call_tool` hook strips both keys before validation and stores them in the `ACTIVE_CHAT_ID` / `ACTIVE_WORKSPACE` ContextVars. Ordered before `ArgRepairMiddleware` so the injected keys are never misread as typos. Regression guard: `tests/test_context_injection.py` drives the real `CallToolRequest` handler path with injected params (the original patch shipped with no test exercising that path).
- **Agent persona — systematic-failure stop rule.** Added guidance: when several *different* tools fail with the same structural error (identical unexpected-keyword / connection / auth message), the cause is the server/config, not the arguments — stop after two such failures and report the blocker plainly rather than silently switching to a different server or an unrelated pipeline, or reimplementing a failed tool with shell heredocs. (Persona trimmed elsewhere to stay under the 700-word budget.)
- **Agent persona — discover-before-commit + scope-matching (general routing robustness).** Two general principles (naming no specific server/tool): (1) do not infer a tool, library, or another server's abilities from its name — the native toolset is the primary instrument; verify capabilities with `aihydro_describe_capability`/`list_available_tools` and use another connected server only when a task explicitly needs its specialty; (2) prefer the most direct tool that satisfies the request — do not launch a long-running or multi-stage pipeline unless the request requires it. This is the platform-side half of the native-primacy routing fix (the extension-side half reorders/frames MCP servers in the system prompt). Motivated by a traced session where the agent routed a lightweight "delineate + signatures" task to a heavyweight third-party model-building server purely by name-association.
- **B-009 benchmark re-adjudication (2026-06-13).** The B-009 bench task (`baseflow_index` for the humid synthetic series) had expected bounds [0.40, 0.75] calibrated against the old inverted filter (which returned the quick-flow fraction). The corrected Lyne-Hollick filter yields BFI ≈ 0.29 for this series. Bounds re-adjudicated to [0.20, 0.40] — calibrated against the corrected algorithm and verified across 5 seeds. The `synthetic.py` docstring also corrected (CAMELS [0.45, 0.70] uses the Eckhardt filter, not Lyne-Hollick). All 59 fixture bench tasks now pass.
- **`baseflow_index` correctness (behaviour change).** The Lyne-Hollick filter in `extract_hydrological_signatures` returned the quick-flow component instead of the baseflow residual (`q − f`), and the multi-pass loop re-fed quick-flow as input. As a result `baseflow_index` reported the *quick-flow* fraction, not the baseflow fraction (e.g. USGS 01109000 returned 0.089 instead of the correct 0.607). Replaced with the standard Nathan-McMahon multi-pass form (baseflow = `q − f` per sweep, output feeds the next alternating-direction sweep). Added known-answer regression tests (`tests/test_hydrology_tools.py::TestBaseflowFilter`). **Anyone who computed `baseflow_index` with any prior published version received the quick-flow fraction and should recompute.**
- **CONUS map quick delineate** no longer returns tiny tributary basins (~6 km²) from nearest COMID alone.
- **CONUS `auto` without `expected_area_km2`** uses fast NLDI (~1–5 s) when basin area is in range; falls back to cloud DEM only when NLDI is unavailable or out of range.
- **`delineate_watershed(gauge_id)`** — NLDI `get_basins` fallback via COMID at gauge coordinates when site-id lookup fails.

### Added

- **Map orchestration MCP tools** (`tools_map.py`): `map_get_state`, `map_list_layers`, `map_update_layer`, `map_apply_symbology`, `map_remove_layer`, `map_set_basemap`, `map_fit_layer`, `map_set_working_geometry`, `map_save_roi` — agent-governed symbology and layer control via `~/.aihydro/map_commands/` + `map_layer_catalog.json`.
- **`map_commands.py`** — host command bridge (`update_layer`, `set_basemap`, `fit_layer`, …).
- **`map_layer_catalog.py`** — graduated symbology break computation; reads host layer catalog written by the VS Code extension.
- **`_resolve_active_roi_geojson`** in `helpers.py`; **`working_geometry_path`** on `HydroSession`.

---

## [1.7.0] — 2026-05-25

### Added — Course mode

Five MCP tools that make the agent course-aware and able to author courses. Targets the HTML Preview panel's course feature (folders with a `course.json` manifest grouping multiple HTML modules into a guided learning path).

- **`course_get_state`** (tier 3) — read the active-course pointer at `~/.aihydro/active_course.json` plus the per-course progress file; return current module, completion %, locked modules, and a `next_recommended` module id.
- **`course_get_curriculum`** (tier 3) — full manifest + prerequisite graph; defaults to the active course or accepts an explicit `course.json` path.
- **`course_set_progress`** (tier 2) — `complete | uncomplete | unlock_prereqs | set_current`. Records `agentGranted: true` + caller-supplied `reason` for transparency.
- **`course_navigate`** (tier 2) — write `~/.aihydro/course_nav_intent.json`; the HTML Preview panel watches it and switches to the requested module (prerequisite gate enforced webview-side).
- **`course_scaffold`** (tier 2) — write `course.json` + AI-Hydro-styled HTML skeletons for each module. Auto-slugifies ids from titles, validates the prerequisite graph for cycles via iterative 3-colour DFS.

### Companion artifact

- `course-authoring` skill added to the AI-Hydro Skills marketplace (separate repo: `github.com/AI-Hydro/Skills`) — pairs with `course_scaffold` per the established skill+tool pattern.

### System prompt

- New `COURSE MODE` section instructing the agent to call `course_get_state` at the start of any course-related conversation and to require explicit user agreement before mutating progress.

### Disk contracts (shared with the VS Code extension)

| File | Reader | Writer |
|---|---|---|
| `~/.aihydro/active_course.json` | tools | webview |
| `~/.aihydro/course_progress/<id>.json` | tools, webview | tools, webview |
| `~/.aihydro/course_nav_intent.json` | extension host | tools |

---

## [2.0.0] — 2026-04-24

### Removed (breaking changes)

- **`sync_research_context`** MCP tool removed. Deprecated in 1.6.0. Replace with `get_session_raw_state` (Phase 1) + `write_research_interpretation` (Phase 2). See [MIGRATION.md](MIGRATION.md).
- **`_train_hydro_model_sync_alias`** private function removed. Deprecated in 1.7.0. Use `train_hydro_model` (kickoff) + `get_training_status` (poll) directly.
- **`findings` field** on `get_session_summary` and `get_project_summary` responses. Deprecated in 1.6.0. Use `get_session_raw_state` to read computed data.
- All `DeprecationWarning` emissions on normal usage are eliminated. Clean import emits zero warnings.

### Added

- **P2 library cards** under `ai_hydro/knowledge/library_refs/`: `pandas`, `numpy`, `shapely`, `matplotlib`, `folium`. Each contains ≥8 gotchas, ≥4 common patterns, and `version_compatible` range. Discoverable via `get_library_reference()`.
- **`export_session` `capsule_path` parameter** — explicit output path for the capsule folder. Signature: `export_session(session_id, capsule_path=None, format="capsule")`.
- **`model/` directory in research capsule** — copies trained model artifacts (HBV params, simulated discharge CSV, metrics summary) alongside `data/`, `figures/`, `environment.yml`, `citations.bib`.
- **`MIGRATION.md`** — migration guide covering every removed 1.x signature with before/after examples.
- **knowledge-compat.yml CI extended** with P2 card smoke tests (pandas, numpy, shapely, matplotlib, folium).

### Changed

- `get_library_reference` façade retained (M3 façade decision: MCP resource support not yet uniform across Cline/Claude Code/Claude Desktop; see §7.6). Decision documented in function docstring.
- `_sync_reminder` in `helpers.py` updated to reference `write_research_interpretation` instead of removed `sync_research_context`.
- All internal documentation and `research.md` templates updated to reference the two-phase split tools.

---

## [1.7.0] — 2026-04-24

### Added

- **`get_training_status(job_id)`** — poll a training job started by `train_hydro_model`. Reads `status.json` from the job artifact directory. Returns `{job_id, status, progress, partial_results, error, log_path, updated_at}`.
- **`ai_hydro/modelling/runner.py`** — detached subprocess runner for training jobs. Reads `job_config.json`, runs training, writes checkpoints + `status.json` at completion or failure.
- **Six built-in v1 skills** (loaded by `list_skills()`, stored under `ai_hydro/skills/`):
  - `flood-frequency-analysis` — extreme-value FFA, USGS Bulletin 17C workflow, distribution selection.
  - `baseflow-separation` — Lyne-Hollick vs UKIH method selection, BFI interpretation by climate/geology.
  - `model-selection` — HBV-light vs LSTM vs regionalization decision guide (Nearing 2021, Beck 2020).
  - `calibration-diagnostics` — NSE/KGE decomposition, common pathologies, next-step recommendations (Gupta 2009, Clark 2021).
  - `signature-interpretation` — FDC shape, BFI, runoff ratio, Q95/Q05 → basin-storyline paragraph (Addor 2018).
  - `watershed-analysis-workflow` — end-to-end pipeline orchestrating all core analysis tools.
- **P1 library cards:** `torch.json` and `geopandas.json` under `ai_hydro/knowledge/library_refs/`. Both include `version_compatible` range and hydrology-specific gotchas.
- **`.github/workflows/knowledge-compat.yml`** — weekly + release-triggered CI workflow that smoke-tests each library card against its `version_compatible` lower-bound and validates required JSON fields (B2 Tier 3 enforcement).

### Changed

- **`train_hydro_model` rewritten as kickoff tool (R2 compliance):** Now returns `{job_id, status: "pending", artifact_dir, log_path, started_at}` immediately after spawning a detached subprocess. Heavy work (HBV restarts, LSTM training) runs in the background.
- **`get_model_results`** extended to accept an optional `job_id` argument — reads results directly from the artifact directory when a complete job is available, without requiring session cache.

### Deprecated

- **`train_hydro_model` synchronous call** (`_train_hydro_model_sync_alias`): calling with session_id only kicks off then polls (backward-compat wrapper). Emits `DeprecationWarning("Calling train_hydro_model synchronously will be removed in 2.0...")`; removal in 2.0.

---

## [1.6.0] — 2026-04-23

### Added
- **`get_session_raw_state(session_id)`** — Phase 1 of the two-phase interpretation workflow (G1 compliance). Returns raw computed slot data for the LLM to read before authoring scientific prose.
- **`write_research_interpretation(session_id, site_name, interpretation)`** — Phase 2 of the two-phase workflow. Writes LLM-authored interpretation into `research.md` and session. Replaces the write path of `sync_research_context`.
- **`run_python(script, workspace_dir, timeout_seconds, allow_network)`** — first-party workspace-scoped Python execution tool. Replaces the out-of-tree `mcp_python` reference. Workspace-scoped CWD, no network by default, stdin-only (no shell interpolation), 120s default timeout.
- **`list_relevant_clis()`** — enumerate AI-Hydro-aware CLI tools installed in the environment. Discovers tools via `aihydro.clis` entry-point group; falls back to best-effort detection of `swat` and `camels-extract` binaries.
- **`list_skills(domain, workspace_dir)`** — enumerate workflow skills across built-in / plugin / workspace tiers. Returns empty list in 1.6.0 (no built-in skills yet); plugin and workspace tiers polled.
- **`load_skill(name, workspace_dir)`** — load the full content of a named workflow skill (frontmatter + body).
- **`separate_baseflow(session_id, method, alpha, n_passes)`** — baseflow separation via Lyne-Hollick (1979) recursive filter or UKIH five-day interval method (Gustard et al. 1992). Writes full daily series + BFI to `session.baseflow`. Existing BFI scalar in `extract_hydrological_signatures` unchanged.
- **`ai_hydro/mcp/resources.py`** — native MCP resource layer for knowledge cards. URI scheme: `aihydro://knowledge/library/{name}` and `aihydro://knowledge/list`. Runtime drift detection compares installed library version against card `version_compatible` range; injects `stale: true` + `stale_reason` when outside range.
- **`ai_hydro/skills/`** package — three-tier skill registry (built-in / `aihydro.skills` entry-point / workspace `.aihydrorules/skills/`). YAML frontmatter parsed; workspace tier overrides plugin overrides built-in.
- `aihydro.skills` and `aihydro.clis` entry-point groups in `pyproject.toml`.

### Changed
- **Persona rewrite (T2.1):** Replaced 126-line nominal persona with ~55-line categorical persona. Zero named tools, libraries, or CONUS assumptions. Six capability layers enumerated abstractly. Discoverable via enumeration calls.
- **`sync_research_context` deprecated** in favour of `get_session_raw_state` + `write_research_interpretation` (G1: LLM authors interpretation, Python returns raw state). Old tool aliased with `DeprecationWarning`; removal planned for 2.0.
- **`get_library_reference`** — added no-arg branch (returns catalog of all available references, fixing R6 discoverability gap). Single-arg callers unaffected. Now delegates to `resources._load_card` for consistent drift-warning behaviour.
- All 8 built-in library reference cards gain `version_compatible` semver range field.

### Deprecated
- `sync_research_context`: use `get_session_raw_state` + `write_research_interpretation`. Will be removed in 2.0.

---

## [1.5.2] — 2026-04-23

### Removed
- Deleted dead `ai_hydro/workflows/` stubs (`__init__.py`, `compute_signatures.py`, `fetch_data.py`, `modeling.py`) that referenced the pre-refactor module layout.

### Changed
- Removed remaining legacy `Tier 2` / `Tier 3` wording in live package and test surfaces.
- Removed stale `ai_hydro.tools.hydrology` and `ai_hydro.tools.watershed` references in live package and test code.

---

## [1.5.1] — 2026-04-19

### Added — Raster map support
- **`plot_raster_tile(array, bounds_wgs84, output_dir, name, colormap)`** in `analysis/plots.py` — clean, decoration-free PNG export (transparent NoData, P2–P98 colour clipping) for use as a deck.gl `BitmapLayer` image.
- **`push_raster_layer()`** in `map_events.py` — raster variant of the map event writer; stores PNG path + WGS84 bounds so the TypeScript watcher can base64-encode the image at pick-up time.
- **`_bounds_to_wgs84(bounds, crs_str)`** in `tools_analysis.py` — reprojects raster bounds to EPSG:4326 via pyproj; silent fallback for geographic CRS or missing pyproj.
- **`compute_twi` raster push** — after TWI computation, a `viridis_r` tile is pushed as layer `twi_<session_id>`.
- **`create_cn_grid` raster push** — `YlOrRd` CN tile pushed as `cn_<session_id>`.
- **5 new raster tests** in `TestRasterMapEvents`.

### Added — Vector map support
- **`ai_hydro/mcp/map_events.py`** — Python → VS Code map bridge. `push_layer()` writes a JSON event file to `~/.aihydro/map_events/` which the extension's `MapEventWatcher` picks up and renders. `push_gauge_point()` helper for single-station markers. Four style presets: `watershed`, `flowlines`, `gauge`, `default`.
- **`show_on_map` MCP tool** — new tool (10th analysis tool, 29th total) to push any GeoJSON string to the AI-Hydro map panel directly from an agent session. Validates GeoJSON, applies style presets, returns `ok`/`layer_id`/`message`.
- **`delineate_watershed` auto-push** — after every successful watershed delineation the boundary polygon and gauge point are pushed automatically; map panel opens side-by-side.
- **7 new tests** in `tests/test_mcp_integration.py` covering `push_layer`, style presets, overrides, dict input, error handling, and `show_on_map` smoke + invalid-JSON rejection.

---

## [1.5.0] — 2026-04-18

### Added
- **`ai_hydro/citations.py`** — three-tier BibTeX citation registry. Tier 1: per-tool data source citations (USGS NWIS, NHDPlus, 3DEP, GridMET, NLCD, POLARIS, CAMELS-US, HBV). Tier 2: platform citations (AI-Hydro + aihydro-tools Zenodo DOIs) always included. Tier 3: plugin citations via `register_plugin_citation(key, bibtex, tool_names)`.
- **`HydroSession.add_citations(keys)`** — accumulates citation keys per tool call (no extra save).
- **`HydroSession.export_bibtex()`** — builds a ready-to-use `.bib` string from accumulated keys; `cite_all()` is a backward-compat alias.
- **`_citations` field** persisted in session JSON and restored on `load()`.
- **`sync_research_context`** Phase 2 now writes `citations.bib` to the workspace alongside `research.md`.
- **`export_session`** includes BibTeX in all export formats.

### Fixed
- **Shadow `ai_hydro/session.py` deleted** — was unreachable (Python prefers `session/` package) but wrote to `.clinerules/research.md` if ever imported directly; removed the confusion.
- **`session/persona.py`** path corrected: `.clinerules/research.md` → `.aihydrorules/research.md`.
- **`mcp/helpers.py`** `_session_store()` now accepts `tool_name=` kwarg; auto-adds citations in the same `session.save()` call as slot data (zero extra I/O).
- **`mcp/tools_analysis.py`** — all 10 `_session_store()` calls updated with `tool_name=` for citation tracking.
- **`mcp/tools_modelling.py`** — HBV citation (`seibert2012hbv`) added after model training.
- **`mcp/tools_docs.py`** docstring: `.clinerules/tools.md` → `.aihydrorules/tools.md`.
- **`workflows/camels_extraction.py`** — `extract_camels_attributes` renamed to `fetch_camels_attributes` (stale name removed from codebase).

---

## [1.4.0] — 2026-04-17

### Removed
- **`extract_camels_attributes` tool** — the incomplete per-site CAMELS-like attribute
  extractor has been removed from the public tool set. A dedicated `camels-attrs` MCP
  server will be released as a community plugin via the `aihydro.tools` entry point.
- **`[camels]` extra** (`camels-attrs>=0.1.0`) removed from `pyproject.toml`.
- **`_get_camels_attrs_version()`** removed from `tools_docs.py`.

### Changed
- **Tool count: 28 → 27.** `list_available_tools` now returns exactly 27 built-in tools.
- **CAMELS-US benchmark data is unaffected.** The 671-gauge CAMELS-US dataset continues
  to be fetched internally by `train_hydro_model` via `fetch_camels_streamflow()` for
  HBV and LSTM training — this is a data source, not a user-facing tool.

### Note for upgraders
If you were calling `extract_camels_attributes` directly, remove those calls.
Static catchment attributes for any USGS gauge are available from `delineate_watershed`
(morphometric) and `extract_geomorphic_parameters` (DEM-derived). A full CAMELS-attribute
extractor will be available as a separate plugin package.

---

## [1.3.0] — 2026-04-16

### Added
- **Python env context in `start_session`** — response now includes:
  - `mcp_python`: path to the interpreter running the MCP server
  - `mcp_pip`: corresponding pip path
  - `available_packages`: `{name: version}` dict for all installed packages
  Agents use this to write correct Python scripts without guessing interpreter paths
  or assuming what is installed.
- **`list_available_tools` tool** — returns all registered MCP tools at runtime
  with names, descriptions, and parameter schemas. Includes community plugin tools.
  Call this instead of relying on documentation for an accurate picture of capabilities.
- **`get_library_reference` tool** — per-library reference cards for 8 core hydro
  libraries covering field-name gotchas, unit assumptions, CRS requirements, and
  copy-paste code patterns. Prevents hallucination in generated scripts.
  - `pynhd` — NLDI watershed polygons and NHD data
  - `pygeohydro` — USGS NWIS streamflow and NLCD land cover
  - `pygridmet` — GridMET daily climate (precipitation, temperature)
  - `py3dep` — 3DEP elevation (DEM) access
  - `hydrofunctions` — simple NWIS streamflow client
  - `pysheds` — DEM-based flow direction, accumulation, TWI
  - `rasterio` — raster I/O, masking, reprojection
  - `xarray` — N-dimensional labeled arrays for gridded data
- **`ai_hydro.knowledge` module** — hosts built-in library reference cards as
  structured JSON. Located at `ai_hydro/knowledge/library_refs/*.json`.
- **`aihydro.knowledge` entry point** — community plugins can contribute additional
  library reference cards by registering a `get_refs_dir` callable:
  ```toml
  [project.entry-points."aihydro.knowledge"]
  my_lib = "my_package.knowledge:get_refs_dir"
  ```
  where `get_refs_dir()` returns a `pathlib.Path` to a directory of `*.json` files.
- **Agent instructions** updated with explicit Python scripting decision tree:
  call `start_session` → call `get_library_reference` → use `mcp_python`.
  Also fixed `.clinerules/` → `.aihydrorules/` path reference.

### Changed
- **`session_id` / `gauge_id` separation** — `session_id` is now a free-form research
  identity (any string); USGS gauge IDs are a separate `gauge_id` parameter on the two
  USGS-specific data tools only (`delineate_watershed`, `fetch_streamflow_data`). All
  analysis tools (`extract_hydrological_signatures`,
  `compute_twi`, etc.) take `session_id` only and operate on whatever data is cached.
  - Backward-compatible: sessions where `session_id` looks like an 8-digit USGS gauge
    still resolve correctly via the `_resolve_usgs_gauge()` helper.
  - Stored sessions now track `site_id` (e.g. `"01031500"`) and `site_type` (`"usgs_gauge"`)
    separately from the session identifier.
- **`add_gauge_to_project` renamed to `add_session_to_project`** — parameter renamed from
  `gauge_id` to `session_id`; old name remains as a backward-compat alias.
- **`ProjectSession.session_ids`** — replaces `gauge_ids`; backward-compat `gauge_ids`
  property kept for existing project JSON files.
- **`fetch_streamflow_data` now uses `dataretrieval`** instead of `hydrofunctions`.
  Fixes a `pd.Timedelta(Day)` incompatibility with pandas ≥ 2.2 on Python 3.13.
- Tool count: 26 → 28 (added `list_available_tools`, `get_library_reference`)

---

## [1.2.0] — 2026-04-10

### Added
- **ProjectSession** (`ai_hydro.session.project`): project-scoped research state
  that spans multiple gauges, topics, and literature — not tied to a single USGS gauge.
  Storage: `~/.aihydro/projects/<name>/project.json`.
- **ResearcherProfile** (`ai_hydro.session.persona`): persistent researcher persona
  built from agent interactions over time, analogous to memory features in Claude.ai
  and ChatGPT but domain-specific to computational hydrology.
  Storage: `~/.aihydro/researcher.json`.
- **10 new MCP tools** (26 total):
  - `start_project` — create or resume a named research project
  - `get_project_summary` — overview: gauges, journal, literature, metrics
  - `add_gauge_to_project` — associate a USGS gauge session with a project
  - `search_experiments` — full-text search across all gauge sessions in a project
  - `index_literature` — scan a folder of PDFs/txt/md → build searchable index
  - `search_literature` — query the index; returns excerpts for LLM synthesis
  - `add_journal_entry` — log a timestamped experiment note to the project journal
  - `get_researcher_profile` — return the persistent researcher persona
  - `update_researcher_profile` — update profile fields (agent or user driven)
  - `log_researcher_observation` — agent logs an observation about the researcher
- **Folder-based literature mode**: no vector database, no embeddings. Drop
  PDF/txt/md files into the project `literature/` folder, call `index_literature`,
  then `search_literature`. LLM synthesizes from plain-text excerpts.
- **Cross-session experiment search**: `search_experiments` queries stored results
  across all gauges in a project — "show me all basins where I ran LSTM",
  "which gauges have BFI > 0.6".
- **Researcher profile in research.md**: `HydroSession.write_research_context()`
  now appends the researcher profile block to `.clinerules/research.md` so the
  agent has persona context in every conversation automatically.
- **Updated agent instructions** in `app.py` documenting the full memory hierarchy:
  `ResearcherProfile → ProjectSession → HydroSession → research.md`.

### Changed
- `ai_hydro/session/__init__.py`: exports `ProjectSession` and `ResearcherProfile`
  alongside `HydroSession`.
- `ai_hydro/mcp/__init__.py`: imports `tools_project` module on startup.
- Agent system prompt rewritten to reflect v1.2 architecture and memory layers.

---

## [1.1.0] — 2026-03-31

### Added
- Published to PyPI as `aihydro-tools` (`pip install aihydro-tools`).
- Console script `aihydro-mcp` → starts MCP server on stdio.
- Plugin entry-point system: `[project.entry-points."aihydro.tools"]` for
  community tool registration via pip packages.
- `--version` / `--diagnose` CLI flags.
- `python -m ai_hydro.mcp` fallback entry point.

### Removed
- **RAG system** (`rag/`, `registry/`, `knowledge/` directories): removed due to
  heavy dependencies (chromadb, sentence-transformers) and immaturity.
  Archived at `github.com/AI-Hydro/aihydro-rag`.
- `query_hydro_concepts` MCP tool (was part of RAG system).
- `[rag]` extras in `pyproject.toml`.

### Changed
- All hardcoded "17 tools" references replaced with generic language throughout
  docs, tests, and setup scripts — tool count is now dynamic.
- Version bumped from 1.0.5 → 1.1.0.
- `setup_mcp.py`: updated expected tool set, docstrings, print output.
- `tests/test_mcp_integration.py`: tool count assertion now uses
  `len(EXPECTED_TOOLS)` instead of hardcoded 17.

---

## [1.0.5] — 2026-03-28

### Added
- `--diagnose` / `--check` flag to `aihydro-mcp` CLI for verifying server health.
- `python -m ai_hydro.mcp` module entry point as fallback for environments where
  the console script is not on PATH.

### Fixed
- Box Drive read-only filesystem workaround: `os.chdir(~/.aihydro/cache/)` at
  server startup prevents write errors on macOS with Box Drive sync.

---

## [1.0.4] — 2026-03-27

### Fixed
- MCP server registry and session import path corrections after Phase 2
  modularisation.
- `setup_mcp.py` dual-mode detection: prefers `which aihydro-mcp` (pip install),
  falls back to `python mcp_server.py` (monorepo dev).

---

## [1.0.3] — 2026-03-26

### Added
- `export_session` tool: exports session as JSON, BibTeX, or plain-text methods
  paragraph. Saved to disk (not returned inline) to preserve context window.
- `sync_research_context` tool: refreshes `.clinerules/research.md` and
  `.clinerules/tools.md` from live server state.

---

## [1.0.2] — 2026-03-25

### Added
- Phase 2 MCP server modularisation: monolithic `mcp_server.py` split into
  `tools_analysis.py`, `tools_session.py`, `tools_modelling.py`, `tools_docs.py`,
  `app.py`, `helpers.py`, `registry.py`.
- Plugin discovery via `importlib.metadata.entry_points(group="aihydro.tools")`.

---

## [1.0.1] — 2026-03-24

### Added
- Phase 1 package restructure: `ai_hydro/` now organized into `core/`, `data/`,
  `analysis/`, `modelling/`, `session/` subpackages.
- `HydroSession` dynamic slot system: plugins can add custom result slots without
  modifying core code.
- `HydroSession.write_research_context()`: auto-writes `.clinerules/research.md`
  on every session save.
- `cite_all()`: generates combined BibTeX for all computed session results.

---

## [1.0.0] — 2026-03-20

### Added
- Initial release: 16 MCP tools across analysis, session, and modelling.
- Data tools: `delineate_watershed`, `fetch_streamflow_data`, `fetch_forcing_data`,
  `extract_camels_attributes`, `create_cn_grid`.
- Analysis tools: `extract_hydrological_signatures`, `extract_geomorphic_parameters`,
  `compute_twi`.
- Session tools: `start_session`, `get_session_summary`, `clear_session`,
  `add_note`, `export_session`, `sync_research_context`.
- Modelling tools: `train_hydro_model` (HBV-light + LSTM), `get_model_results`.
- `HydroSession`: per-gauge persistent research state at
  `~/.aihydro/sessions/<gauge_id>.json`.

---

[Unreleased]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.0.5...v1.1.0
[1.0.5]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/AI-Hydro/aihydro-tools/releases/tag/v1.0.0
