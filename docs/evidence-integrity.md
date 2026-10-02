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
- Make run records immutable and registry/session writes transactional under
  concurrency. SQLite currently permits row replacement; fingerprints detect
  a later difference, but cannot establish a tamper-proof history.

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
