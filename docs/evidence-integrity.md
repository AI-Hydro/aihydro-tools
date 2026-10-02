# Registry evidence integrity

Implemented locally on 2026-09-07. This is a retained-record integrity gate,
not a scientific certification system.

`promote_claim_to_registry` requires a human approval record for the claim's
current revision (see [Human approval](#human-approval-adr-002a)), eligible
claim status and limitations. It resolves every evidence span before writing to
the registry. Failure returns a structured `EVIDENCE_*`
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

## Human approval (ADR-002a)

Added in Slice 1b (2026-10-02), revised after the slice-1 adversarial review.
`researcher_approved` was an ordinary tool argument, so an agent could (and in a
reproduced run did) promote its own claim. It is now only a *request flag*: it
must be true, and it is never sufficient. Promotion needs an approval record.

**What this does and does not stop.** It blocks unintended and naive
self-approval: authority cannot be conferred through a tool argument or any MCP
tool, and an agent that shells out to `aihydro-approve` without a terminal is
refused. It does **not** stop a process running as the same OS user. Such a
process can append a correctly sealed record itself (the seal is an unkeyed
digest, so it proves integrity, not origin) or drive the CLI through a
pseudo-terminal; the review demonstrated both. Records and registry stamps
therefore carry `channel: "cli_same_user"`, which names how the record was
produced and must not be read as verified human identity. Closing the gap needs
a client-held signing key (ADR-002b, owner decision).

**Approval record.** Stored at `$AIHYDRO_HOME/approvals/approvals.jsonl`
(`AIHYDRO_HOME` defaults to `~/.aihydro`, resolved at call time). Append-only,
one line per record, written under a cross-process lock; existing lines are
never rewritten.

```json
{"schema": "aihydro.approval/1", "claim_id": "...", "session_id": "...",
 "claim_revision_digest": "sha256:...", "approver": {"kind": "human", "id": "..."},
 "channel": "cli_same_user", "approved_at": "<UTC>", "statement": "...",
 "record_digest": "sha256:..."}
```

`record_digest` is the `aihydro_core.records.digest` of the other fields. Lines
that fail verification (edited without resealing, unsealed, or with a non-human
approver) are ignored; a correctly resealed forgery verifies.

**Claim revision digest** (`aihydro.claim_revision/2`). The rule: every
authority-bearing field copied into the registry row, plus the retained
evidence the claim rests on, is bound. Over `digest` (`aihydro.c14n/1`):
claim text, `claim_type`, status, confidence, `confidence_rationale`, scope
(basins, period, forcing, metric, model versions), every evidence span,
`evidence_versions`, limitations, `prereg_id`, `uncertainty_verified`,
normalised through `ScientificClaim`. `evidence_versions` maps each span's
`source_id` to the `sha256-v2` fingerprint of the retained record it resolves
to (`registry/evidence.py`), computed at promotion time. So editing the claim,
or mutating a retained run, dataset result or passage after approval, changes
the digest and invalidates the approval. A span that cannot be resolved binds as
`unresolved:<code>`; promotion refuses it on evidence grounds first. Not bound:
`contradictions`, `citations`, timestamps and promotion bookkeeping, none of
which is copied into the registry row.

**Single use.** An approval authorises exactly one promotion. The registry row
stores `approval: {record_digest, channel}`; the registry refuses (under its
write lock) a second row citing the same record, and promotion refuses an
approval that already authorised one. Promoting again, for example after
retained evidence changed, needs a fresh approval.

**Who can write it.** Only the `aihydro-approve <session_id> <claim_id>` CLI
(`ai_hydro/approval/cli.py` -> `writer.py`). It loads the session read-only,
prints the bound fields including each retained-evidence fingerprint and the
revision digest, and requires the human to type the first 12 hex characters of
that digest. It refuses unless stdin and stdout are a terminal and has no
`--yes` flag. `--approver` defaults to the OS user. No MCP tool imports or calls
the writer; `tests/test_approval_authority.py` enforces this statically (AST
scan of `ai_hydro/`), by importing the full MCP server in a clean interpreter,
and by inspecting loaded modules and registered tools (that last check skips,
with a stated reason, where the installed FastMCP cannot enumerate tools).

**Promotion.** After every other gate passes, a missing or consumed record for
the current revision returns `APPROVAL_REQUIRED` with the exact command
(`aihydro-approve <session_id> <claim_id>`) in `approval_command` and
`recovery`. The refusal does not return the revision digest.

**Legacy rows.** Registry rows written before this change have no `approval`
key and are never rewritten. `list_registry_claims` and the defensibility
report label them `approval: self_asserted` at read time.

The record does not say the claim is true: it records that a named person ran
the CLI for this exact revision of the claim and its retained evidence.

## State isolation

Scope: the registry (`$AIHYDRO_HOME/registry/claims.jsonl`) and approval paths
only. Sessions, course state, jobs and caches still live under
`Path.home()/.aihydro`; the bench and tests isolate sessions separately by
patching the session directory. The registry and approval paths are resolved
when used, not at import. `tests/conftest.py` gives every test its own
`AIHYDRO_HOME`, and `aihydro-bench --run` sets a temporary one for its pytest
subprocess, so test and bench promotions never write the user's real registry.
Before this change the shipped bench tasks B-016 and B-045 wrote promoted
fixtures into `~/.aihydro/registry`. Registry read-modify-write operations
(`append`, `mark_stale`, `mark_retracted`) run under an exclusive cross-process
lock (`fcntl.flock` on a sidecar `claims.jsonl.lock` on POSIX, `msvcrt.locking`
on Windows; with neither available the lock is a logged no-op). The lock is
advisory and the registry file is still rewritten whole, not append-only.

`tests/test_registry_fingerprint_golden.py` pins one legacy `sha256-v2` evidence
fingerprint, so changes to the algorithm that would silently re-stale or re-bless
existing rows fail loudly.

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

**Served data in the capsule.** Session slots strip arrays longer than 50
elements on save, so `session.json` does not hold the discharge series a
signature was computed from. `fetch_streamflow_data` retains the series it served
(in the workspace, or beside the session file when there is none) and points the
slot's `_data_file` at it. `export_session` consumes exactly that artifact: it is
copied verbatim into `data/` (`retained_artifact`, with sha256) and a
stdlib-readable `data/served_streamflow_<gauge>.csv` (`date,q_cms`; missing days
as empty cells; floats written with `repr` so they round-trip exactly) is derived
from it. `capsule_manifest.json` lists both under `data_artifacts` and in `files`
(so `replay.py` verifies the hashes), with `n_rows`, `n_missing`, the request, the
`produced_by_run_id` (the slot's `meta.run_id`) and that run's
`producer_record_digest`. Sessions written before the fetch tool retained its
series fall back to `aihydro_data.fetch` for the recorded request;
`retrieval.mechanism` says which path was used and `retrieval.cache_hit` whether
the aihydro-data disk cache answered (the bytes the run saw) or the provider was
queried again (a re-query, to be weighed by the reader). If no source yields the
series the entry has `status: "unavailable"` and a reason; it is never omitted
silently. `consistency_checks` are export-time comparisons: `n_rows` against the
slot's recorded `n_days`; the exported series' digest against the
`<run_id>#q_cms` `served_data` ref a consuming run recorded (so the CSV is the
series that run actually read); and, when a `baseflow_index` signature exists, a
stdlib Lyne-Hollick (alpha 0.925, 3 passes) on the exported series against the
recorded value. A mismatch sets `status: "exported_with_inconsistency"`. These are
not a replay: `replay_status` stays `archive_integrity` and `recomputation` stays
`not_performed`. A reader recomputes from the CSV with their own code.

**Binding of the exported series.** Each `data_artifacts` entry has a
`binding`. `producer_sealed`: the producing run's sealed record lists the
retained file's digest (`extra.retained_files`, recorded by `fetch_streamflow_data`)
and the exported file matches it. `replay.py` re-checks this from `run_log.json`
(not from the manifest), so a swapped series fails replay with exit 1 even if the
manifest is regenerated, and it also checks that the CSV equals the retained JSON.
`self_attested`: nothing sealed names the series (sessions fetched before
fetch-time sealing, a series that no longer matches its sealed digest, a slot
array, or a re-query); the capsule then vouches only for itself, and the README
and `replay.py` say so. A re-query (`aihydro_data_refetch`) is never attributed to
the run: `produced_by_run_id` is null, `requested_by_run_id` names the run that
triggered it, status is `exported_requeried` (provider queried again) or
`exported_from_cache`, and the README says "re-queried at export; may differ from
what the run consumed". The refetch's product is compared with the slot's recorded
`_aihydro_data_product`; a mismatch, a differing BFI or any sealed-digest mismatch
sets `exported_with_inconsistency`. Every consumer `<run_id>#q_cms` ref is
compared, not only the last. The manifest carries relative paths only.

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
