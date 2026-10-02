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
- `record_coverage` and `record_errors` (additive; `schema_version` stays 1):
  how many run-log rows carry a verifiable `aihydro.run/2` record
  (`run_log_rows`, `v2_records`, `v2_verified`, `legacy_unrecorded`,
  `record_errors`, `coverage`, `problems`), and the `record_error` of each run
  whose record says a digest is missing. Reading verifies records and never
  writes. `record_coverage` is `null` when the installed `aihydro-core` has no
  records module. See [evidence integrity](evidence-integrity.md#run-records-aihydrorun2).
- Run rows (additive): `minimal` (true for rows written by the recording
  middleware rather than by the tool; their `key_outputs` is always `{}`, so
  consumers can hide them while counting them), `record` (the sealed record,
  unchanged) and `record_error` (lifted from the record, else null).
- Claim fields (additive), read from the insert-only revision store without
  loading or migrating the session: `revision`, `revision_digest`,
  `history_len` (null/0 when the claim has no history), `revision_error` (set,
  with the other fields null, when only that claim's chain fails verification;
  other claims and the runs are unaffected), and `revision_drift`
  (`state` `no_history|in_sync|drifted|evidence_unchecked`, `drift`,
  `changed_fields`, `evidence_checked`).
  - Evidence is re-read with the same `fingerprint(resolve_source())` promotion
    uses, from the run log the snapshot already holds, so `in_sync` means the
    claim fields and the retained run rows it cites both match the latest
    revision (`evidence_checked: true`). A tampered or missing cited run reads
    `drifted` with `evidence_versions` in `changed_fields`.
  - Dataset and paper spans need a loaded session or the passage index and are
    not checked read-only: `evidence_checked: false`, `revision_drift_reason`
    says why, and the claim is never `in_sync` (`evidence_unchecked`).
  - `approval` is `{state, for_revision_digest, channel, trust_root, principal,
    policy, record_digest}`. `state` is `none`, `approved` (an accepted
    approval bound to the digest recomputed with live evidence, as the
    promotion gate reads it), `consumed` (a registry row cites it),
    `stale_evidence` / `stale_revision` (an approval exists for the recorded
    revision but retained evidence changed / the claim was edited since;
    `recorded_state`, `live_digest`), `evidence_unchecked` (accepted approval,
    evidence not checkable read-only) or `unverifiable` (the verifier does not
    accept the record: no trust root, bad signature; also corrupt chains and
    capsules). Approvals fail closed (ADR-002b); only `approved` means the gate
    would accept it, and it never appears with unchecked or changed evidence.
  - A capsule never reads the live revision store, and the snapshot does not
    consult a capsule's own `approvals/`; capsule claims read `unverifiable`.
- `revision_source`: `sqlite`, `sqlite_immutable`, `absent`, `capsule` or `error`.
- Reading never writes. SQLite stores open `mode=ro` with no PRAGMA or DDL, so
  a DELETE-journal store stays DELETE-journal. If that fails (read-only
  directory, WAL sidecars that cannot be created) the read retries
  `immutable=1`, which skips locking and ignores an uncheckpointed WAL, and
  `run_log_source` / `revision_source` say `sqlite_immutable`. A read of a WAL
  store in a writable directory may still create its `-wal`/`-shm` sidecars.
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
event revision ordering and scientific recomputation remain separate work. Rows
that carry a sealed run record are insert-only; legacy rows are not. The Replay panel labels absent validation as “not checked” and
shows retained inputs/uncertainty; a passing recorded check is not certification
of scientific validity.

Local integration evidence and commands are recorded in
[the execution plan](../plans/persisted-research-surfaces-2026-09-07.md).
