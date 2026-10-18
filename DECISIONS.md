# Decisions

## 2026-10-18 — Tool annotations are write-and-network based; U5b is not done (Wave U, U5)

**Annotations.** `readOnlyHint` is true only for a tool that persists nothing and calls no network,
so a network fetch that only fills a cache is not read-only, and `delineation_doctor` /
`gee.status` (Earth Engine initialisation) are open-world. Three kinds of recording are not counted
as a tool write because every call has them: the run record the middleware makes, the run-log row
`post_run` writes for the call itself (so `summarize_series` and its siblings are read-only), and
`_resolve_session` bookkeeping. Creating an empty tool-owned cache root before listing it is not a
write either. Derived slots and files that a re-run recomputes are overwrites of derived data, so they
are idempotent and not destructive; claims, assumptions, the interpretation, profile, skills, cleared
slots, cache entries and running jobs are researcher-authored or irreplaceable state, so changing them
is destructive.

**Hot listing.** `AIHYDRO_TOOL_SURFACE` defaults to `full` so no host changes behaviour on upgrade. A
host that refuses to call an unlisted tool name cannot reach the tools `hot` omits; such hosts stay on
`full`. The extension is unaffected (it reads `_meta.hot` from the full list).

**Recent-study fallback.** `audit_interpretation` already refused to guess a study (it passes
`allow_auto_create=False`); it now also states `allow_recent_fallback=False`, with a test. 27 write
tools and 2 read tools still use the fallback by default (listed in `docs/context.md`). Removing it for
them would turn a weak model's omitted `session_id` into an error, which is a behaviour change for the
extension, so it is an operator switch (`AIHYDRO_RECENT_SESSION_FALLBACK=off`) and not the default.
The rule that fired is already in `extra.session_resolution` of each run record.

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
