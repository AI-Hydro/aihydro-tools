# Request context

Every tool call can carry the caller's context. AI-Hydro reads it from the MCP
request `_meta`, under one namespaced key:

```json
{"_meta": {"aihydro/context": {
  "study_id": "basin_26p9_78p1",
  "workspace": "/path/to/project",
  "chat_id": "01KV1EPM88P9FV5CWNVJ9V60JY",
  "client": "aihydro-vscode/0.4.0"
}}}
```

All fields are optional. `study_id` means `session_id`; no tool is renamed.

## Precedence

Per field, `_meta["aihydro/context"]` is read first. The legacy hidden tool
arguments `_chat_id` and `_workspace` are read second and still work. They are
always stripped before argument validation, and the first use in a process logs
one deprecation warning. A malformed `_meta` value is ignored.

Session resolution (`_resolve_session`, 2.8.2): explicit `session_id` argument, then
`study_id` from `_meta`, then the chat binding, then auto-create from a hint, then the one
study of the call's workspace, else an error that lists labelled candidates.

- **Workspace.** The call's workspace is `_meta.workspace` (or the legacy `_workspace` argument).
  The machine-global `~/.aihydro/active_workspace.json` is read only when the call carries no
  context at all; with two VS Code windows it names whichever was focused last.
- **One study in the workspace** (`workspace_single_study`). Exactly one visible study whose
  `workspace_dir` is that workspace resolves, with a top-level `session_note`. A read-only tool
  does not bind the chat; a writer does, and the result carries `session_binding`. It is an
  inference, so it is closed to `audit_interpretation` (`allow_recent_fallback=False`), to
  evaluation runs, and to admin tools that change state (`allow_auto_create=False` and not
  `readOnlyHint`), which need a named or bound study.
- **Most recent study on disk** (`recent_fallback`). OFF by default: it picked test and unrelated
  studies. `AIHYDRO_RECENT_SESSION_FALLBACK=on` (also `1`, `true`, `yes`) turns the guess back on for
  this release only; it is always off in an evaluation run (`AIHYDRO_EVAL_NONCE`, tested by
  `eval_condition.in_evaluation_run()`). When it fires the result carries a top-level
  `session_warning`. The project default replaces it.
- **Error.** `SESSION_RESOLUTION_FAILED` carries `candidates` (at most 5, each
  `{study_id, label, last_run}`, from the workspace, else the most recent visible studies) and
  `next_tools` naming `aihydro_rebind_chat`.
- **Recovery tools.** `aihydro_rebind_chat(study_id)` and `aihydro_chat_status()` read the chat id the
  host injected. An explicit `chat_id` must equal it (`CHAT_ID_MISMATCH` otherwise); without a chat
  context it must be a ULID and not a study id. `start_session(session_id)` binds the chat too (not
  with `shard_id`, not in an evaluation run).
- **Binding changes.** A call that changes a chat's binding adds
  `session_binding: {chat_id, study_id, label, previous_study_id, bound_by}` to its result. That field is
  how the host refreshes its display; the server store is the only authority, so a host must not send
  `_meta.study_id` back on every call.

The legacy hidden `_chat_id` argument is trusted only when the host injects no `_meta` chat id (the `_meta` value wins per field); a model-supplied `_chat_id` is not a supported way to choose a chat.

Explicit ids bind only an existing study (a typo is not bound). `start_session(session_id)` binds an existing study unless it belongs to a different workspace than the call's, and binds a newly created study only when the call carries a workspace. `aihydro_rebind_chat` refuses archived/test studies unless `include_hidden=true` and is unavailable in evaluation runs. With no call context (workspace from the global file) `workspace_single_study` serves read-only calls only.

A read-only tool (`readOnlyHint`) resolves its session with no side effects: it never rebinds the chat and never
backfills `workspace_dir`.

## What is recorded

Each run record's `extra` carries:

- `context_source`: `meta` (any field came from `_meta`), `legacy_args`, or
  `none`.
