# AI-Hydro Agent Execution Model

> Companion to `DESIGN_PRINCIPLES.md`. That doc says *what the agent is allowed
> to do and why*. This doc says *how the tool surface is presented to the agent,
> how calls are made reliable, and how long-running / parallel work runs* — the
> machinery that makes a weak driving model behave like a production agent
> (Claude Code / Codex class) inside an MCP-tool world.

**Core rule of this model:**

> **An MCP tool call must return fast. Slowness becomes a *job*, never a blocked
> pipe.** Anything that can exceed ~30 s, or that the user may want to run in
> parallel or cancel, is kicked off as a detached OS process and polled — it
> does not block the MCP transport.

Everything below either serves that rule or serves *context economy* (don't
swamp the prompt) and *call reliability* (don't let a weak model fail on a
guessed parameter name).

---

## 1. The four mechanisms (what exists today)

| # | Mechanism | Purpose | Where it lives | Status |
|---|-----------|---------|----------------|--------|
| 1 | Tiered progressive disclosure | Keep the prompt lean across ~100+ tools | `mcp/app.py` (`TOOL_TIERS`, `HOT_TOOL_ALLOWLIST`, `is_hot_tool`), `mcp/__init__.py` (`_tag_tools_with_tier_meta`), extension `system-prompt/components/mcp.ts` | **Solid — keep** |
| 2 | Discovery protocol | Fetch full schema on demand for the long tail | `mcp/tools_discovery.py` (`aihydro_describe_capability`, `describe_tool`, `describe_tools`, `list_available_tools`) | **Solid — keep** |
| 3 | Reliability middleware | Turn near-miss calls into successes / teaching turns | `mcp/arg_repair.py` (FastMCP `Middleware`), `mcp/enforcement.py` | **Strongest piece — keep, use as template** |
| 4 | Async jobs | Run long / parallel / cancellable work without blocking MCP | `mcp/tools_modelling.py` (only) | **Incomplete — generalize (see §3)** |

### 1.1 Tiered progressive disclosure

- Tier is assigned **at registration** in `TOOL_TIERS` (1 = scientific output a
  paper could cite, 2 = workflow, 3 = infrastructure). Single source of truth.
- **Hot** = `tier == 1` OR member of `HOT_TOOL_ALLOWLIST`. Hot tools get their
  **full input schema** injected into the system prompt (zero round-trip to call
  correctly). Everything else is injected as a **one-line summary** grouped by
  domain — always *listed*, never hidden.
- `_tag_tools_with_tier_meta()` stamps `_meta.{tier,hot,domain}` onto each MCP
  tool; the extension is a **pure renderer** of that intent. Docs, agent
  context, and runtime can't drift because they read the same registry.

Keep the hot allowlist **small and high-frequency**. It is the one part that
rots by hand — guard it with a test (see §5).

### 1.2 Discovery protocol (the on-demand half)

```
aihydro_describe_capability()          → domain inventory (counts)
aihydro_describe_capability("watershed")→ 1 line per tool in that domain
describe_tool("compute_twi")           → full schema + worked example call
describe_tools(["a","b"])              → batch, before chaining several
list_available_tools()                 → every tool name
```

`describe_tool` returns a **copy-pasteable `example_call`** built from required
params + sensible placeholders — a weak model needs an *example*, not just a
spec. This is what makes deferral safe.

### 1.3 Reliability middleware (cross-cutting, uniform, free)

Implemented as a FastMCP `Middleware` sitting in front of **every** tool, so new
tools inherit it with no per-tool code:

1. **Silent repair** — alias table (`index`→`index_name`, `geojson`→`geometry`,
   `gauge`→`gauge_id`, `session`→`session_id`, …) + fuzzy match against the
   tool's real params + obvious type coercion. If repair makes the call valid,
   it just succeeds; the model never sees the mistake.
2. **Teaching error** — if still invalid, the response carries: what was wrong in
   plain language, the correct schema, a corrected example call, and the closest
   valid param names. The fix travels *in the error*.
3. **Retry-loop breaker** — repeated identical failing call escalates guidance.
4. **Session auto-resolution** — missing `session_id` falls back to the active
   session and tells the agent which one it used.

> **This is the template for all cross-cutting behavior.** New reliability,
> provenance, or enforcement concerns should be middleware, not per-tool code.

---

## 2. Why MCP-tools (not bash) — and the honest tradeoffs

AI-Hydro runs almost all work through typed MCP tools rather than free-form bash.
That is deliberate: the typed contracts *are* the product (DESIGN_PRINCIPLES),
they carry provenance/validation/citation enforcement that bash cannot. There is
still an arbitrary-execution escape hatch — **`run_python`** (a hot tool) — for
the genuinely open-ended case.

**The real limitations of the MCP transport** (and how this model answers them):

| Concern | Reality | Answer |
|---------|---------|--------|
| "Can't run servers in parallel" | One stdio server serializes calls over one pipe; the agent loop awaits each call | Parallelism comes from **detached job processes** (§3), not from the transport |
| "Can't conveniently kill a call" | `McpHub.callTool` is a blocking request with only a per-server timeout; no clean per-call cancel | The heavy work is a **killable OS subprocess**; the MCP call returns in ms. Add `cancel_job` to kill by stored PID |
| "Long task blocks everything" | A blocking tool stalls the whole agent turn | **Kickoff-only** tools return `{job_id}` immediately |

The conclusion is *not* "switch to bash." It's "make every slow tool a job."

