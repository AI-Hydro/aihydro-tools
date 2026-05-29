---
name: mcp-tool-authoring
description: Conventions for adding a new AI-Hydro MCP tool so it is born compliant with context-injection, argument-repair, self-correcting errors, and session resolution.
when_to_use: When scaffolding, writing, or reviewing a new @mcp.tool() function (built-in module or community plugin via the aihydro.tools entry point).
domain: development
tools_used:
  - list_available_tools
  - describe_tool
tags:
  - authoring
  - conventions
  - mcp
  - contributing
---

# Authoring a new AI-Hydro MCP tool

Use this when adding or reviewing an `@mcp.tool()`. Full reference:
`knowledge/tools/AUTHORING_GUIDE.md`. The driving model is weak — design so the
right call is obvious and the wrong call self-corrects.

## Steps

1. **Name + domain.** Verb-first snake_case (`compute_*`, `fetch_*`, `extract_*`,
   `list_*`, `get_*`). Match an existing prefix in
   `tools_discovery.py::_DOMAIN_PREFIXES` so the tool groups correctly. New
   domain? Add the prefix (longest-prefix wins).

2. **Tier + hot.** Set tier in `app.py::TOOL_TIERS` (default 2): Tier-1 =
   scientific artifact (auto-hot, add a post-run validator), Tier-2 =
   workflow/data, Tier-3 = infra/discovery. A tool is *hot* (full schema inline)
   iff tier==1 or in `HOT_TOOL_ALLOWLIST`. Keep the hot set ~22; only add
   near-every-session tools. Everything else is summary-level — visible by name,
   one `describe_tool` away. `_meta` is stamped automatically; do not set it.

3. **One-line summary.** The first docstring line is ALL that summary-level
   injection shows. Make it self-contained, action-first, < ~100 chars, naming
   valid values inline. Deprecated? Prefix `[DEPRECATED — prefer <replacement>]`.

4. **Parameters.** Type-hint every param (drives JSON-schema coercion + error
   help). Use canonical names matching `arg_repair.py::_GLOBAL_ALIASES`
   (`index_name`, `geometry`, `gauge_id`, `session_id`, `latitude`, `longitude`,
   `start`, `end`, `frequency`, `resolution`) so aliases route in; add a new
   alias if the model will mis-name a new param. Required = no default;
   optional = sensible default. Keep the signature flat (scalars + `list[str]`).

5. **Session.** Resolve via `_resolve_session(session_id, None)` (walks explicit
   → chat binding → auto-create → most-recent fallback → actionable error). Read
   slots with `session.get(slot)`, write with `session.set(slot, value)` —
   **there is no `get_slot`**. Call `_maybe_set_workspace(session)` if writing
   files. Use `allow_auto_create=False` for admin/query tools.

6. **Output.** Return a lean JSON dict; reference large arrays via a `_data_file`
   pointer. On failure raise a typed error (`ToolError` /
   `SessionResolutionError` with `recovery` + `next_tools`) — the middleware
   turns it into self-help. Never return a bare stack trace.

7. **Register.** Add `@mcp.tool()`, import the module in `mcp/__init__.py` (or
   declare an `aihydro.tools` entry point). Tier-1: also
   `register_post_validator(...)`. Write a registration + smoke test; run
   `pytest -m "not live"`.

## Template

```python
from __future__ import annotations
from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _resolve_session, _maybe_set_workspace
from ai_hydro.session import HydroSession

@mcp.tool()
def compute_my_metric(session_id: str | None = None, threshold: float = 0.5) -> dict:
    """Compute <action-first one-liner naming valid values> for the bound study."""
    session_id = _resolve_session(session_id, None)
    session = HydroSession.load(session_id)
    _maybe_set_workspace(session)
    cached = session.get("my_metric")
    if cached:
        return {**cached, "_cached": True}
    result = {"value": 0.42, "threshold": threshold}
    session.set("my_metric", result)
    return result
```

## Checklist
- [ ] Verb-first name matching a domain prefix
- [ ] Self-contained one-line docstring summary
- [ ] Type hints; canonical/aliased param names; required vs default correct
- [ ] `_resolve_session` + `.get`/`.set` slots
- [ ] Tier set; hot only if high-frequency
- [ ] Lean JSON dict output; typed errors
- [ ] Imported/entry-pointed; Tier-1 validator; test added
