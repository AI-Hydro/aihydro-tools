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

Session resolution (`_resolve_session`): explicit `session_id` argument, then
`study_id` from `_meta`, then the chat binding, then auto-create from a hint,
then the most recent study on disk, else an error.

## What is recorded

Each run record's `extra` carries:

- `context_source`: `meta` (any field came from `_meta`), `legacy_args`, or
  `none`.
- `session_resolution`: the rule that selected the session: `explicit_arg`,
  `meta`, `chat_binding`, `result`, `writer_row`, `auto_create`, or `recent_fallback`. `writer_row` means
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

The chat to study binding file is `<AIHYDRO_HOME>/chat_studies.json`
(`$AIHYDRO_HOME`, default `~/.aihydro`), resolved when requested, so a test or
CI job that sets `AIHYDRO_HOME` is isolated from the user's bindings.

## FastMCP 2.14 note

FastMCP 2.14.7 does not copy `_meta` onto the middleware message
(`message.meta` is `None`). It is on the live request context,
`context.fastmcp_context.request_context.meta`, where `aihydro/context` is a
pydantic extra. The in-process `fastmcp.Client.call_tool(..., meta={...})`
sends it. Covered by `tests/test_explicit_context.py`.
