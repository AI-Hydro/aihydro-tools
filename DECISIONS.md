# Decisions

## 2026-10-30 - One period-completeness rule for period statistics (tool audit T1-P)

**Context.** SPI over a partial month, annual maxima over partial years and a 7-day low flow spliced across a gap each
returned a number for a quantity the record did not support (SPI-1 -3.09, Q100 +44%). Each tool had its own grouping.

**Decisions.**
1. **One helper, in aihydro-watershed** (`signatures.periods.complete_periods`), used by SPI/SPEI, annual maxima and 7Q10. It
   returns the complete periods, the excluded ones with observed/expected days, and a mask. The tools report the excluded periods.
   It lives in aihydro-watershed because the kernels do and the dependency direction is downward; aihydro-core would have
   forced a core release first. It closes the calendar-year and spliced-window parts of D26 in the kernel (the tools expose `year_start_month`); the baseflow splice remains open in D26. It does not touch D18 (`_temporal_coverage` counting NaN values) or D27.
2. **Thresholds.** Months: every day present (1.0) is SOURCED: WMO-No. 1203 (2017) s4.4.1(c), p.7: "A monthly value for a sum parameter
   (for example, total precipitation) can only be calculated if there are complete data over the month" (exceptions: estimated or
   accumulated data within the 11-day and 5-consecutive-day limits); WMO-TD 341 (1989), PDF p.6: "Monthly totals should be based on a
   full month's data". The SPI User Guide (WMO-No. 1090) marks missing months -99 (PDF p.16), asks for "at least 20-30 years" (PDF p.9),
   has no within-month rule, and calls tolerance "a subjective decision" (PDF p.9). Years: 0.95 of days is an ENGINEERING CHOICE, not
   sourced (the package's water-balance fraction); the 7Q10 window fraction (0.95), the 6-season minimum for the seasonal trend and the
   stricter SPEI calendar rule are engineering choices too, signed off by the coordinator and labelled as such. Bulletin 17C treats
   missing years as broken or incomplete records (pp.11-12) and represents them with flow intervals and perception thresholds in EMA
   (Appendix 3); it states no daily-coverage threshold. All thresholds are arguments and are echoed in the results.
3. **Conventions, verified.** Water year 1 Oct-30 Sep labelled by the year it ends: Bulletin 17C (TM 4-B5) Appendix 10, p. 104. Climatic
   year 1 Apr-31 Mar: USGS SIR 2006-5035 ("designated by the calendar year in which it begins"); SIR 2008-5126 section 3 uses April 1 to
   March 31 for 7-day minimums. Bulletin 17C defines the annual flood series
   from the INSTANTANEOUS peak and allows the maximum daily mean as an alternative; the tool says its series is the daily-mean
   maximum, and `map_flood_inundation` carries that statistic with the discharge. `lp3` here is Pearson III on the logs by moments, not the
   Bulletin 17C procedure.
4. **Excluded, not imputed.** An incomplete period is dropped and named. Missing precipitation is never zero. An accumulation
   window containing an excluded month is NaN.
5. **Trend (D07):** sub-annual series use the seasonal Kendall test and seasonal Sen slope (Hirsch, Slack & Smith 1982), season = calendar
   month; Theil-Sen + Mann-Kendall is for yearly series only. Unsupported series (a season with < 2 values, < 2 cycles) return
   None + reason, not a number. Months with at least 2 values are used (`seasons_used`); the test is refused if fewer than 6 remain (an
   engineering choice, flagged). The p-value includes the Hirsch & Slack (1984) between-season covariance, with the form
   sigma_gh = (K_gh + 4*sum(R_ig*R_ih) - n*(n+1)^2)/3 over years present in both seasons; it was validated by SIMULATION (a test: size near
   nominal under AR(1) 0.6 and a shared year effect, where the independent-seasons test is 0.30 and 0.43) and NOT checked against the paper's
   text, which I could not read. Long-term persistence and short records are not handled. A yearly series that is clipped at either end is
   dropped (reported). A missing optional package must not change the statistic; `pymannkendall` is dropped.
5b. **Mean annual totals (D11):** the sum over the 12 calendar months of (mean of finite days x days in month); None + reason if a
   month has no data; aridity on the same basis for P and PET. A plain mean over the finite days was +48% (dry season missing) or -48%
   (wet season missing) in the review's test. The estimator is a MONTHLY MEAN: a year-month is a valid monthly
   mean when it has at most 10 missing days and no run of 5 or more (WMO-No. 1203 s4.4.1(a), the sourced definition for a mean parameter; the
   run test is implemented as longest run < 5); a calendar month's value is the mean of its valid monthly means, and it must be valid in at
   least 80% of the record's years (s4.4.2, defined for climate normals, applied here by analogy; disclosed). The wet-day frequency uses the
   same basis as a ratio per month (s4.4.1(d)). Mean annual temperature and `calculate_water_balance_metrics`
   use the same basis.
5c. **Days a source never holds:** GridMET (pygridmet) and Daymet have no 31 Dec in leap years; the kernel takes them out of expected
   days (`calendar`) and reports it, so 100% for months does not drop every leap December. Real gaps still exclude. The tool applies the
   calendar only when EVERY column the statistic uses (precipitation for SPI; precipitation and PET for SPEI) comes from such a product,
   read from `variable_provenance`; CHIRPS precipitation with GridMET PET gets no calendar, so a real CHIRPS gap is not forgiven. No product
   key, no calendar.
6. **Inclusive end (D10):** `filterDate` end is exclusive; one helper `filter_end` adds a day at every call, in aihydro-data and in
   the tools repo's own copy of the GEE code (a test keeps them in step). Daily-product edge tolerance is 0 (a pygridmet or Daymet
   31 Dec leap-day absence stays disclosed-only).
7. **Sub-daily (D12) is refused, not resampled.** Resampling would be a silent choice of aggregation; the error says how to fetch daily.
   The guard also checks the date spacing, because the slot's `interval` label was itself wrong. It now lives in one loader used
   by baseflow, FDC and flood frequency; `extract_hydrological_signatures` applies the same guard to the slot it consumes.

**Left for Wave T2 (for the coordinator to move into `docs/vision-2040/plans/tool-integrity-2026-10.md`):** partial-year maxima as
censored bounds (rather than excluded); a flood-season span rule (a year can be 95% complete and miss its flood season); the USGS
peak-flow file as the Bulletin 17C input (instantaneous peaks, EMA); accumulation-flag (approval/qualifier) handling in the annual
statistics; the long-scale SPI trade-off (SPI-12/24 under a 100% month rule loses windows to every gap); flag a year whose minimum
7-day window abuts an unevaluable window, since that minimum is an upper bound.

