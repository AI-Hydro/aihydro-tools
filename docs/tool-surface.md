# Tool surface: annotations, ordering, and the hot listing

`tools/list` is the only thing a host reads before it decides which tools to show, approve or
gate. Three properties of it are fixed by the server (`ai_hydro/mcp/tool_surface.py`).

## Annotations

`ai_hydro/mcp/tool_annotations.toml` has one row per registered tool, including the tools
registered by `aihydro-data` and `aihydro-lsh` entry points. Each row carries the four MCP
`ToolAnnotations` hints and an `evidence` string that says why, from the code.
`apply_tool_annotations` stamps them on the tools once everything is registered, and they are
served in `tools/list`.

| Hint | `true` when |
| --- | --- |
| `readOnlyHint` | the tool writes nothing (no session slot or field, workspace file, map or preview command, claim, ledger, registry, job, cache entry, profile or skill) and calls no network, Earth Engine initialisation included |
| `destructiveHint` | it deletes, cancels, or overwrites researcher-authored state (claims, assumptions, interpretation, cleared slots, profile, skills, cache entries, a running job), or it can run arbitrary code |
| `idempotentHint` | repeating the call with the same arguments leaves the same state |
| `openWorldHint` | it calls a network service (NLDI, USGS, Earth Engine, Crossref, DEM and forcing servers) or runs arbitrary code |

Not counted as a write: the run record the middleware makes for every call, the run-log row
that `post_run` writes for the call itself, `_resolve_session` bookkeeping (chat binding,
one-time `workspace_dir` backfill), and creating an empty tool-owned cache root before listing
it. A recomputed derived slot or file is an overwrite of derived data, so it is not
destructive.

`tests/test_tool_surface.py` fails when a registered tool has no row, when a row has no tool
(rows for an optional plugin are enforced when that plugin is importable), and when a tool
marked `readOnlyHint = true` calls a known writer or the network.

Adding a tool means adding its row (and its tier in `TOOL_TIERS`).

## Order

`tools/list` is sorted by tool name. The order no longer depends on import order or
entry-point iteration order, so two servers built from the same tools return byte-identical
lists.

## `AIHYDRO_TOOL_SURFACE`

| Value | `tools/list` |
| --- | --- |
| `full` (default; any other value) | every registered tool |
| `hot` | the hot tools only: every Tier 1 tool plus `HOT_TOOL_ALLOWLIST` (`start_session`, `get_session_summary`, `data_fetch`, `compute_spectral_index`, `fetch_camels_us`, `run_python`) and the discovery tools (`aihydro_describe_capability`, `describe_tool`, `describe_tools`, `list_available_tools`) |

The VS Code extension does its own progressive disclosure from `_meta.hot`; other hosts
(Claude Code, Codex, Desktop) receive whatever `tools/list` returns, so `hot` is the way to
shrink their tool context. Tools left out of the hot listing stay registered and callable by
name; `aihydro_describe_capability(domain)`, `list_available_tools()` and `describe_tool(name)`
find them. When the server starts with `hot`, its instructions gain one paragraph saying so. A host
that refuses to call a tool it did not list will not reach them: keep `full` for such hosts.
Set it per server process in the host's MCP configuration
(`"env": {"AIHYDRO_TOOL_SURFACE": "hot"}`); the listing filter reads the variable on every
`tools/list`.

## Description cap

No tool description is longer than 1500 characters
(`tests/test_tool_description_cap.py`). Parameter documentation lives in the description, so a
long docstring is trimmed by tightening prose, never by dropping a parameter or an error code.
