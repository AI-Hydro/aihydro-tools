# Persisted research surfaces — 2026-09-07

## Scope

Close R02: the extension reads session JSON, while authoritative runs now live
in SQLite. Its synthetic slot-history fallback loses actual IDs and hides
repeated executions. The experiment reader also does not unwrap current v2
feature/parameter slots.

Implement one versioned backend snapshot, exposed as an MCP resource and a
Python API. Extension panels use the existing connected backend, selected by
its advertised resource template, with explicit failure when unavailable or
ambiguous. No new database, kernel, interpreter discovery or Node SQLite schema.
Keep explicit capsule-path support using the capsule's own `run_log.json`.

## Acceptance

- Python-created SQLite runs, including repeated tools, retain actual IDs,
  outputs, timestamps, inputs and compact uncertainty/check evidence in panels.
- Current nested experiments and formal claims load through the same snapshot.
- Legacy JSON logs remain readable without migration/write side effects.
- Capsule runs come from the capsule, never a live same-ID session.
- Missing logs produce empty recorded history, not reconstructed runs. Corrupt
  logs, identity conflicts and incompatible/absent backends produce errors.
- Async loading cannot let a slow previous request overwrite a newer selection.
- No scientific recomputation is claimed; this is inspection of recorded runs.

## Verification

Temporary Python sessions and capsule fixtures; strict resource contract tests;
extension unit tests including a real Python-persisted snapshot; host/webview
TypeScript checks; relevant webview tests; import layering and diff checks.
Preserve all earlier uncommitted tools/modelling work and unrelated data work.


## Local outcome

Implemented the backend snapshot API, CLI and advertised MCP resource, and
connected Replay, Experiment Table and Evidence Board through the existing
backend connection. Removed fabricated slot-history backfill. Preserved exact
repeated-tool IDs, metadata and compact evidence. Added explicit storage/version/
backend errors, request coalescing and stale-response guards. Evidence events
from other studies no longer merge into the selected ledger. Replay no longer
labels unvalidated runs as passing and exposes stored inputs and uncertainty.

Validation:
- 153 Python tests passed across snapshots, MCP resources, run durability,
  evidence-integrity, registry/ledger and layering. Native MCP resource routing
  is exercised against the same Python snapshot.
- 18 extension tests passed, including a real Python-written session/SQLite log
  and nested experiment fixture, plus host stale-response/error propagation.
- 16 webview tests passed across Replay, ledger isolation, Evidence Board and
  experiment plots. Expected error-path tests log their simulated failures.
- Host and webview TypeScript checks passed. Import-linter kept its contract;
  new Python files passed Ruff. Both repo diff checks passed.

Local/uncommitted. No installed VSIX/backend restart, remote deployment or live
scientific data was involved. The new resource must be available on the running
backend before the updated panels can load. Large-study pagination, automatic
refresh, event revision ordering and cross-store transactional consistency remain
open; this is recorded-history inspection, not recomputation.
