# Registry evidence integrity

Implemented locally on 2026-09-07. This is a retained-record integrity gate,
not a scientific certification system.

`promote_claim_to_registry` still requires explicit researcher approval,
eligible claim status and limitations. It now resolves every evidence span
before writing to the registry. Failure returns a structured `EVIDENCE_*`
error with the source ID and recovery instructions; it leaves the session
claim available and writes no promoted entry.

| Evidence | Required retained source |
|---|---|
| Run | Exact ID in the session's SQLite run log; any embedded run/session identity must agree. Historical runs never fall back to a current product slot. |
| Dataset | Exact product name resolving to a retained session result. Manifest hashes, DOI strings and similar product names alone are insufficient. |
| Paper | Intact indexed passage identified by passage hash, or indexed document name plus passage hash. The current index cannot verify page numbers. This verifies retained passage content, not entailment or the original document bytes. |

A `metric_ref` must resolve to a finite numeric scalar. `nse`,
`key_outputs.nse` and `data.nse` address the original metric; nested dictionary
paths are supported. Array indexing and synthesized array-length summaries in
new run records are not metric evidence. A declared scope metric must have a
matching explicit metric reference. This is a name match, not validation of
the metric's scientific definition or convention.

Metric-scoped empirical and negative-result claims require both the existing
`uncertainty_verified` acknowledgement and a persisted uncertainty record for
each referenced metric. The currently supported structure is:

```json
{
  "value": 0.8,
  "ci_low": 0.7,
  "ci_high": 0.9,
  "ci_level": 0.95,
  "n": 30,
  "method": "bootstrap_block"
}
```

These are illustrative numbers. The fields follow the core bootstrap output;
`n` retains the producer's semantics. Values must be finite, bounds ordered,
confidence level strictly between zero and one, and `n` an integer at least
two. The method must be declared and cannot be `none`, `unknown` or
`unavailable`. The uncertainty's point estimate must match the referenced
metric within numerical tolerance. The interval need not contain that point
estimate. Recorded failed/error/invalid checks or result states block
promotion. An empty check list is not proof that required validations passed.

The session Store Protocol writer, enforcement writer and legacy helper retain
an additive `evidence` object with schema version 1: original scalar/dictionary
outputs, uncertainty and quality flags. Arrays are omitted rather than replaced
with lengths; nonfinite scalar outputs become null in this compact object.
Existing `key_outputs` remain available for display and older consumers.
Historical records without this object remain readable; promotion only succeeds
if their original stored outputs contain the required evidence. Current slots
cannot supply uncertainty missing from a historical run. Other uncertainty
formats need an explicit producer adapter; they are not guessed into this one.

New registry entries carry `evidence_schema_version: 2` and full
`sha256-v2:` content fingerprints. Missing, changed or unresolvable records are
marked stale by `check_registry_staleness`. Legacy self-ID hashes, empty hashes
and unverifiable metadata snapshots receive the reason
`legacy_evidence_unverifiable`; their historical versions are preserved.
The check is explicit, not a background monitor. Re-promotion after review
retains stale/retracted history. Staling an older revision does not stale a
session claim linked to a newer revision.

The promotion response and entry declare `evidence_verification` with level
`retained_record_integrity`. Scope alignment, method validity and claim-text
alignment are explicitly `not_verified`. These are remaining research gates:

- Bind period, basin/population, units, conventions, metric definition and CI
  to the same analysis artifact. A scalar value match alone cannot do this.
- Verify transitive input artifacts, external file bytes and software versions.
- Check scientific appropriateness of the method and uncertainty design.
- Formalize quantitative assertions instead of inferring values from prose.
- Make registry/session writes transactional under concurrency. Run-log rows
  that carry a sealed run record are now insert-only (see below), but legacy
  rows without a record can still be replaced, a SQLite file can still be
  edited directly (the edit is detectable, not preventable), and none of this
  is a tamper-proof history.

No real claims are migrated or promoted by installing this change. All
regression fixtures use isolated temporary sessions, registry files and passage
indexes.

## Run records (`aihydro.run/2`)

Added in the 2040 records slice. Every tool call that resolves a session
(except the exempt catalog, view, UI and lifecycle tools listed with reasons in
`ai_hydro/session/run_records.py::RECORD_EXEMPT`) gets a sealed
`aihydro_core.records.RunRecord` in `entry["record"]` of its run-log row. A
FastMCP middleware (`RunRecordMiddleware`, `ai_hydro/mcp/app.py`) attaches it
after the call. It adds the record to the row the tool's own writer produced
(`post_run`, the Store Protocol writer, the legacy helper) or creates a minimal
row (`"minimal": true`, empty `key_outputs`) when the tool wrote none. Failed
calls, including raised exceptions, are recorded with `status: "error"`.
A Tier-1 call that uses both `post_run` and the Store Protocol writer produces
two rows (the slot write and the `post_run` row). Both are recorded; the
secondary row's `extra.call_run_id` is the `_run_id` returned to the caller.
The map CLI (`hydro_map_cli delineate-point`) calls the tool directly, so it
records through `run_records.recorded_call` (`extra.mcp_client = "direct_call"`).
List, string and number results are digested as delivered. Replay and coverage
report a record without `extra.entry_digest` as *unbound*, not verified.
Calls with no resolvable session are counted as `no_session` and not recorded.
Direct Python calls that bypass the MCP server are not recorded.