**Not done / open.** The `latest` SPI value is the last COMPLETE month; the tool does not extrapolate. SPI minimum calibration
length (D27) and `_temporal_coverage` NaN counting (D18) are separate findings. The streamflow loader
(`_load_daily_streamflow`) is shared by baseflow, FDC and flood frequency, and the signatures tool applies the same guard; a general
study-level series loader remains with the study-integrity workstream.
## 2026-10-30 - Same-gauge re-delineation keeps the gauge record (T1 review follow-up, basin identity)

A new outline of the same gauge dropped the gauge's streamflow, because the stored stamp carried the old polygon digest. Two options were on the table: keep streamflow when the gauge is unchanged, or normalise the geometry digest (round to about 1e-6, canonical ring start). **Chosen: judge streamflow on the gauge alone.** The digest is the key every existing stamp and registered-feature cache entry was computed with; normalising it would change that identity and turn every stored stamp into a mismatch or an unverified read. Streamflow is an observation at a station, so the gauge is its true identity; everything computed from the polygon (geomorphic, forcing, TWI, ...) is still dropped. A stamp with no gauge is still judged on the geometry. The other option stays open if polygon-derived slots should survive sub-metre redraws.

## 2026-10-30 - One study resolver, one claim lifecycle, earned pre-registration (tool integrity T1-S)

Source: the record audit (`docs/vision-2040/findings/tool-audit-record-2026-10-30.md`), R-01..R-07, R-29, R-32.

1. **Only explicit tools create a study.** `HydroSession.load` still returns an empty session for an unknown id (callers that create
   rely on it), so the guard is at the tool boundary: `resolve_existing_study` / `_resolve_session(..., must_exist=True)`. It is a separate
   flag, not implied by `allow_auto_create=False`, so the data-fetch and series tools (other lanes) are untouched. `session_id` stays a required
   argument on the ledger tools; making it optional (R-36) is a schema change left for its own PR. The GeoWorkspace map tools
   (`map_set_roi`, `map_set_working_geometry`, `map_save_roi`) deliberately open a study from a drawn region and are allow-listed in the scan test.
2. **One code.** The existence refusal is `STUDY_NOT_FOUND` everywhere, including `list_claims`/`list_assumptions` (was `SESSION_NOT_FOUND`).
3. **Retraction propagates without an approval record.** Retracting narrows what the registry asserts; it grants nothing. Everything that
   widens it (a `promoted` row, including the return from `stale`) still needs the human `aihydro-approve` record and
   `promote_claim_to_registry`. A retracted registry row is never reopened: a re-promotion writes a new row, and the study claim cannot return to
   a supporting status (`retracted_claim_gate`). A human-only retract CLI (audit R-03) is not added: it would only reach what the author's own
   status change now reaches.
4. **Leaving `stale` requires re-bound evidence, not a rationale.** The gate compares the sources recorded at detection (`stale_sources`, on
   the claim and in the registry row) with the evidence the claim now cites. A claim stale without recorded sources falls back to its previous
   spans. The gate is on `supported`/`weakly_supported`; downgrades are always allowed.
5. **Study claims keep their registry link across a redefinition** (`promoted`, `registry_id`, `promoted_at`, `staleness_detected_at`,
   `stale_sources`). These keys are outside the revision digest and the approval record, so keeping them grants nothing.
6. **Post-hoc is judged on the planned analyses only.** The audit proposed counting all runs existing at lock; that would label nearly
   every real study post hoc (delineation and data fetch always precede the plan). A plan is `post_hoc` when a run or slot named EXACTLY by a
   planned analysis predates `locked_at`. Matching is by normalised exact name; descriptions that are not tool names never match. Plans
   already on disk without the stamp are judged from their timestamps at report time.
7. **Citation not-found requires every provider to answer.** A DOI is `not_found` only if CrossRef, DataCite and Semantic Scholar all
   answered without a record whose DOI matches; any unreachable provider makes the answer `unavailable`. The free-text threshold (0.6, 0.8 for
   high confidence) is a heuristic on title words, not a verification; a hit above it is still a lookup result for the agent to cite verbatim.
8. **Deferred:** `equations.yaml` content (R-06), optional `session_id`/`study_id` alias (R-36), `session_note` when an explicit id differs from
   the bound study, the remaining non-atomic writes (R-18/R-41, T2), three-state check results (T2).
## 2026-10-30 - A coordinate pair has no axis order: refuse it at one boundary; the gauge locates itself

Owner evidence: `geometry=[-68.5828, 47.2375]` (lon, lat; USGS 01013500, northern Maine) was read by aihydro-data as (lat, lon), a point
in the Southern Ocean. With `gauge_id` the call was refused as "region 'global'"; without it nothing refused and the router served CHIRPS /
ERA5-Land / GEOGLOWS for the wrong place, real-looking numbers for the wrong location. That second case is the defect that matters.

1. **Refuse, do not normalise (even when only one reading is possible).** When both values are within +/-90 the pair is genuinely
   ambiguous (`GEOMETRY_AXIS_AMBIGUOUS`: both readings and the exact fix are returned, nothing is guessed). When one value exceeds 90
   only one reading is valid (`GEOMETRY_AXIS_SWAPPED`). Normalising that case would have been correct, but it makes the accepted
   inputs depend on the data, so the same call shape works for Seattle and silently fails for Maine. One rule is easier for a weak model
   to learn: bare pairs are never accepted; send `{"lat":..,"lon":..}`. The error is retryable (`terminal: false`) and its `recovery`
   text is the corrected argument. The refusal costs one turn; a wrong-place fetch costs a wrong result.
2. **One boundary, not per-tool logic.** `GeometryBoundaryMiddleware` checks any call that supplies `geometry` / `geometries`, so tools
   registered by other packages (`data_validate_request`, `data_batch_fetch`) are covered with no change there; the retaining
   `data_fetch` and `data_fetch_background` run the same `check_geometry_arg` for direct Python calls. A scan test lists the live tool
   schemas and fails if any geometry-taking tool is not behind it. Out of scope, and still bare-pair readers: aihydro-data's
   `outlet=(lat, lon)` parameter and the `*_geojson` string parameters (GeoJSON is unambiguous).
