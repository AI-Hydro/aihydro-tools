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
