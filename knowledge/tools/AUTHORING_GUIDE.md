# AI-Hydro MCP Tool Authoring Guide

How to add a new MCP tool so it is **born compliant** with the conventions
settled during the reliability/context-injection work (WS-1…WS-5). Following
this guide means your tool gets correct context-injection (hot vs summary),
argument-repair, self-correcting errors, and session resolution **for free** —
no follow-up cleanup pass.

> Audience: contributors adding `@mcp.tool()` functions to `ai_hydro/mcp/` or
> to a community plugin via the `aihydro.tools` entry-point group.

---

## 0. Should this be a tool at all? (decision gate)

Answer these **before** writing code. Most "I need a new tool" turns out to be a
skill or a parameter.

1. **Is it one atomic, verifiable action?** If it's a multi-step pipeline, it's a
   **skill**, not a tool (see `DESIGN_PRINCIPLES.md` → Three primitives). If it's
   reference knowledge the model reasons *with*, it's **package knowledge**.
2. **Has the simpler existing layer demonstrably failed?** Trigger-based deferral
   (`DESIGN_PRINCIPLES.md`): you need a documented benchmark/trace failure, not a
   hypothetical. Tool count grew 11 → 56 → ~100 without this discipline.
3. **Which tier?** (set in `app.py::TOOL_TIERS`)
   - Returns a number a hydrologist could cite/argue about → **Tier 1** (auto-hot,
     gets a validator). When in doubt, Tier 1.
   - Data fetch / workflow op → **Tier 2**. Infra / state / discovery → **Tier 3**.
4. **Will it run > ~30 s, or want to run in parallel / be cancellable?** Then it
   must be a **job**, not a blocking tool — see §7.5 and `AGENT_EXECUTION_MODEL.md`.

If 1–2 don't clear the gate, stop — don't add the tool.

---

## 1. The 90-second mental model

The MCP server is the **single source of truth** for how a tool is presented to
the (weak) driving model. Three machines read your tool and each keys off a
specific part of it:

| Machine | Reads | You control via |
|---|---|---|
| **Context injector** (`mcp.ts` renderer) | `_meta.hot`, `_meta.tier`, `_meta.domain`, first docstring line | tool name prefix + `TOOL_TIERS`/allowlist |
| **Argument-repair middleware** (`arg_repair.py`) | declared parameter names + JSON types | clear param names + type hints |
| **Self-help error path** | `inputSchema`, `required` | type hints + which params have defaults |

Design for a **weak model** (`deepseek-v4-flash`): it guesses parameter names
and loops on errors. Your job is to make the right call obvious and the wrong
call self-correcting.

---

## 2. Naming + domain

The tool **name prefix** decides its domain bucket (used for grouping in the
summary-line view). Match an existing prefix in
`tools_discovery.py::_DOMAIN_PREFIXES` so your tool lands in the right group:

```
watershed   → delineate_*, compute_twi, create_cn_grid, extract_geomorphic_*, merit_*
streamflow  → fetch_streamflow_*, extract_hydrological_*, separate_baseflow
data_fetch  → data_*
session     → start_session, *_session, add_note
maps        → map_*, gee.*, show_on_map
...
```

Rules:
- **Verb-first, snake_case**: `compute_*`, `fetch_*`, `extract_*`, `list_*`, `get_*`.
- Reuse a known prefix so domain matching works; if you need a new domain, add it
  to `_DOMAIN_PREFIXES` (longest-prefix wins).
- Tools that match no prefix get domain `general` — avoid that.

---

## 3. Choosing tier + hot

**Tier** (set in `app.py::TOOL_TIERS`; defaults to 2 if unlisted):
- **Tier 1** — produces a *scientific result/artifact* the researcher cites
  (signatures, delineation, TWI, CN, geomorphic, model results, validators).
  Tier-1 tools are automatically **hot** and may get a post-run validator.
