# Persisted research snapshot v1

One Python-owned read interface serves Replay, experiments, claims and headless
inspection. It does not run an analysis, migrate a session, or synthesize
historical runs from current product slots.

Python API:

```python
from ai_hydro.session.surfaces import read_research_snapshot
snapshot = read_research_snapshot("my-study")
# Explicit exported capsule directory or session JSON paths are also accepted.
```

The CLI uses the same function:
`python -m ai_hydro.session.surfaces my-study`.
`--home /path/to/aihydro-home` is available for isolated inspection/tests.

MCP resource template: `aihydro://research/snapshot/{reference}`. `reference`
is the session ID or explicit path encoded as UTF-8, unpadded base64url. This
preserves Unicode, spaces and path separators without ambiguous URI decoding.
References are resolved on the backend's machine. The extension discovers the
connected server advertising this exact template; zero or multiple matching
servers produce an explicit actionable error. An older backend must be updated
and restarted. It does not fall back to reconstructed or stale history.

The JSON envelope contains:

- `schema_version: 1`, `session_id`, `session_path`, `source` (`session` or `capsule`).
- `run_log_source`: `sqlite`, `legacy_json`, `capsule_json`, or `absent`.
- `runs`: chronologically ordered retained run records with original IDs,
  tool names, timestamps and key outputs. Existing inputs and compact evidence
  are retained, not reconstructed from current slots.
- `claims`: canonical claims plus legacy claims when not superseded.
- `experiments`: the active-feature/latest-parameter result using the backend
  slot-selection convention, with legacy-feature fallback.
- `warnings`: missing-history diagnostics. Nonfinite values are serialized as
  null for strict JSON; null never means zero.

For live sessions an existing SQLite log is authoritative, even when empty.
Read errors, malformed rows and conflicting run/session identity are errors;
they do not trigger legacy fallback. When no database exists, a retained legacy
JSON log is read without migration. An exported capsule reads its own adjacent
`run_log.json` or embedded legacy log and never borrows from a live same-ID
session. If no history is retained, `runs` is empty with a warning. Current
analysis outputs do not become invented `.stored` runs.

The extension shares simultaneous requests but does not cache completed
snapshots as fresh. UI waits time out after 15 seconds; late responses cannot
overwrite a newer Replay/experiment selection. Evidence Board loads use the
same snapshot and propagate backend failures to the UI. Its claim-event updates
are restricted to the selected study; events received during snapshot loading
are not applied to that in-flight view.

These are read snapshots, not a transaction spanning session JSON and SQLite.
Automatic refresh, pagination/virtualization for very large studies, stable
event revision ordering, immutable runs and scientific recomputation remain
separate work. The Replay panel labels absent validation as “not checked” and
shows retained inputs/uncertainty; a passing recorded check is not certification
of scientific validity.

Local integration evidence and commands are recorded in
[the execution plan](../plans/persisted-research-surfaces-2026-09-07.md).
