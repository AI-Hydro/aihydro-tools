# Persisted evidence gate — 2026-09-07

## Scope and acceptance

Replace registry run-ID self-hashes with fingerprints of resolved persisted
records. Promotion must reject missing/foreign runs, absent or nonfinite
referenced metrics, recorded failed/error checks, and quantitative evidence
without a matching persisted uncertainty estimate. Preserve drafts and old
session loading. Old unverifiable registry snapshots must become visibly stale
on inspection, with a review reason; never silently certify them as fresh.

Retain compact metric/uncertainty/check evidence at both run-log writers so
new runs need not rely on mutable current slots. Dataset references resolve
exact slots; manifest metadata alone cannot prove retained payload content.
Paper references resolve retained indexed passages. Quantitative paper-only
claims require structured numerical evidence and cannot pass by citation alone.

This slice checks record integrity and uncertainty structure, not the truth of
prose, scientific appropriateness of a CI, period/population identity, complete
data lineage, or external artifact bytes. Registry concurrent writes, run-log
immutability, replay execution and the extension reader remain separate work.

## Sequence

1. Implement a shared evidence resolver and compact result-evidence capture.
2. Enforce it before promotion; fingerprint resolved content and detect missing
   or changed evidence. Version the fingerprint format; retain old entries.
3. Replace unsupported positive fixtures with actual persisted evidence. Add
   adversarial tests for missing/changed/deleted evidence and old entries.
4. Update package and ecosystem status with measured validation and limits.

## Verification

All session/registry/passage writes in the affected tests use temporary paths.

`/opt/miniconda3/bin/python -m pytest -q tests/test_registry.py tests/test_ledger.py tests/test_evidence_integrity.py tests/test_enforcement_run_log_inputs.py tests/test_run_log_durability.py tests/test_capsule.py --tb=short`

`git diff --check`


## Outcome

Completed locally, uncommitted against `aihydro-tools@bdf0f9f`. The regression
suite above plus `tests/test_audit.py` and `tests/test_layering.py` passed:
**174 tests**, one pre-existing asyncio deprecation warning. `lint-imports`
kept its one contract; Ruff on the new Python files and `git diff --check`
passed. The core bootstrap implementation was exercised through the actual
run writer and promotion path using synthetic observations.

Additional review repairs: content-aware registry IDs preserve revisions;
restored evidence cannot silently reuse a stale/retracted promotion; an old
stale revision cannot stale a newer claim. Negative-result claims share the
numerical evidence requirements. Three run-writer entry points retain compact
scientific evidence (including the legacy helper).

Acceptance is limited to retained-record integrity. New responses explicitly
report unverified scope/method/text alignment. No real registry migration,
full offline-suite run, CI-version matrix, publication or deployment occurred.