3. **Gauge first.** A USGS site number determines a place. `geometry` is optional when `gauge_id` (or a verified session BasinRef alias,
   or a session USGS `site_id`) names it; the point is the Water Data API monitoring-location record. ADR-003 A2 is untouched:
   a gauge request is still `mode="manual"`, `product="NWIS_STREAMFLOW"`, `fallback=[]`; nothing here can send it to a modelled
   product. A geometry given with the gauge must be within 5 km of it (the A2 `max_distance_m` pre-registered threshold, read from
   aihydro-data, not a new number); otherwise `GAUGE_LOCATION_MISMATCH` names both places. If the location lookup fails, only that
   check is skipped (the pinned fetch cannot return another place's data); a gauge-only call whose lookup fails returns a transient error.
4. **Deprecated tool.** `fetch_streamflow_data` takes `start_date`/`end_date`. Rather than adding parameters, the existing arg-repair
   alias table gets the reverse pair (`start`->`start_date`, `end`->`end_date`), which applies only when the tool has no `start`/`end`.

5. **Same validator for every coordinate (audit R-11, D43).** Selection is by parameter name, not a tool list. A bbox whose four
   values are all within +/-90 is as ambiguous as a pair (`[47,-69,48,-68]` is a lat-first box in Antarctica or a lon-first box in
   Maine), and a range check cannot tell them apart, so bare 4-lists are refused unless a value beyond 90 settles the axes; the
   dict `{"west","south","east","north"}` is the explicit form. GeoJSON is trusted to be [lon, lat] (the spec), but each position is
   range-checked, which catches projected metres; a lat-first GeoJSON position inside +/-90 is indistinguishable from a valid one and
   is NOT detected.
6. **`outlet` is a coordinate too (review of the first version).** Range-checking it was not enough: a swapped in-range outlet
   (`[-68.58, 47.24]`) passes any range check, and GEOGLOWS / Open-Meteo snap to the outlet ahead of the polygon, so a Maine
   polygon returned modelled flow for the Southern Ocean. Now (a) a bare outlet pair is refused like a geometry pair, the explicit
   form is `{"lat","lon"}` and the boundary converts it to the `[lat, lon]` list aihydro-data's signature takes; (b) when an outlet and a
   geometry are both present (single call, `data_fetch`, and `data_batch_fetch` per label) the outlet must be inside the geometry or
   within the A2 5 km tolerance of it, else `GEOMETRY_OUTLET_MISMATCH`.
6b. **Where the outlet is converted.** Only the middleware converts `{"lat","lon"}` to `[lat, lon]` (before schema validation); it then
   sets `BOUNDARY_CHECKED` for the call so the wrapper does not read the converted pair as a caller's bare pair. A direct Python call
   leaves it unset and the wrapper runs the same functions. The outlet is not compared with a station-id geometry (that routes to an
   observation product) or with a `{label: outlet}` mapping (no consumer). A geometry spanning more than 180 degrees of longitude is
   refused rather than measured in planar lon/lat; the outlet is wrapped to the geometry's side of +/-180.
7. **Residual, made visible instead.** A GeoJSON Point, WKT or in-range polygon with swapped axes is a valid place and cannot be
   detected. Every `data_fetch` result (and the `data_fetch_background` reply) therefore carries `resolved_place`: `{lat, lon, region}`
   for a point, `{bbox, centroid, region}` otherwise, `{gauge, region}` for a station id; it is also in the sealed call inputs. When the
   session's `site_id` or BasinRef names the place, a top-level `session_note` says so (and is prepended to `notes`).

Consequence for callers: `data_fetch(variable, start, end, geometry=None, ...)` reordered its Python signature; MCP callers are unaffected.

## 2026-10-30 - Basin identity is a stamp on the result, checked by the one session loader (tool integrity T1-B)

**Context.** Audit D01-D05/D33: the result cache key held no geometry for the session basin, a watershed change invalidated
nothing, and `site_id` plus the streamflow slot survived it, so a second delineation in a used session was answered with the
first basin's TWI, gauge and flow. `feature=` was resolved leniently (unknown ids fell back to the session basin).

**Decisions.**
1. **Stamp the result, not the key.** Each basin-dependent result carries `meta.basin_identity = {geometry_digest, gauge_id}`;
   the digest is `content_hash` of the bare geometry, the one the registered-feature keys already used (`_feature_geom_key` now
   delegates to it, so existing keys do not change). A key-only fix would leave `get(slot)` consumers (streamflow, forcing,
   geomorphic ...) unprotected; one stamp is checked by `get_result` (cache) and `get` (slots).
2. **The loader is `HydroSession`, not a per-tool helper.** `put_result` stamps, `get_result` serves verified only, `get` skips
   stale. A scan test fails if any module reads `_slots` around it or writes an unclassified slot. T1-P's `load_series` can sit on
   top of it.
3. **Unstamped means unverified, not wrong.** Cache lookups miss (recompute); slot reads still work and are flagged
   (`summary()["basin_identity"]`, tool results, `meta.basin_identity_inputs_unverified` on anything derived from them, so a stamp
   is never laundered). Refusing them would break every session on disk; trusting them is the bug.
4. **A watershed change is the invalidation event.** Replacing the session watershed with a different digest drops the
   `__legacy__` results of every dependent slot, clears `site_id`/`site_type`/`site_name` unless the new watershed names the same
   gauge, and appends to `extra["basin_changes"]`. Run-log rows (sealed evidence) are not touched. `camels` and `model` are exempt:
   multi-basin by design.
5. **`feature=` is strict everywhere** via `_feature_cache_resolve` -> `_resolve_registered_feature`; raw GeoJSON strings are no
   longer registered on the fly by these tools (use `register_feature`). A raw `geometry_geojson` that is not the session basin has
   no gauge, area or flow: signatures refuse it rather than borrow the session's (D05).
6. **Out of scope, not changed:** the geometry validator and `data_fetch` gauge handling (`vision2040/geometry-gauge-first`);
   period statistics and `load_series` (T1-P).

## 2026-10-30 - Data codes are exempt by keyword, not by shape (answer audit)

Owner evidence: an answer was audited at "0/13 numbers traced", and two of the hard findings were the USGS parameter code `00060` and
statistic code `00003` in "parameter 00060, statistic 00003 (daily mean)". A metric word within 40 characters ("daily mean") made
them `quantity`.

1. **Keyword plus shape, both required.** Exempt (reason `data_code`) only a number that is exactly 5 digits with a leading zero,
   with a code keyword directly before or after it. Shape alone would exempt real values ("flow 00412"); a keyword alone would
   exempt "parameter 412". The metric-word proximity rule stays and cannot override it, because the keyword names the number a label.
2. **Never when quantity-shaped.** A unit, percent, decimal or sign leaves the number a quantity. A metric-value reading
   ("discharge 00060", "discharge 00060 cfs") has no code keyword and so is not exempt.
3. **HUC/EPSG/FIPS/NLCD were already exempt** (mask reason `code_label`); only tests were added.
4. **Open, not changed here:** "7,305 daily values" classifies as an exempt `bare_integer`, not a `sample_size`, because "value" is
   not in the sample-noun list. Adding it would tighten the audit for other answers, so it is left for a separate decision.

## 2026-10-29 - Map provenance is stamped by the server and the claim link carries no status (UX program Wave 1, T-M1)

**Context.** Layers reach the host through a file drop, so the host cannot tell which call produced one. The old
`map_add_layer_from_run` parsed the study JSON for a run log that now lives in SQLite and took geometry from summary values.
Claims change status after a layer is on the map.

**Decisions.**
1. Python stamps `chat_id`, `study_id`, `_run_id`, `tool`, `source`, `retrieved_at` inside the `push_*` functions; the host only adds
   `project_id` and the item id. A missing value is omitted, never blank, so the host can say "no run record" honestly.
2. A layer pushed before its run row exists reserves the row's id on the call capture; `post_run` and the run recorder store the row
   under it. The alternative (patch the event file after the call) races the host watcher.
3. `map_add_layer_from_run` takes geometry only from a recorded artifact and refuses one whose digest no longer matches. A layer must
   not claim to be a run's output when the file was edited afterwards.
4. `bind_claim_to_item` stores the pair of ids only. Claim status is read from the claim at the time it is asked; storing it on the
   item would go stale.
5. The ContextVar does not survive into `data_fetch_background`: the job is a `Popen` subprocess. `chat_id`, `study_id` and
   `workspace` are captured at submit time into the job config and echoed in the status.
6. Until T-P1, a project is `dir:<sha256(realpath)>` behind one helper (`project_key`). `project_key.py` switches to the
   `prj_...` id once T-P1 lands; callers treat the key as opaque. Job status carries the key, not the absolute workspace path.
7. `map_get_state` accepts a caller-supplied `chat_id` and `workspace`. This is acceptable because the server is a single-user,
   local process: the caller is the user's own agent, and there is no other principal to isolate.
8. `map_add_layer_from_run` treats the manifest `path` as data: the real path must lie inside the study workspace
   (`ARTIFACT_OUTSIDE_WORKSPACE`), the file is read once and the same bytes are hashed and parsed, and the result says
   `hash_checked: false` when no digest was recorded.
9. A reserved run id is stored as a row under its own study even when the call's results end up in another study, so a
   layer's `_run_id` never dangles.


## 2026-10-29 — Active-study context resource; `research.md` becomes a stub (UX Wave 1, T-R1)

1. The study digest moves from `.aihydrorules/research.md` (loaded by the extension into every prompt, rewritten under the
   agent) to `aihydro://project/{prj}/context?chat=`, fetched by the host at task start and on change. The file becomes a
   one-release stub (M8) and is never rewritten once it is the stub, which closes the F-TS6 flip-flop for good instead of
   only at mid-task saves (T-C1).
2. The study shown is only the one bound to the chat and belonging to the project. No confirmed-default or recent-study
   fallback: a fallback would put a study the chat never chose into the stable prompt, the leak class this work removes.
3. Numbers are never remembered: results are re-read from the run log at call time and rendered with `[run:]` markers (B6).
   A line-level gate (`bare_numbers`, on the audit grammar) withholds anything else that carries a number, rather than
   trying to enumerate which free-text fields might. A coordinate study label therefore falls back to the study id. A
   `[lit:]` marker does not satisfy the gate: this section carries stored results only.
4. Free text is untrusted: profile fields, study label, site name/type/id and project title can be written by the agent
   (`update_researcher_profile`, `set_site_name`) and are neutralised (marker syntax defused) before the gate, so only lines
   built from run rows and ledger claims carry markers. The profile does not record who wrote a field, so it is described
   as stored fields, not "user-edited". The gate's plain-integer / long-digit exemptions hold only for the study's own site
   id. `?chat=` isolation is not authenticated: the resource is local and host-trusted, accepted as is.
   A non-generated `research.md` is backed up once (`research.md.bak-<ts>`) before the stub replaces it.
5. Not injected on purpose: notes, interpretation text (a pointer only; its numbers are already audited in the session, but
   the digest should not be a second copy), agent observations (consent-gated, T3) and memory (T3).
6. The long Citation Grammar block that lived in `research.md` is replaced by a short, number-free reminder in the context
   text; the audit tools (`audit_interpretation`, `write_research_interpretation`) report the exact violation. Risk: the worked example and the exemption list are no longer in the always-loaded prompt; if agents regress on citing, put the full grammar behind the same resource, number-free.

## 2026-10-29 — Project registry v2: confirmed default only, read-side duplicate grouping (UX Wave 1, T-P1)

1. A project default resolves a study only when a person confirmed it (`default_confirmed_by` = `user` | `host_launch`).
   The migration and an auto-create into a project with no default write a suggestion, which is offered as a candidate and
   never resolved (B4); otherwise the removed "most recent study" guess would return, scoped to a project.
2. Duplicate studies (same site id) are grouped when read and never merged: run logs are append-only, and merging is a
   decision for the owner (D8). Studies with no site id are not grouped.
3. Study labels are computed on read from the study index, not stored in `project.json`, so a rename of a site cannot leave a
   stale label. The file holds only the links (`study_id`, `added_by`, `added_at`).
4. The audience flag is data: the server serves `aihydro/audience = host` and does not refuse agent-originated calls; the host
   hides the five tools from the model and refuses them from it. Until it does, an agent that knew the name could confirm its own
   suggested default, so `list_available_tools` and `aihydro_describe_capability` filter on the flag (`agent_visible`, no tool
   names) and the discovery contract test exempts host-audience rows. Server-side refusal of agent-originated calls is still
   deferred to the host work; `describe_tool(name)` still resolves them.
5. Directory keys (`dir_key`) are the realpath, case-folded on darwin and win32 where the filesystem usually is
   case-insensitive. Trade-off: on a case-sensitive APFS volume two distinct directories differing only in case fold to one
   key and would share a project. Accepted as rarer than the moved-or-retyped-path case it fixes; revisit if reported.
6. The reserved `prj_` prefix is checked case-insensitively (a case-variant legacy name overwrote a registry file on APFS).
   Project default resolution reads only the call's own `_meta` workspace, never the machine-global `active_workspace.json`.
7. M3 cannot use a chat's cwd (the backend does not store it); bindings are matched through the study's `workspace_dir`.

## 2026-10-22 — Validators judge the series a computation used, bound by digest; the CAMELS reference is packaged (Science Wave 3, PR-4b, 2.8.0)

**Context.** Before 2.8.0 every streamflow validator read the session `streamflow` slot, which need not be the series
`extract_hydrological_signatures` consumed (a 2000-2020 slot beside signatures over 1989-2009 validated the wrong
years), and nothing ran the validators after signatures. The PR-4a claim gate that refuses "differs from CAMELS" claims
for forcing-dependent metrics was written against a shape no producer emitted yet.

**Decisions.**
1. The signatures wrapper retains the served series (`_served_series` from aihydro-watershed 0.2.0) as a
   content-named session data file, records `meta.retained_series = {path, digest}`, sets `data._data_file`, declares the
   file in the sealed record's `retained_files`, and pops the key before the result is cached, written or sealed.
   Slot health then reads the slot as `ok` by the rule PR-4a already had.
2. `record_length`, `usgs_qualification_codes`, `stationarity` and `regulated_basin` take `source="signatures"|"streamflow"`
   and an optional `series_digest`. A series that fails any digest check is `insufficient_data` / `series_mismatch`; they never
   fall back to another series. The four run inside the signatures call on the in-memory series (not through
   `register_post_validator`, whose builder only sees the session id) and are appended to `quality_flags`.
   Standalone `source="signatures"` calls resolve `session.get("signatures")` (active feature or legacy); a result stored
   under `__override__` (an explicit `geometry_geojson=`) is not found there, and `series_digest` is the guard against
   a wrong resolution.
3. The CAMELS attributes v2.0 hydro, topo (`area_gages2`) and clim (`frac_snow`, `aridity`, `p_seasonality`) tables are packaged as
   verbatim files with provenance JSON and attribution (CC-BY 4.0, OQ8). The session `camels` slot overrides them when it holds
   the same gauge. The source used is recorded with its hash (S6). The soil, vegetation and geology tables are not packaged.
4. `reference_comparison` lives at one place, `key_outputs.reference_comparison` of the run row; the gate reads exactly there
   and the depth-4 search is gone. `camels_reference` is the summary block (source + hash, period, area, forcing, climate
   context, per-signature `can_claim_difference`).
5. The forcing gate uses aihydro-core `scope_excludes`: a cited interval whose scope does not VARY the forcing product
   (absent and malformed scopes included) excludes it. A cited metric with no interval entry at all is left to the uncertainty gate.
6. Run-row evidence compaction keeps short lists of strings inside the `uncertainty` block. Without this the interval
   `scope` (lists) was dropped from every real run row, and the PR-4a forcing gate could never fire on real output.
7. The signatures wrapper passes the slot's series WITH its dates. The slot drops days without a value, so its list is not
   consecutive from `start_date`; a bare list was read as consecutive days and shifted later values onto earlier dates.
8. Precipitation consumers key on `_precipitation.status` plus reason codes: `used_with_gaps` is usable when its only gap
   reason is `client_calendar_assumption` and no water year was excluded.
9. The capsule keeps a stdlib port of the Ladson 2013 kernel (the capsule stays free of the watershed package); a test
   cross-checks it against the watershed kernel to 1e-12, with gaps, short runs and dropped rows.

**Not done / follow-ups.** `qualifier_exposure` is only produced when watershed fetches the series itself; a series taken
from the slot has no flags frame there. `synopsis_for_llm` still lists degraded slots. The 671-basin table is
read once per process and cached.

## 2026-10-20 — Public CI does not exercise the modelling-backed and aihydro-lsh tools (private repos)

`aihydro-modelling` and `aihydro-lsh` are private repositories, so the public CI install
(CI installs core, data and watershed from PyPI via the pyproject pins; until 2.8.0 `.github/ci/install-siblings.sh` installed them from pinned SHAs) cannot include them.
Consequences: (1) tests needing the modelling package are skipped via the `needs_modelling` marker, so the
modelling-backed science tools (autoresearch, model-space, episode scoring) and the aihydro-lsh tools are not
run in public CI; without the package a modelling tool returns a structured `MODELLING_NOT_INSTALLED` error.
(2) `compare_series` is a base-kernel tool and carries its own `_kge` (parity with the package's version is
tested only where the package is installed). (3) The run-record coverage ratchet is an exact exempt-count
(`EXEMPT_RATCHET` in `tests/test_run_record_coverage.py`) over tools owned by this repo and declared deps, so
it does not depend on which optional plugins are installed (the old 0.55 fraction gave 99/173 with lsh vs
88/162 without). Follow-up: add a CI job with a read-only token for both private repos so these paths run on
every PR; then drop the skips. The sibling SHA pins are temporary until the next PyPI batch.

## 2026-10-28 — `write_research_interpretation` stops rewriting `research.md`; discovery answers are capped (Wave 0, T-C1/T-A1)

`write_research_interpretation` was one of the five `sync_context=True` call sites in the 2026-10-20 rule below and the only one that
fires mid-task in a normal run. The owner has retired `research.md` as an agent-facing surface (the mid-task rewrite included), so the
call now uses a plain `save()`; `research.md` is regenerated at `start_session` (and archive, shard merge, export). Consequence: an
interpretation reaches the next chat through the session (`get_session_summary`), not through the prompt file.

`list_available_tools` returned the full registry (135,896 characters, no arguments), and the server's own notes told the agent to call
it. Every answer is now capped at `LIST_TOOLS_MAX_CHARS` (4,000, serialised) and paged; one tool in full needs `name=`. The cap is a
property of the call, not of a tool list, so a larger registry (plugins) pages instead of growing.

Annotation vocabulary (`effect`, `requiresConsent`, `audience`) lives in the existing `tool_annotations.toml` rows and is served as
`_meta["aihydro/..."]`. It is data: the server never reads it to decide anything, hosts do. The vocabulary is closed and tested; a
`ui` row must pass a static scan (map/preview command writers only) and a runtime audit-hook run of every `ui` tool.
Definition of `ui`: the tool only queues a command for the host. It may read session, run-log, workspace and catalog files; the host
may persist map-layer state in response; it never writes scientific state (slots, claims, run records, workspace files) and uses no
network. Precondition for the extension's X1-A1 auto-run: `preview_revise_section` and `preview_address_comment` are safe to
auto-run only while the host keeps the person's accept step for previews (recorded in `docs/tool-surface.md`).

`start_session` returns only the last 5 notes (each trimmed to 240 characters) with `notes_total`; notes were its unbounded part.
`computed` / `slot_health` scale with the slot count, so the 4,000-character budget holds for a session with a normal slot count.

## 2026-10-20 — `research.md` is written at task boundaries only (session study 01031500, M3)

`.aihydrorules/research.md` is part of the agent's system prompt. Until now every `HydroSession.save()`
regenerated it, so any tool that stored a result changed the agent's own instructions mid-task and
invalidated the provider's prompt cache for the rest of the conversation (one request cost 26% of the task).

Rule: `save()` does not write it. `save(sync_context=True)` does, and is called only at
`start_session`, `archive_session`, `write_research_interpretation`, shard merge, and (via
`write_research_context()`) `export_session`. The project-level digest (`ProjectSession.save`) is a separate,
rarely triggered writer and is unchanged.

Consequences: (1) a session auto-created by a tool call (no `start_session`) has no `research.md` until its
interpretation is written or it is exported or archived; the file would only have held an empty skeleton, and
`get_session_summary` / `get_session_raw_state` remain the live view. (2) `research.md` can lag the session by
design; it is the state at the last boundary, not the current state. (3) The `_sync_reminder` still asks the
agent to write the interpretation, which is the call that publishes mid-to-late task findings into it.

## 2026-10-20 — Answer auditor: tokenised number grammar with anti-gaming constraints (session study S4 + PR 4 review)

The 01031500 session answer exposed an auditor that was noisy in both directions (29 "numbers", 21
violations, 0 of 5 markers resolved, 8 "cited" only through a +/-60-char year whitelist, one real sample size
waved through). The first rewrite (PR 4) fixed the noise but an adversarial review showed the new
exemptions could be used to hide fabricated numbers. Final rules:

1. **Tokenise before matching.** Not numbers: digits in URLs, file paths, strict-grammar markers, and
   code spans *that contain no decimal, percent or unit-bearing number*. Mixed tokens (`a.b-c`) are masked
   only when they have a real id shape: 3+ segments with an 8+ digit segment, a hex/UUID tail, or the
   `v1.2.3`/`version 1.2` version forms. Everything else (`NSE:0.61`, `KGE-0.61`, `site_0.61`, `bias-12.5%`,
   `412.5m3/s`, `~774km2`, `.61`, `v0.61`) has its number extracted. Considered and rejected: "any
   letters+separator+digits token is an identifier" (it hid every glued value).
2. **Classification.** exempt (year/date token, list index, `Figure N`, HUC/EPSG/FIPS/NLCD/COMID integer
   labels, meta-counts, own ids, bare integers) / `sample_size` (hard) / `method_param` (advisory) /
   `quantity` (hard: decimal, unit, metric word nearby or inherited from a table header/label cell).
3. **Anti-gaming constraints.** (a) Word windows never read marker text (`[run:...]` is not the noun "run").
   (b) A meta-count needs no metric word in its sentence; a bare integer in a sentence that carries a marker
   is a quantity. (c) Identifier exemptions never override a unit, need 5+ digits, and come only from the
   study's own session/site id or a 8-15 digit integer directly beside USGS/gauge/station/COMID/HRU;
   never from claim `scope.basins`, run `*_id` outputs or anything else a model can write. (d) A year is exempt
   only as a period reference (`in 2019`), not as the value of a metric (`flow 1990`). (e) Malformed
   marker-shaped brackets are a hard `malformed_marker`, never silently ignored. (f) Value match is a true
   half-ULP of the printed digits (0.7 != 0.61, 9 != 8.0, 7306 != 7305), exponent-aware (`3.2x10^-3`).
   (g) `suggested_marker` needs an exact round-half match and a compatible declared unit; it is a pointer,
   never accepted on the agent's behalf. (h) Markers bind inside their own sentence/bullet/cell only.
   (i) `[lit:<tag>]` tags are single tokens without spaces, at least 3 characters (e.g. `[lit:smith2020]`);
   `[lit:Smith et al. 2020]` is a hard `malformed_marker`.
4. **Severity.** hard = uncited quantity or sample size, value/unit mismatch, path/run not found,
   `hollow_evidence`, malformed marker. advisory = method parameters, unresolvable literature hash.
   `passed` means no hard violation.
5. **Known limit (by design).** A bare integer with no unit, decimal or metric word nearby and no marker in
   its sentence (`3 events had 412`) is not auditable by grammar alone and is exempt; the cost of auditing
   every integer is rejecting honest prose, and the counted/ordinal integers carry no scientific claim.

## 2026-10-19 — Features are basins: experiments fail closed, and retained series are content-addressed (science correctness, WP-1a + H0)

Two silent-wrong-number defects from the 2026-10-19 reviews (experiments/modelling/sessions E1-E5, E13;
hydrograph/data blocker 1).

1. **An explicitly named feature is its own basin, or an error.** Before: `feature="01013500"` that was not
   registered resolved to `__legacy__`; a registered feature took its gauge from `session.site_id` and its
   area from `session.watershed`. A three-gauge experiment returned one basin three times and said
   `complete`. Now `extract_hydrological_signatures` / `extract_geomorphic_parameters` resolve an explicit
   `feature=` strictly (registered id or name only; no on-the-fly registration of a JSON string, no
   fallback). The gauge is `Feature.metadata["gauge_id"]` (set by `register_feature(..., gauge_id=)`), the
   area is `Feature.area_km2` or the polygon's geodesic area, and the session's streamflow slot is used only
   when it is that gauge and period with an intact digest. A feature with no gauge is refused
   (`FEATURE_GAUGE_UNKNOWN`), not given the session's flow. `feature=None` (active feature / session basin)
   keeps its previous semantics: the session's own gauge, area and slot. Calls with `geometry_geojson=` are
   an explicit override and also keep the session's gauge. Cache keys include the gauge (and, for a
   registered feature, geometry hash and area), so re-registering a feature against another gauge cannot
   serve the old numbers. A cache hit returns the run id that produced it (`source_run_id`); the run id is
   now persisted on the cached entry. Considered and rejected: inferring a gauge from the feature id when it
   looks like a USGS number (the id is a label, not evidence); this is the guess that produced the defect.
