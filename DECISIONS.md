# Decisions

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
