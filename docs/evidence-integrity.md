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

Added in Slice 1b (2026-10-02). `researcher_approved` was an ordinary tool
argument, so an agent could (and in a reproduced run did) promote its own claim.
It is now only a *request flag*: it must be true, and it is never sufficient.
Authority comes from a record the model cannot mint.

**Approval record.** Stored at `$AIHYDRO_HOME/approvals/approvals.jsonl`
(`AIHYDRO_HOME` defaults to `~/.aihydro`, resolved at call time). Append-only,
one line per record, written under a cross-process lock; existing lines are
never rewritten.

```json
{"schema": "aihydro.approval/1", "claim_id": "...", "session_id": "...",
 "claim_revision_digest": "sha256:...", "approver": {"kind": "human", "id": "..."},
 "approved_at": "<UTC>", "statement": "...", "record_digest": "sha256:..."}
```

`record_digest` is the `aihydro_core.records.digest` of the other fields. Lines
that fail verification (tampered, unsealed, or with a non-human approver) are
ignored.

**Claim revision digest.** `digest` (canonicalization `aihydro.c14n/1`) over the
tagged object `aihydro.claim_revision/1`: claim text, scope (basins, period,
forcing, metric, model versions), status, confidence, every evidence span, and
limitations, normalised through `ScientificClaim`. Editing any of these changes
the digest and invalidates the approval; the claim must be approved again.
Deliberately outside the digest: `claim_type`, `confidence_rationale`,
`contradictions`, `citations`, `prereg_id`, `uncertainty_verified`, timestamps
and promotion bookkeeping.

**Who can write it.** Only the `aihydro-approve <session_id> <claim_id>` CLI
(`ai_hydro/approval/cli.py` -> `writer.py`). It loads the claim read-only, prints
text, scope, evidence spans, limitations and the revision digest, and requires
the human to type the first 12 hex characters of that digest. It refuses unless
stdin and stdout are a terminal, and has no `--yes` flag. `--approver` defaults
to the OS user. No MCP tool imports or calls the writer; `tests/test_approval_authority.py`
enforces this statically (AST scan of `ai_hydro/`), by importing the full MCP
server in a clean interpreter, and by inspecting registered tools.

**Promotion.** After every other gate passes, a missing record for the current
revision returns `APPROVAL_REQUIRED` with the exact command
(`aihydro-approve <session_id> <claim_id>`) in `approval_command` and
`recovery`. Registry entries gain `approval: {"record_digest": ...}`.

**Legacy rows.** Registry rows written before this change have no `approval`
key and are never rewritten. `list_registry_claims` and the defensibility
report label them `approval: self_asserted` at read time.

**What this does and does not establish.** It makes agent self-approval through
the MCP tool surface impossible. It does not stop a process that runs as the
same OS user from writing the approvals file directly (for example through a
general-purpose code-execution tool such as `run_python`, or a shell tool in the
client), and the digest seal proves integrity, not origin. The TTY check blocks
accidental or naive non-interactive use, not a determined same-user process.
Closing that gap needs a signing key or an approval service outside the agent's
reach (ADR-002b, owner decision). The record also does not say the claim is
true: it records that a named human reviewed this exact revision.

## State isolation

The registry (`$AIHYDRO_HOME/registry/claims.jsonl`) and approval paths are
resolved when used, not at import. `tests/conftest.py` gives every test its own
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