2. **`separate_baseflow` is not an experiment tool.** It is a session-singleton tool (no `feature`, one
   `baseflow` slot, key `bfi`). An adapter would have had to re-acquire each feature's series outside the
   run record. The per-basin baseflow index already exists with provenance as `baseflow_index` from
   `extract_hydrological_signatures`. Removing the tool is the smaller and safer change; old stored
   experiments that named it fail with a structured error at run time.
3. **Metric names are the tool's real output keys.** `_TOOL_OUTPUT_KEYS` in `tools_experiments.py` lists
   them and `define_experiment` rejects others. The signature keys are checked against the real tool run
   offline; the geomorphic keys are checked against the source of
   `aihydro_watershed.characterize.geomorphic.extract_geomorphic_parameters` because the DEM step needs the
   network (the experiment routing is tested with a labelled stand-in for that step).
4. **Retained streamflow files are named by content.** The file is written under a scratch name, hashed and
   renamed to `streamflow_<gauge>_<sha256[:12]>.json`; an existing file is never replaced. The workspace is
   shared by all sessions in a folder and a run record seals the digest of the exact file it retained, so
   any name that depends only on the gauge lets a later fetch invalidate a sealed record. Run-scoped
   directories were considered; content addressing is simpler, idempotent, and needs no new lookup because
   records already carry the path. Old records and slots (legacy `streamflow_<gauge>.json`) are read by
   recorded path and digest, unchanged.

