# Tool surface: annotations, ordering, and the hot listing

`tools/list` is the only thing a host reads before it decides which tools to show, approve or
gate. Three properties of it are fixed by the server (`ai_hydro/mcp/tool_surface.py`).

## Annotations

`ai_hydro/mcp/tool_annotations.toml` has one row per registered tool, including the tools
registered by `aihydro-data` and `aihydro-lsh` entry points. Each row carries the four MCP
`ToolAnnotations` hints and an `evidence` string that says why, from the code.
`apply_tool_annotations` stamps them on the tools once everything is registered, and they are
served in `tools/list`.

A registered tool with no row (for example a third-party plugin tool) is served with conservative
explicit hints: `readOnlyHint=false`, `destructiveHint=true`, `idempotentHint=false`,
`openWorldHint=true` (hints the tool declares itself are kept). It is never read-only until it gets a
row. The test suite still requires a row for every tool this repo and its first-party plugins register.

| Hint | `true` when |
| --- | --- |
| `readOnlyHint` | the tool writes nothing (no session slot or field, workspace file, map or preview command, claim, ledger, registry, job, cache entry, profile or skill) and calls no network, Earth Engine initialisation included |
| `destructiveHint` | it deletes, cancels, or overwrites researcher-authored state (claims, assumptions, interpretation, cleared slots, profile, skills, cache entries, a running job), or it can run arbitrary code |
| `idempotentHint` | repeating the call with the same arguments leaves the same state |
| `openWorldHint` | it calls a network service (NLDI, USGS, Earth Engine, Crossref, DEM and forcing servers) or runs arbitrary code |

"Read-only" here means the call mutates no session or study state: no slot, field, claim, binding,
file or job. It is not "leaves no trace". A read-only call may still append sealed run-log rows, and
some mint citable ids: the series tools (`summarize_series`, `compare_series`, `detect_threshold_runs`,
`bootstrap_statistic`, `measure_feature`) return a `_run_id` that claims can cite as evidence. So a
`readOnlyHint = true` call may write only these things, and nothing else:

- its run records (the middleware's sealed run record and `post_run`'s run-log row for the call
  itself, `<session>.runlog.sqlite3`): evidence-minting about the call, not a change to the study;
- an empty tool-owned directory (an `aihydro-data` cache root) created with `mkdir`;
- interpreter housekeeping (`__pycache__`, matplotlib/fontconfig caches).

A read-only call also resolves its session **without side effects**: `ToolSurfaceMiddleware` marks
calls to tools whose row has `readOnlyHint = true`, and `_resolve_session` then neither rebinds the
chat to the session it read nor backfills `workspace_dir` (which saves the session and bumps its
mtime). Writers keep the old behaviour. A recomputed derived slot or file is an overwrite of
derived data, so it is not destructive.

### What is enforced, and what is not

The rows were classified by reading the code; they are not machine-proved. Two tests guard the
`readOnlyHint` claim, because hosts trust it:

- `tests/test_tool_surface.py` (static): every registered tool has a row and every row a tool
  (rows for an optional plugin are enforced when that plugin is importable), and no read-only tool
  calls a name on the deny-list (session/file/command writers, job starters, `subprocess`, `exec`,
  `eval`, `ee.*`, `fetch_*`/`download*`/`ensure_*`, `requests`/`httpx`). The scan follows helpers
  through module globals, function-local imports and `module.func` calls to depth 3. It is a
  deny-list: a new writer under a new name passes it.
- `tests/test_readonly_runtime_guard.py` (runtime): every read-only tool is called against a fixture
  session under `sys.addaudithook`; any file write, rename/remove, sqlite connection, socket/DNS
  event or subprocess outside the allowlist above fails. It sees only the branches the example
  arguments reach (a tool that refuses the arguments early proves little), so the two guards are
  complementary. A mutation test relabels every non-read-only tool as read-only and requires one
  guard to flag it (97 of 97).

Adding a tool means adding its row (and its tier in `TOOL_TIERS`).

## Order

`tools/list` is sorted by tool name. The order no longer depends on import order or
entry-point iteration order, so two servers built from the same tools return byte-identical
lists.

## `AIHYDRO_TOOL_SURFACE`

| Value | `tools/list` |
| --- | --- |
| `full` (default; any other value; always, in an evaluation run) | every registered tool |
| `hot` | the hot tools only: every Tier 1 tool plus `HOT_TOOL_ALLOWLIST` (`start_session`, `get_session_summary`, `data_fetch`, `compute_spectral_index`, `fetch_camels_us`, `run_python`) and the discovery tools (`aihydro_describe_capability`, `describe_tool`, `describe_tools`, `list_available_tools`) |

The VS Code extension does its own progressive disclosure from `_meta.hot`; other hosts
(Claude Code, Codex, Desktop) receive whatever `tools/list` returns, so `hot` is the way to
shrink their tool context. Tools left out of the hot listing stay registered and callable by
name; `aihydro_describe_capability(domain)`, `list_available_tools()` and `describe_tool(name)`
find them. When the server starts with `hot` (and not in an evaluation run), its instructions gain one paragraph saying so; the instructions are fixed at start, while the listing filter reads the variable on every `tools/list`. An evaluation run (`AIHYDRO_EVAL_NONCE` set, tested by `eval_condition.in_evaluation_run()`) forces `full`, so every arm sees the same tool surface. A host
that refuses to call a tool it did not list will not reach them: keep `full` for such hosts.
Set it per server process in the host's MCP configuration
(`"env": {"AIHYDRO_TOOL_SURFACE": "hot"}`); the listing filter reads the variable on every
`tools/list`.

## Description cap

No tool description is longer than 1500 characters
(`tests/test_tool_description_cap.py`). Parameter documentation lives in the description, so a
long docstring is trimmed by tightening prose, never by dropping a parameter or an error code.