**What a record does not give you (limits).**

- A seal proves *integrity*, not *origin*. Anyone who can write the run log can
  build a record that verifies.
- Insert-only protects against cooperating writers (stale snapshots, accidental
  rewrites), not against an adversary with access to the SQLite file.
- A deleted sealed row is not detectable: there is no hash chain or signed head
  yet. The hash-chained ledger in swatplus-builder is the model for that fix.
- A writer that pre-seals its own record, including `run_python` code or any
  same-user process, authors its own provenance. The middleware leaves an
  already-sealed row untouched and will not overwrite it.
- If `aihydro_core.records` is unavailable when a record is written, the record
  is dropped (and logged) and the legacy row kept; an unverifiable sealed-looking
  record is never stored.

A record states: tool, tool version (`meta.version` when the result carries
one, otherwise the `aihydro-tools` distribution version, labelled by
`version_source`), session, UTC `recorded_at`, status, `input_digest`,
`output_digest`, `parents`, `input_refs`, `env_digest`, and `record_error`.
Digests are `sha256:<64 hex>` over the strict `aihydro.c14n/1` encoding.

- `input_digest` covers the arguments as received, minus private (`_`), `ctx`,
  top-level `session`/`session_id` and secret-shaped names at any depth. Long
  strings are included in full. Defaults a caller omitted are not filled in, so
  an omitted default and the same value passed explicitly digest differently.
- `output_digest` covers `result["data"]`, or the result minus transport keys
  (`_run_id`, `_record_error`, `quality_flags`, `next_steps`). It covers what
  the caller received, not arrays a tool wrote to disk. Outputs over 64 MiB are
  not digested and the record says so.
- `extra.entry_digest` binds the row's legacy fields (`key_outputs`,
  `evidence`, ...) to the record. Editing them after sealing is detected.
- `env_digest` identifies interpreter, platform and the versions of the named
  AI-Hydro and numerical distributions (memoised per process). It is not a
  lockfile.
- A digest that cannot be computed is `None` with an explicit `record_error`.
  The record is still sealed, and the tool result gains `_record_error` so the
  failure is visible to the agent. The middleware never fails a tool call and
  never alters its result otherwise.
- The record is **not** evidence that the computation is reproducible, correct
  or appropriate, and no actor is recorded (the MCP client name goes in
  `extra.mcp_client` when the client sent one).

**Insert-only rows.** Once a row's record is sealed, a same-id write is a no-op
when identical (or when it is a stale legacy snapshot with the same other
fields, which never drops the record) and is **refused and logged** otherwise.
A record that does not verify, or whose `run_id` differs from the row, is
refused. Rows without a record keep the legacy upsert behaviour, and
`HydroSession.set("_run_log", ...)` still works.

**Run ids.** `post_run` and middleware-created ids are
`{tool}.{yyyymmdd}.{session8}.{hex8}` (the suffix was 4 hex digits). No
consumer parses the suffix: the auditor grammar accepts `[A-Za-z0-9._-]+`, and
the extension treats ids as opaque. Older ids stay valid. Uniqueness within a
session is checked at generation, and the sealed-row rule applies at write time.

**Lineage.** `extract_hydrological_signatures` records the streamflow run whose
slot supplied its series: `parents=[run_id]` and two `served_data` input refs,
the producer's recorded `output_digest` and the digest of the series actually
read (`<run_id>#q_cms`). The slot carries its run id in `meta.run_id`, stamped
by the run-log writer when the slot is stored. A slot stored before this change
carries none, so no edge is recorded and `extra.parent_unresolved` says so.
If the producer has no record (written outside the middleware) the edge has a
reference and no digest. Other tools declare lineage with
`run_records.declare_lineage`.

**Replay.** The `replay.py` written into a capsule verifies file hashes and every
v2 record (the seal, and the binding to its run-log row), prints
`replay_status`, and never claims recomputation. `--live` additionally
cross-checks numeric `key_outputs` against the values retained in `session.json`
using the real export shape (slots are top-level keys). It prints the number of
comparisons and exits 2 when it found none to compare. Exit 1 means a failed
check. `capsule_manifest.json` carries `replay_status: "archive_integrity"`
and `recomputation: "not_performed"`. See `ai_hydro/capsule/standalone_replay.py`.

| replay_status | Established |
|---|---|
| `archive_integrity` | Files match their hashes; every v2 record verifies. Nothing recomputed. |
| `cross_check` | `--live` and at least one comparison, all agreeing within tolerance. Still no recomputation. |
| `not_performed` | An integrity check failed. |