Review follow-up (B1/C1/C2). Basin-selecting call arguments are not experiment params: `geometry_geojson`
(an explicit override that silently reuses the session gauge) is refused at definition and at run time.
`feature=None` is resolved to the registry's active feature and scoped like `feature=<id>`, because the
cache key and result id already came from it; the alternative (refuse) was rejected as it would break the
no-argument call after any `register_feature`. A registered active feature without `gauge_id` is therefore
refused, not given the session's streamflow. aihydro-modelling 0.6.1 is unpublished until the next release
batch: on 0.6.0 `get_leaderboard` omits the episode facts with a `version_note`; the `>=0.6.1` pin stays and
0.6.1 must be uploaded before tools.

Not done here (out of scope, still open): the experiment is not itself a sealed run record and its id omits
tool and data versions (E6); a cache entry written by an older code version can still be served (E6); the
`define_experiment` `destructiveHint` (E14); the quality-code, gap and date-format defects of the generic
series payload (H0 items 2-6).

## 2026-10-19 — `aihydro-approve` refuses in an agent context; a speed bump, not a boundary (approval policy, AP1)

**Decision.** `approve` and `enroll --user-trust` exit 6 when `AIHYDRO_AGENT_CONTEXT` is truthy
(not empty/0/false/no/off) or `AIHYDRO_ACTIVE` is present, before the TTY check, so a pseudo-terminal
cannot get past it. `revoke` (tightens) and plain `enroll` (only prints) stay allowed. There is no override
flag or env var, because any override is reachable by the agent. A human terminal that inherited the marker
(for example VS Code launched with `code .` from an agent terminal) is refused too; that only makes approval
harder and the message says to open a fresh OS terminal.