- **Tier 2** — workflow / data acquisition (fetch, compute index, run_python).
- **Tier 3** — infrastructure / discovery / state (list_*, get_*_status, map_*).

**Hot** (full schema injected inline; everything else is a summary line fetched
on demand via `describe_tool`). A tool is hot iff `is_hot_tool(name)` →
`tier == 1` **or** name ∈ `HOT_TOOL_ALLOWLIST` (`app.py`).

> Keep the hot set small (~22 tools). Only add to `HOT_TOOL_ALLOWLIST` if the
> tool is called in nearly every session (e.g. `start_session`, `data_fetch`).
> Everything else is summary-level — still fully visible by name, one
> `describe_tool` round-trip away. **Do not make a tool hot to "help" the model;
> that bloats context and hurts every session.**

You do **not** set `_meta` yourself — `_tag_tools_with_tier_meta()` in
`__init__.py` stamps `tier`/`domain`/`hot` onto every tool after registration.

---

## 4. The one-line summary (first docstring line)

Summary-level injection shows **only your tool name + the first line of the
docstring**. Make that line a self-contained, action-first sentence:

```python
def compute_spectral_index(index_name: str, session_id: str | None = None) -> dict:
    """Compute a spectral index (NDWI, NDVI, NDBI, NBR, MNDWI, …) for the bound study."""
```

- One line, < ~100 chars, no leading blank line.
- State *what it produces*, not *how*. Name common valid values inline.
- For a **deprecated** tool, prefix `[DEPRECATED — prefer <replacement>]` so the
  model is steered away (see `fetch_streamflow_data`).

---

## 5. Declaring parameters (so repair + errors behave)

The argument-repair middleware (`arg_repair.py`) renames aliased/typo'd keys and
coerces obvious type mismatches **before** your body runs, then returns a
schema-rich self-help payload if the call still fails. To make it work for you:

- **Use canonical parameter names** that match the global alias table
  (`_GLOBAL_ALIASES`): `index_name`, `geometry`, `gauge_id`, `session_id`,
  `latitude`, `longitude`, `start`, `end`, `frequency`, `resolution`. If your
  param is a synonym of one of these, **rename it to the canonical** so existing
  aliases route to it. If you introduce a genuinely new param that the model
  will mis-name, add an alias to `_GLOBAL_ALIASES`.
- **Always add type hints** (`str`, `int`, `float`, `bool`, `list[str]`). They
  become the JSON schema `type` that drives coercion (`"28.2"`→`28.2`,
  `"B4"`→`["B4"]`) and the self-help schema shown on failure.
- **Required vs optional**: required params have no default; optional ones get a
  default (usually `None`). The worked example in `describe_tool` / error
  payloads includes required params + any with a non-None default — so give
  optional params sensible defaults.
- Keep the **signature flat** (scalars + `list[str]`). Avoid nested dict params;
  the weak model fills those poorly and the repair shim can't help.

---

## 6. Session resolution

Never require the model to pass `session_id` when it can be inferred. Resolve it
through the shared helper, which walks: explicit id → chat binding → auto-create
from hint → most-recent-study fallback → actionable error.

```python
from ai_hydro.mcp.helpers import _resolve_session
from ai_hydro.session import HydroSession

def my_tool(session_id: str | None = None, ...) -> dict:
    session_id = _resolve_session(session_id, None)   # chat_id injected by server
    session = HydroSession.load(session_id)
    cached = session.get("my_slot")        # NOTE: .get(slot) / .set(slot, value)
    ...                                    # there is NO get_slot()
    session.set("my_slot", result)
    return {...}
```

- Read/write session slots with `session.get(slot)` and `session.set(slot, value)`
  (dynamic slots — plugins may add their own). **`get_slot` does not exist.**
- For a tool that must *not* auto-resolve (admin/query), call
  `_resolve_session(..., allow_auto_create=False)`.
- Call `_maybe_set_workspace(session)` if your tool writes files, so outputs land
  in the VS Code project dir.

---

