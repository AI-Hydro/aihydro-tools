# Changelog — aihydro-tools

All notable changes to the `aihydro-tools` Python package are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

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