**Honest limit.** This is a speed bump. A process running as the same user can `unset` the variables, import
the writer, or drive the CLI otherwise; the CLI cannot stop same-user code. It catches an agent that runs the
real program in an agent terminal with the marker inherited, independent of command text. The real boundary
is the ADR-002b signature with user presence: an `sk-` key that needs a physical touch. A non-`sk` key
unlocked in ssh-agent lets a same-user agent sign silently. The CLI help, the module docstring and the
CHANGELOG say so.

**Follow-up (proposed, not implemented here).** An opt-in `require_user_presence` policy that the promotion
verifier enforces (refuse approvals whose verified channel is not `ssh_sig_sk_*`), plus a CLI warning when
signing with a non-`sk` key obtained from the agent.

**Not done on purpose.** No parent-process check (human and agent VS Code terminals share an ancestor chain)
and no `__main__.py`: `python -m ai_hydro.approval` fails ("is a package and cannot be directly executed");
the console script `aihydro-approve` (or `python -m ai_hydro.approval.cli`) is the entry point. Setting the
marker in the extension's terminals and clearing it in the approval terminal is extension-side work.

## 2026-10-18 — Tool annotations are write-and-network based; U5b is not done (Wave U, U5)

**Annotations.** `readOnlyHint` is true only for a tool that persists nothing and calls no network,
so a network fetch that only fills a cache is not read-only, and `delineation_doctor` /
`gee.status` (Earth Engine initialisation) are open-world. "Read-only" means no session or state mutation, not no trace: a read-only call may append run records
(the middleware's sealed record, `post_run`'s run-log row for the call itself: evidence-minting about
the call; the series tools mint citable `_run_id`s this way) and create an empty tool-owned cache directory; nothing else. Session-resolution
bookkeeping is NOT allowed for read-only calls: a read-only call that named a session used to rebind
the chat to it (re-routing later writes) and backfill `workspace_dir` (a save that bumped the mtime
the recent-study guess sorts by); both are now switched off for tools whose row says
`readOnlyHint = true`, derived from the table. Derived slots and files that a re-run recomputes are
overwrites of derived data, so they are idempotent and not destructive; claims, assumptions, the
interpretation, profile, skills, cleared slots, cache entries and running jobs are researcher-authored
or irreplaceable state, so changing them is destructive. The rows are classified by reading the code
and guarded by a static deny-list and a runtime audit-hook test; neither is a proof.