## 7. Output conventions

- Return a **plain `dict`** (JSON-serializable). On success, include the
  scientific payload plus any `_data_file` pointer for large arrays (don't dump
  megabytes of series into the response).
- On failure, prefer raising a typed error (`ToolError` / `SessionResolutionError`
  with `recovery` + `next_tools`) — the middleware turns exceptions into
  self-help. Don't return bare stack-trace strings.
- Keep responses lean: the response also costs context. Summarize; reference
  files; don't echo inputs back verbatim.

---

## 7.5 Long-running tools must be jobs (not blocking calls)

An MCP call must return fast. If your tool can exceed ~30 s, may run in parallel,
or might need cancelling, **do not block** — kick off detached work and return a
`job_id` immediately, then expose poll/result tools.

The pattern today lives in `tools_modelling.py` (`train_hydro_model` →
`get_training_status` → `get_model_results`): write a `job_config.json`, spawn a
detached `subprocess.Popen(..., start_new_session=True)` running a
`python -m <pkg>.runner <dir>` entry that writes `status.json` checkpoints, and
return `{job_id, status:"pending", log_path}`. The agent polls; results are read
from the artifact dir.

> A shared `ai_hydro/mcp/jobs.py` (start/status/result/**cancel**/list + a PID
> registry) is the planned generalization — see `AGENT_EXECUTION_MODEL.md` §3.
> Until it lands, copy the modelling pattern **and persist the PID** so the work
> is cancellable.

---

## 8. Registration

```python
# in ai_hydro/mcp/tools_<area>.py
from ai_hydro.mcp.app import mcp

@mcp.tool()
def my_tool(...) -> dict:
    """One-line summary. ..."""
```

Then import the module in `ai_hydro/mcp/__init__.py` so its decorators run.
Community plugins instead declare an entry point:

```toml
[project.entry-points."aihydro.tools"]
my_tool = "my_pkg.module:my_tool"
```

If Tier-1, also register a post-run validator in `__init__.py` via
`register_post_validator(name, validator_fn, kwargs_builder)`.

---

## 9. Copy-paste template

```python
from __future__ import annotations
from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _resolve_session, _maybe_set_workspace
from ai_hydro.session import HydroSession


@mcp.tool()
def compute_my_metric(
    session_id: str | None = None,
    threshold: float = 0.5,
) -> dict:
    """Compute <one-line, action-first, names valid values> for the bound study."""
    session_id = _resolve_session(session_id, None)
    session = HydroSession.load(session_id)
    _maybe_set_workspace(session)

    cached = session.get("my_metric")
    if cached:
        return {**cached, "_cached": True}

    # ... deterministic computation ...
    result = {"value": 0.42, "threshold": threshold}

    session.set("my_metric", result)
    return result
```

---

## 10. Pre-merge checklist

- [ ] Name is verb-first snake_case and matches a `_DOMAIN_PREFIXES` prefix.
- [ ] First docstring line is a self-contained one-liner (the summary view).
- [ ] Every param has a type hint; canonical names used (or alias added).
- [ ] Required params have no default; optional ones have sensible defaults.
- [ ] `session_id` resolved via `_resolve_session`; slots via `.get`/`.set`.
- [ ] Tier set in `TOOL_TIERS` if Tier-1/3; added to `HOT_TOOL_ALLOWLIST`
      only if truly high-frequency.
- [ ] Returns a lean JSON dict; large arrays referenced by file pointer.
- [ ] Module imported in `__init__.py` (or entry-point declared).
- [ ] Tier-1 only: post-run validator registered.
- [ ] A test exists (registration + a smoke call). Run `pytest -m "not live"`.

See also: `CONTRIBUTING.md`, `DESIGN_PRINCIPLES.md` (tiers, three primitives,
trigger-based deferral), `AGENT_EXECUTION_MODEL.md` (how tools are presented,
made reliable, and run as jobs), `TOOL_AUDIT.md`.