- `session_resolution`: the rule that selected the session: `explicit_arg`,
  `meta`, `chat_binding`, `result`, `writer_row`, `auto_create`, `workspace_single_study`, or `recent_fallback`. `writer_row` means
  the call wrote a run-log row into a session that matched neither the
  explicit argument nor the `_meta` study.
  A tool that calls `_resolve_session` reports the rule it used; otherwise the
  recording middleware derives it from the same order.
- `context_study_id` and `context_mismatch: true`: set when the record's
  session differs from the requested study (the `study_id` in `_meta` or an
  explicit `session_id` argument), for example a tool that wrote into another
  session.
- `context_client`: the `client` label, when sent.

## Where state lives

Chat bindings are `<AIHYDRO_HOME>/chat_bindings.v2.json` (`$AIHYDRO_HOME`, default `~/.aihydro`), resolved
when requested, so a test or CI job that sets `AIHYDRO_HOME` is isolated from the user's bindings.

- **Many chats, one study.** A chat has one study; a study may have many chats. There is no stored inverse, so
  binding a second chat never unbinds the first.
- **Chat ids** must be ULIDs and must not equal a study id (the live store held `basin_x -> basin_x`, a study id
  passed as a chat id).
- **v1 file.** `chat_studies.json` is read once, only while v2 does not exist, to seed v2 (the original is copied to
  `chat_studies.json.v1.bak`; entries that are not valid chat ids are skipped and counted in `meta`). After that it is
  never read. For one release it is rewritten as a derived v1-shaped projection (active, non-archived studies only)
  so the installed 0.4.0 extension's header chip keeps working. A write to the v1 file by an older server changes
  nothing v2 resolves. Give an older server (the pinned 2.3.7 used for P1) its own `AIHYDRO_HOME`: the projection
  would overwrite what it writes to a shared v1 file.

## Study recorder and study resources

Every file a tool writes into the workspace (through `HydroSession.write_workspace_file`) and every layer it pushes
(`map_events.push_*`) is noted on the call's capture and, once the call's run row is sealed, recorded in the owning
study's `artifact_manifest` by one function (`session/artifact_recorder.py`, called from
`run_records._record_call`). Entry: `{artifact_id, kind: "workspace_artifact", type, path, sha256, size_bytes,
study_id, run_id, tool, recorded_at}`; `run_id` is the call's run row. Not recorded: files written inside a subagent shard (filtered at the write helper),
files outside the study's workspace; files over 64 MB are listed with `hash_deferred: true` and no sha256, files a tool writes without those helpers.

- `aihydro://study/list{?workspace,include_hidden}`: studies with labels, read from the files without loading sessions.
  Archived studies and studies whose id starts `test-` / `e2e-proof-` and that have no workspace are hidden unless
  `include_hidden=true`; they are never deleted.
- `aihydro://study/by-artifact{?path,workspace}`: rule `manifest` (the manifest names the file and its sha256 still matches;
  `resolved` when exactly one study does) or rule `artifact_name_heuristic` for files written before the recorder (same
  directory as a study's workspace and the filename contains one of the study's identifiers). The heuristic returns
  candidates only; `resolved` is false and nothing is bound.
- `aihydro://capabilities`: capability flags (`binding.v2`, `study.by_artifact`, ...). Hosts detect features by these,
  not by version.

Study label (never a bare id): a user-set label, else the site name with its gauge alias ("Piscataquis River near
Dover-Foxcroft, ME (USGS 01031500)"), else the outlet ("Basin at 24.20°N, 75.75°E"), else "<id> (unnamed study)".

## FastMCP 2.14 note

FastMCP 2.14.7 does not copy `_meta` onto the middleware message
(`message.meta` is `None`). It is on the live request context,
`context.fastmcp_context.request_context.meta`, where `aihydro/context` is a
pydantic extra. The in-process `fastmcp.Client.call_tool(..., meta={...})`
sends it. Covered by `tests/test_explicit_context.py`.
