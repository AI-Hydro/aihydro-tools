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
pseudo-terminal; the review demonstrated both. Unsigned (v1) records therefore
carry `channel: "cli_same_user"`, which names how the record was produced and
must not be read as verified human identity. ADR-002b closes the forgery gap
with signed v2 records (see [Signed approvals](#signed-approvals-adr-002b)).

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

**Capsule approvals and external verification.** The machine's trust root is not
the boundary against a same-user process; a third party's own key file is.
`export_session` writes `approvals/` into the capsule: one verbatim approval
record (signature included) per promoted claim, plus `approvals/index.json`
with the registry row stamp (`approval`, `claim_revision_digest`,
`claim_revision`). Files are hashed in `capsule_manifest.json` (also summarised
under `approvals`). Claims with no approval (legacy `self_asserted` rows, or a
promoted claim without a registry row) are listed with status `no_approval`,
never implied approved. `python replay.py --allowed-signers FILE` verifies each
approval with `ssh-keygen -Y verify` against the file the verifier supplies (for
example the owner's published GitHub keys), namespace `aihydro-approval@v1`,
principal = the approver id; it also re-derives the sealed body digest and checks
that the approval's `claim_revision_digest` equals the registry stamp's. It also
binds the approval to the capsule: the record's own `claim_id` must equal the
index entry's, the record, index and `session.json` must name one session, and
`claim_revision_digest` is recomputed in stdlib (`aihydro.claim_revision/2`) from
the claim in `session.json` and the cited run rows in `run_log.json`; editing the
claim, or a cited run row, after approval fails. Claims whose evidence is not
run-backed (dataset or paper spans), or legacy `evidence` lists, cannot be
re-derived in stdlib and FAIL with that reason rather than PASS. Approval files
are confined to `approvals/<64 hex>.json` named by the record digest (no absolute
paths, traversal or symlinks); the approver must be a human actor. The index
`status` `record_carried` means only that the record is in the capsule. It
prints `PASS`/`FAIL` per approval and
`approvals: N verified against supplied signers, M failed, K unsigned (cli_same_user/opt-out)`;
any failure exits 1. Unsigned v1 records are reported `UNSIGNED`, never `PASS`;
without `--allowed-signers` signed approvals are "not verified (no signer file
supplied)". The capsule carries no trust root, and local revocation is not
consulted: use `valid-before` in the supplied file to retire a key. A pass shows
who signed which claim revision, not that the claim is true.

## Claim revisions (`aihydro.claim_revision_record/1`)

Added in 2040 slice 2. Every authority-bearing change to a session claim
appends exactly one sealed row to `<sessions_dir>/<session_id>.claims.sqlite3`.
`ai_hydro/session/claim_revisions.py` is the only writer.

A row carries `session_id`, `claim_id`, `revision` (from 0), `supersedes` (the
previous row's `revision_digest`), `revision_digest`, `content`, `cause`
(`{tool, reason, run_id?, ...}`), `actor`, `recorded_at` and `record_digest`
(`aihydro_core.records.ClaimRevision`). `revision_digest` is the same
`aihydro.claim_revision/2` digest an approval binds to, including the retained
evidence fingerprints, so a revision and an approval name the same thing.

| `cause.reason` | Written by |
|---|---|
| `created`, `redefined` | `add_claim` (`redefined` when the id already existed) |
| `status_update` | `update_claim_status` |
| `promotion` | `promote_claim_to_registry`, after the registry row is written. Promotion is outside the digest, so the row repeats the approved `revision_digest` and records `registry_id` |
| `staleness` | `check_registry_staleness` when it sets `status: stale` |
| `evidence_drift` | promotion found that the retained evidence differs from the latest stored revision. The revision is written, then promotion is refused unless an unused approval already binds the new revision |
| `out_of_band_edit` | promotion found claim fields that differ from the latest revision for another reason (the session JSON was edited outside the tools) |
| `legacy_unrecorded` | first touch of a claim that predates the store |

Rules:

- **Insert-only.** `PRIMARY KEY (claim_id, revision)`, plain `INSERT`, and
  `BEFORE UPDATE`/`BEFORE DELETE` triggers that abort. Read-modify-insert runs in
  `BEGIN IMMEDIATE`, so concurrent writers get consecutive numbers.
- **Fail closed.** Every read verifies each seal and the chain; a failing row
  raises instead of being skipped, and promotion therefore errors.
- **Record before save.** The revision is written before `session.save()`. A
  change that cannot be recorded is not persisted.
- **No unchanged rows.** An update that moves no authority field (same
  `revision_digest`) writes nothing.
- **Promotion** requires the approval's `claim_revision_digest` to equal the
  latest stored revision's `revision_digest`. Registry rows gain
  `claim_revision_digest` and `claim_revision` (the approved revision number).
  `registry_id` is unchanged and legacy rows are not recomputed.
- **Legacy claims** get revision 0 with `legacy_unrecorded` on first touch,
  recording the state when first seen. Earlier history is never back-filled.

A seal proves integrity, not origin: a process running as the same OS user can
rewrite the SQLite file and re-seal. Signed approvals (ADR-002b) address origin.

**There is no external anchor for the chain itself.** Truncating the tail, or
editing the latest row and re-sealing it, cannot be detected from the SQLite
file alone. Each seal covers only its own row, and each row links backwards.

The anchor that exists today is the registry. Promotion stamps
`claim_revision_digest` and `claim_revision` on the registry row.
`check_registry_staleness` compares every stamped row with the session's chain
and returns `revision_chain_mismatches`, each flagged
`revision_chain_mismatch` with a reason: `missing_revision` (tail truncated
below the promoted revision), `different_digest` (the revision was rewritten and
re-sealed) or `chain_corrupt`. This detects tampering only for claims that were
promoted and only at or below the promoted revision. Registry rows written
before this change carry no stamp and are skipped. The check reports and does
not change claim status.

**Reading.** `history(session_id)` returns, per claim, `{ok: true, rows}` or
`{ok: false, error}`, so one corrupt chain does not hide the others. `latest`
and `get_revision` raise for a corrupt chain, so anything that gates authority
fails closed. `revision_drift(current_fields, latest_row)` is a pure helper that
reports whether a claim's current fields (with live evidence fingerprints) still
match its latest revision.

**Reverting to an approved state reuses the approval.** An approval binds a
revision digest, not a point in time. If a claim is edited and then edited back to
fields whose digest equals an approved, unconsumed revision, the old approval
matches again and promotion can use it (once). The chain shows the round trip as
two new revisions that repeat the earlier digest, so a reviewer can see it. The
approval is still single use, and any difference in the claim or its retained
evidence breaks the match.

### Signed approvals (ADR-002b)

`aihydro-approve` is the canonical approval channel; the editor extension is
only a client that opens it. After the terminal confirmation the CLI signs the
approval with an SSH key through `ssh-keygen -Y sign -n aihydro-approval@v1`
(stdlib subprocess; no new dependency; private keys are never read or stored by
this code) and writes schema `aihydro.approval/2`:

```json
{"schema": "aihydro.approval/2", "claim_id": "...", "session_id": "...",
 "claim_revision_digest": "sha256:...", "approver": {"kind": "human", "id": "..."},
 "approved_at": "<UTC>", "statement": "...",
 "signer": {"fingerprint": "SHA256:...", "key_type": "ssh-ed25519"},
 "record_digest": "sha256:...",
 "signature": {"format": "sshsig", "namespace": "aihydro-approval@v1", "armored": "-----BEGIN SSH SIGNATURE-----..."}}
```

`signer` is sealed by `record_digest`; `signature` signs the UTF-8 bytes of
`record_digest` and sits outside the seal. v2 has no `channel` field: the
**verifier derives the channel**, never the record, and the label names both the
key class and the trust root it was verified under:

| Channel | Meaning |
|---|---|
| `ssh_sig_sk_system_trust` | `sk-` key (physical touch per signature, no `no-touch-required`), protected system trust root |
| `ssh_sig_system_trust` | other enrolled key (software / agent), protected system trust root |
| `ssh_sig_sk_user_trust`, `ssh_sig_user_trust` | same, but the trust file is user-writable |
| `ssh_sig_sk_supplied`, `ssh_sig_supplied` | verified against an allowed_signers file the verifier supplied |
| `cli_same_user` | legacy v1 record, accepted only under an explicit opt-out |

**What this does and does not stop (read this first).** Against a process
running as the same OS user there is **no boundary** unless
`trust_root == "system"` **and** the key is an `sk-` key that needs a touch.
`*_user_trust` is integrity-only and equivalent to `cli_same_user`: the trust
file is editable by that process, which can enrol its own key and sign. A
software key loaded in ssh-agent signs silently. A trojaned CLI in an editable
install can show one claim and have the human sign another. No label here means
"a human was verified"; the guarantees are that each approval needs a signing
action by an enrolled key, and that a third party can verify the signature
against keys published off the machine.

**Trust root.** The signature must verify against an `allowed_signers` file:
`/etc/aihydro/allowed_signers` (override for tests: `AIHYDRO_SYSTEM_TRUST_FILE`),
else `$AIHYDRO_HOME/trust/allowed_signers`. The label `system` is earned only
when the file **and its directory** are neither writable by nor owned by the
current user (`os.access` + owner check); otherwise the file is labelled
`user_writable`. `valid-after` / `valid-before` are enforced by `ssh-keygen` at
verification time, so a rotated-out key stops verifying (an unconsumed approval
it signed must be redone). Verification also accepts a file the verifier
supplies (`check_approval(record, allowed_signers=...)`, trust root `supplied`):
that off-machine check is the real boundary.

**Refused by `find_approval`:** a sealed v2 line with a missing, malformed or
invalid signature (including a genuine signature copied from another record);
a signature whose key differs from the sealed `signer`; a key not in
allowed_signers or outside its validity window; a revoked key; an
`approver.id` that is not the enrolled principal of the signing key.

**Stamp.** `approval_stamp(record)` returns `{record_digest, channel,
trust_root, principal, signer {fingerprint, key_type}, policy}`; `principal`
comes from `ssh-keygen -Y find-principals` / `-Y verify`, and `policy` is
`signed_required` or `unsigned_opt_out`.

**Fail closed (`require_signed`).** With no allowed_signers file anywhere, no
approval verifies and the CLI refuses (exit 5) and tells the human to enrol.
v1 (`cli_same_user`) approvals are accepted only under an explicit
development opt-out, `AIHYDRO_REQUIRE_SIGNED=0` or `{"require_signed": false}`
in `$AIHYDRO_HOME/approvals/config.json`; the opt-out is logged and every
record verified under it is stamped `policy: "unsigned_opt_out"`. A protected
system trust root forces signatures and cannot be relaxed. Tests that use v1
fixtures set the opt-out explicitly.

**CLI.**

```
aihydro-approve <session_id> <claim_id> [--key PATH]
aihydro-approve enroll <key.pub> [--principal NAME] [--valid-before D] [--user-trust]
aihydro-approve revoke <SHA256:fingerprint> [--reason TEXT]
```

The key is `--key`, else `~/.ssh/id_ed25519_sk`, `id_ecdsa_sk`, `id_ed25519`,
`id_ecdsa`, `id_rsa`, else an enrolled ssh-agent key. `--approver` defaults to
the OS user and must equal the principal enrolled for the key (`enroll
--principal` defaults to the OS user). `enroll` prints the `allowed_signers`
line and the `sudo` command for the system root; it never runs sudo, and writes
only the user-writable fallback when `--user-trust` is given.

**Revocation.** `revoke` appends a sealed line to
`$AIHYDRO_HOME/approvals/revocations.jsonl`. That file is deletable and
forgeable by any process running as the user; it is a convenience, not a
security control. Real revocation is setting `valid-before` on the key's line in
the **system** `allowed_signers` (or removing the line).

Hardware behaviour (whether `ssh-keygen -Y verify` enforces the touch flag) is
unverified without a device. The registry-row stamp is wired into promotion in
packet A2; until then rows still carry the constant `cli_same_user`.

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

## No absolute paths in records or capsules

Sealed records reference retained files by a location-independent ref, never by
absolute path: `session-data:<file name>` (session data directory) or
`workspace:<relative path>`, via `ai_hydro/session/refs.py`. The recorded digest
is the file's identity; the ref is only a locator, so a record or capsule leaks
no home directory and means the same on another machine. Records written before
this change keep their absolute paths (sealed rows are never rewritten); readers
resolve them and match capsule files by name, so they still verify. Capsule
`session.json` rewrites local paths to refs or `~/`.

### Run-log row bodies and legacy rows

Every run-log row body (`key_outputs`, `inputs`, `evidence`, and any other
free-form field except `record`) is scrubbed at one choke point, the store
writer (`session/store.py::_scrub_row_body`), before it is digested into
`extra.entry_digest` and stored: session dir to `session-data:`, workspace to
`workspace:`, home to `~/`, any other absolute path to `<abs>/basename`. The
scrub is idempotent. `export_session` applies a second layer to every other
exported JSON/MD/text file. Rows sealed before this change that still contain an
absolute path are never rewritten in the store; because a scrubbed copy of a
sealed row would not verify, the export carries a `redacted_for_privacy` stub
with the row's `record_digest` instead, and `capsule_manifest.json` records
`privacy: {legacy_paths_scrubbed_on_export: N, ...}`. The standalone replay
reports such rows as "redacted for privacy (not verifiable from capsule)":
neither verified nor failed. A claim approval that cites a redacted row cannot be
re-derived from the capsule and says so.

**What the scrubber rewrites (and never does).** Only demonstrably local paths:
under the session dir (`session-data:`), workspace (`workspace:`) or home
(`~/`); other absolute POSIX paths only when rooted in `/Users`, `/home`,
`/private`, `/var`, `/tmp`, `/opt`, `/root`, `/mnt`, `/Volumes`, `/srv` or
`/scratch` with at least two segments; Windows drive and UNC paths (`<abs>/basename`).
Units (`/day`, `m3/s`), `+/-`, NetCDF/Zarr group paths, endpoint paths and
anything inside a URL or URI (`https://`, `s3://`, `doi:`) are never touched.
It is idempotent. It also covers `record.extra` notes and `input_refs` declared
through `declare_lineage`. Dict keys are scrubbed too (a key that is a path is
renamed) and tuples become lists before digesting; exported `.html`/`.svg` files
get the same text scrub, while `approvals/` (human-signed) and `data/` stay
verbatim by design.

**Redaction cannot hide tampering.** On export a path-bearing sealed row is
first verified against its raw body (record seal and `entry_digest`). If it does
not verify it is exported as `{"integrity": "seal_mismatch_at_export", ...}` and
replay counts it as a FAILURE. A row that verifies becomes a
`redacted_for_privacy` stub with `session_id`, `timestamp`, `record_digest`,
`entry_digest` and (when it holds no path) the full `record`, so replay still
checks the seal. Any redaction makes `replay_status` `archive_integrity_partial`
and the manifest `privacy` block lists the redacted run ids.