---

## 3. The async-job contract (standard going forward)

Today only modelling does this, and incompletely. `train_hydro_model`:
- spawns a detached `subprocess.Popen([... "-m", "ai_hydro.modelling.runner", artifact_dir], start_new_session=True)`,
- writes `job_config.json` + `status.json` into a per-job `runs/<job_id>/` dir,
- returns `{job_id, status: "pending", log_path, ...}` immediately,
- is polled by `get_training_status(job_id)` reading `status.json` checkpoints,
- final artifact read by `get_model_results`.

**Good instinct. Gaps to close when generalizing:**
- **PID is logged then discarded** → no cancellation possible. *Must persist it.*
- Status files searched across ad-hoc locations → needs a **registry**.
- No orphan cleanup, no restart recovery, no uniform shape.

### Proposed shared module: `ai_hydro/mcp/jobs.py`

A standard any long-running tool adopts:

```
start_job(kind, runner_module, config) -> {job_id, status:"pending", ...}
    # writes ~/.aihydro/jobs/<job_id>/{config.json, status.json, job.log}
    # registers {job_id, pid, kind, started_at, artifact_dir} in a registry
    # spawns detached subprocess (start_new_session=True), persists PID
get_job_status(job_id) -> {status, progress, partial_results, error, log_path}
get_job_result(job_id) -> final artifact (or error if not done)
cancel_job(job_id)     -> kills the stored PID (os.killpg), marks "cancelled"
list_jobs(kind?)       -> active/recent jobs for this user
```

- **Registry**: `~/.aihydro/jobs/registry.json` (or one dir per job). Source of
  truth for PID + status, so cancel and restart-recovery work.
- **Runner convention**: every job kind ships a `python -m <pkg>.runner <dir>`
  entry that reads `config.json` and writes `status.json` checkpoints. Modelling
  becomes the first adopter, not a special case.
- **Adoption rule**: a tool uses jobs when expected runtime > ~30 s, OR it is
  parallelizable, OR the user may want to cancel it. Fast tools stay synchronous
  — jobs are overkill for them.
- The job tools reuse mechanisms 1–3 verbatim (tiering, `describe_tool`,
  teaching errors). No new context machinery.

---

## 4. Subagents = jobs that run their own tool loop

A subagent is **not** a new transport or framework. It is a job whose runner
drives a constrained agent loop:

- `start_job(kind="subagent", runner="ai_hydro.agents.runner", config={goal, tool_allow=[...], tier_max, budget})`
- The subprocess runs an inner loop over a **tier-restricted** toolset, writing
  progress/checkpoints to `status.json` exactly like the modelling runner.
- The parent polls with `get_job_status`, cancels with `cancel_job`, collects
  with `get_job_result`.

This gives parallel subagents (N detached processes), cancellation, and budget
control with **zero** new infrastructure beyond §3. Scope discipline
(DESIGN_PRINCIPLES trigger-based deferral): build the subagent runner only when a
benchmarked single-loop failure demands it.

---

## 5. The standard, in one page

1. **Disclosure**: tier at registration; hot = Tier1 + small allowlist; long tail
   is listed + summarized, schema fetched via `describe_tool`. Keep the allowlist
   small; **test** that every allowlisted name exists and every registered tool
   has a tier.
2. **Reliability**: every cross-cutting concern is **middleware**, uniform across
   all tools. Wrong calls are repaired silently or returned as teaching turns —
   never raw stack traces.
3. **Execution**: MCP calls return fast. Slow/parallel/cancellable work is a
   **job** (`jobs.py`): detached process, persisted PID, registry, `cancel_job`.
   `run_python` is the open-ended escape hatch.
4. **Subagents**: a job whose runner runs a tier-restricted tool loop. Same
   contract, no new framework.
5. **Adding anything** (tool, middleware, job kind): requires a documented
   failure of the simpler existing layer — a benchmark ID or session trace, per
   DESIGN_PRINCIPLES trigger-based deferral. Tool count already grew 11 → 56 →
   ~100 without this discipline; the rule is what keeps the surface holdable.

---

## 6. Composition: three primitives

This doc covers the *tool* surface. Capability is packaged three ways, and the
choice is orthogonal to execution tier (see `DESIGN_PRINCIPLES.md` → Three
primitives):

- **MCP tool** — one atomic, typed, enforced action. Presented via the tiered
  disclosure in §1; run synchronously (fast) or as a job (§3).
- **Skill** — a workflow playbook composing tools. The agent loads it on demand;
  it is *not* a new tool. Long pipelines belong here, not in a mega-tool.
- **Package knowledge** — reference facts the agent reasons *with*, not executes.

When adding capability, decide which primitive first (authoring gate:
`knowledge/tools/AUTHORING_GUIDE.md` §0). A new tool needs a documented failure
of the simpler layer (trigger-based deferral).

---

## Related docs

- `DESIGN_PRINCIPLES.md` — what the agent may do and why (tiers, three primitives, deferral, approval)
- `knowledge/tools/AUTHORING_GUIDE.md` — how to write a born-compliant tool (the authoring half of this contract)
- `docs/guide/tool-discovery.md` (extension repo) — published version of §1
- `mcp/app.py` — tier + hot source of truth
- `mcp/tools_discovery.py` — discovery protocol
- `mcp/arg_repair.py` — reliability middleware
- `mcp/tools_modelling.py` — current (only) async-job implementation → template for `jobs.py`