**Hot listing.** `AIHYDRO_TOOL_SURFACE` defaults to `full` so no host changes behaviour on upgrade, and
is forced to `full` in an evaluation run (`AIHYDRO_EVAL_NONCE`, via `eval_condition.in_evaluation_run()`) so every arm sees one tool surface. A
host that refuses to call an unlisted tool name cannot reach the tools `hot` omits; such hosts stay on
`full`. The extension is unaffected (it reads `_meta.hot` from the full list).

**Recent-study fallback (lead ruling; superseded 2026-10-28, tools 2.8.2: the guess is now OFF by default and opt-in, see `docs/context.md`).** The guess stayed on for interactive use, because turning it
off would make a weak model's omitted `session_id` an error in the extension. It is not silent: a
result produced through it carries a top-level `session_warning` naming the study, as well as
`extra.session_resolution`. It is off whenever `AIHYDRO_EVAL_NONCE` is set (via `in_evaluation_run()`) (a guessed study would
change what an arm computes) and for `off`/`0`/`false`/`no` in `AIHYDRO_RECENT_SESSION_FALLBACK`.
`audit_interpretation` already refused to guess (`allow_auto_create=False`); passing
`allow_recent_fallback=False` is hardening. 27 write tools and 2 read tools still use the fallback by
default (listed in `docs/context.md`).

**U5b not done.** `fetch_forcing_data` is the only writer of the session `forcing` slot. `data_fetch`
writes the `streamflow` slot (D6) but not `forcing`, and `train_hydro_model`, `propose_and_train`,
the forecast tools, `compute_drought_index`, `compute_soil_loss_rusle` and
`ai_hydro.modelling.metrics` read `session.forcing`. Unregistering it would leave no tool that
produces their input. `fetch_streamflow_data` could be retired alone, but the plan retires both
together, and its consumers (recovery hints in about 15 analysis tools, `inundation_drivers`, the
rainfall-runoff workflow, `datasets.yaml`, `security_policy.yaml`, the extension `xs/overrides.ts`,
about 85 test references) should move in the same change as a `data_fetch(variable="forcing")`
slot writer. That writer is the prerequisite.

## 2026-10-17 — Evaluation arm C1 strips advisory and gate output, not data provenance (Wave U, U3)

Lead ruling. C1 ("tools only") removes ADVISORY and GATE output from agent-visible results:
text produced by validators, the skeptic and promotion logic (`quality_flags` and their verdicts
and messages, `promotion_check`, `skeptic*`, "validators did not run" notes) and `next_steps` hints.
OBSERVATIONAL PROVENANCE that a data tool returns about its own data is tool output and stays
visible in all arms: quality/approval/qualifier counts and columns, `quality_summary`,
`site_resolution`, `upstream`, the `streamflow_*` mirrors and area-check values
(`gauge_area_check`, `gauge_area_ratio`). The general rule is: advisory = a judgement produced by a
validator/skeptic/promotion step; provenance = a fact about the served data. There are no per-tool
carve-outs: `eval_condition.STRIPPED_FIELDS` plus the `skeptic*` prefix is the whole rule, applied at
every depth (so `quality_summary` is never stripped). The failed gauge-area check still reaches C1
agents as data (`gauge_area_check: "fail"`); only its `quality_flags` verdict is stripped. P1 runs at
its pinned tools commit and is unaffected.

