# Decisions

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

**Recent-study fallback (lead ruling).** The guess stays on for interactive use, because turning it
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