## 2026-09-07 — Gate promotion on retained evidence

The adversarial ecosystem assessment reproduced promotion of a nonexistent
run. A run ID is therefore an address, not a content hash or proof of execution.
Resolve exact retained records, validate referenced metric and uncertainty
structure, and fingerprint the contents before promotion. Preserve legacy
claims; mark unverifiable registry evidence for review instead of inventing
historical hashes from current data.

Use the existing session/run-log and registry paths. Add compact evidence to
the existing writers rather than creating another source of truth. Keep
drafting and exploratory computation available even when promotion is blocked.
This gate is stricter than the permissive core uncertainty dictionary protocol;
other numerical evidence formats require explicit adapters, not silent coercion.

Scope and method validity remain separate, machine-visible unverified states.
Do not describe this change as end-to-end scientific certification or immutable
provenance. Concurrent registry writes and external artifact lineage remain open.
See [contract](docs/evidence-integrity.md) and [plan](plans/evidence-integrity-2026-09-07.md).


## 2026-09-07 — GFM reference provenance and validation readiness

Missing optional data packages and older backends returning fixtures must not supply synthetic observational references. Explicit test fixtures remain available with synthetic labels and no observational citation. Automatic GFM extent scoring is withheld because valid observation footprint, model-grid alignment and acquisition-time suitability are not established; the reference map remains available. Typed fetch error details reach the tool result. Manual supplied-reference scoring is unchanged and still needs equivalent support-aware checks. See ../aihydro-data/DECISIONS.md for the cross-package contract and verification in PROGRESS.md.

## 2026-09-08 — Joint validity and undefined flood scores

Flood overlap metrics now intersect finite binary observations with explicit caller-supplied validity masks; nodata codes require masking. Masked/missing cells cannot count as flood or dry evidence. Undefined ratios are null with reasons, not perfect scores. Counts expose excluded support. Universal CSI skill bands are removed: acceptance depends on study-specific criteria. These are unweighted cell counts on caller-aligned grids, not verified area-weighted scores or uncertainty estimates.

GeoJSON extent alone does not establish valid observed dry cells. Scoring requires an explicit reference validity grid and model CRS; the supported path currently requires WGS84, finite invertible transforms and pixel-center rasterization. Other cases are not assessed or fail explicitly. Acquisition-time suitability remains caller responsibility; automatic GFM scoring stays gated. Morphology calibration rejects no-event targets explicitly instead of optimizing an undefined metric.

Ratio definitions and threshold/context dependence: [CAWCR integrated verification procedures](https://www.cawcr.gov.au/projects/verification/Mason/IntegratedVerificationProcedures.pdf), consulted 2026-09-08. Map work follows the same principle: rendering capability must not imply scientific adequacy.


## 2026-10-17 — Run-record `tool_version` is the serving host's code; the producer is separate (2.4.1)

Wave U proof 3 (E7) found sealed records stating `tool_version` 2.3.7 on 2.4.0 code: the version came from
`result.meta.version`, which the watershed package filled from stale editable-install `dist-info`. Two
causes, two rules.

1. **Precedence (new).** `RunRecord.tool_version` is `ai_hydro.__version__` of the process that executed and
   recorded the call (`version_source = "package"`), unconditionally. Distribution metadata is a last-resort
   fallback only (`version_source = "distribution"`, or `"unavailable"`). A result's own `meta.tool` and
   `meta.version` identify its *producer* (possibly another package) and are recorded in
   `extra.producer` / `extra.producer_version`; no sealed-schema field changed. Producers state only their own
   module `__version__` (watershed, lsh, data); none looks at, or guesses, a layer above it. `version_check`
   (in `extra`) appears when the `aihydro-tools` dist-info differs from `ai_hydro.__version__` or a producer's
   `meta.version` differs from its imported module, keeps every value and sets `version_mismatch: true`;
   nothing is silently preferred.
2. **Old records.** Records sealed under 2.4.0 and earlier keep their meaning: for them `tool_version` is
   `meta.version` when `version_source == "result_meta"` (the producer's claim, not necessarily the host's).
   `verify` and replay check seal and digests only; they do not interpret `tool_version`, so old records
   still verify unchanged and are not rewritten. Readers that need the host version for an old record read
   `env_digest` / the environment fingerprint, not `tool_version`. Paper A is pinned at 2.3.7 and its
   records are unaffected.

## 2026-10-28: binding store v2, inferred resolution and the study recorder (tools 2.8.2)

Decided by the owner (UX program, OD-1, OD-2; memory defaults): stay on the 2.8.x line; the global most-recent-study
fallback is removed (here: off by default, opt-in for one release) and becomes a project default later (T-P1); test studies (ids `test-*` / `e2e-proof-*` with no workspace) and archived studies
are hidden, never deleted. Duplicates are not auto-detected: a duplicate is hidden when the user archives it, and the study picker shows labelled candidates; P1 runs with its own `AIHYDRO_HOME`.

1. **A new binding file, not a changed one.** v2 is `chat_bindings.v2.json`. The installed extension (0.4.0) reads
   `chat_studies.json` directly and the pinned 2.3.7 server writes it; a schema change in place would break one and be overwritten
   by the other. v1 is imported once and then never read; a derived v1 projection keeps the old header chip alive for one release.
2. **Many chats per study, no inverse.** Two chats on one study is normal (a continued analysis, a fork). The v1 "a study has one
   chat" rule made every second chat steal the first's binding.
3. **Inference is narrower than the old guess and closed where it must be.** The workspace's single study is a deterministic scope,
   not a guess, but it is still inferred: it is not used by the audit, in evaluation runs, or by admin tools that change state.
   A read-only call resolves without binding (the resolver never binds on a read-only call).
4. **The host must bind before the first call** (the original PSMH 7.5 test expected a read-only first call to bind; it cannot).
   The acceptance test is rewritten accordingly (`tests/test_binding_acceptance.py`).
5. **The recorder has one call site, in the run-record layer.** Low-level helpers (`write_workspace_file`, `push_*`) only note what
   happened on the call's capture; `run_records._record_call` writes the manifest once the call's run row is stored, so an entry
   names the sealed run. A file written by a tool that bypasses those helpers is not recorded; it can call
   `run_records.note_file_written`. Legacy files are resolved by a declared name heuristic that returns candidates and never binds.
6. **`aihydro_chat_status` keeps its exact output keys** (the answer audit and the header read them); candidate studies for an
   unbound chat go in `message` and in the `aihydro://study/list` resource, not in new keys.
7. **Capability flags are a resource** (`aihydro://capabilities`), not a line in the server instructions, so the prompt does not grow.
