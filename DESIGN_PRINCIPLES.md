# AI-Hydro Design Principles

## Core thesis

> AI-Hydro ensures the LLM operates inside a versioned, validated, reproducible hydrology environment. It does not teach the LLM hydrology.

Every feature proposal passes or fails this sentence.

- Justification is "this constrains the LLM to operate within a verifiable scientific contract" → **yes**.
- Justification is "this teaches the LLM more hydrology" → **no**. The LLM already knows hydrology. What it lacks is a trustworthy environment to operate inside.

This sentence lives at the top of every architectural document and at the top of `CONTRIBUTING.md`.

---

## Trigger-based deferral rule

Every proposed `@mcp.tool()`, memory layer, registry file, knowledge structure, or validator requires **a documented failure of the simpler existing layer** before it may be added.

- Hypothetical failures do not count.
- The failure must appear in an `aihydro-bench` output or a reproducible session trace.
- If no benchmark or trace exists yet, open a draft PR, run the bench, document the failure, then un-draft.

**Why this rule exists:** tool count grew from 11 to 56 without it. Each addition was locally reasonable. Collectively they created a surface no one can hold in their head, a tool-discovery problem for the agent, and a maintenance burden that scales with contributor count.

The benchmark failure ID is the universal currency. It applies equally to tools, knowledge files, session fields, and validators. Four checkboxes, one currency. See `.github/PULL_REQUEST_TEMPLATE.md`.

---

## Tool tiering

Every `@mcp.tool()` has a numeric `tier` (1/2/3), defined once in
`ai_hydro/mcp/app.py::TOOL_TIERS` — the single source of truth. Enforcement,
and whether the tool's full schema is injected (`hot`), both depend on tier.
"Tier" has exactly one meaning in AI-Hydro; it is **not** a complexity or
library-vs-wrapper axis (how tools compose is a separate question — see below).

| Tier | What belongs here | Enforcement |
|---|---|---|
| **1** (scientific) | Tools whose outputs a paper could cite — signatures, watershed, modelling, validators. Automatically `hot`. | Mandatory: provenance + quality checks + uncertainty + citations + claim binding when inside a workflow |
| **2** (workflow) | Data fetch, session ops, export, project management | Provenance + citations |
| **3** (infrastructure) | Ledger ops, knowledge access, skill loading, session housekeeping | Provenance timestamp only |

Rules:
- Tier is assigned at tool registration in `TOOL_TIERS`, not inferred.
- A tool that returns a number a hydrologist might argue about is Tier 1. When in doubt, assign Tier 1.
- Validators are Tier 3 — they produce `ValidatorResult`, not `HydroResult`, and do not need their own validators.
- The escape hatch `acknowledged_compromise=True` is available only on Tier 1 tools, is logged, and surfaces in the capsule README as a flagged exception.

> **Authoring a tool that respects these contracts:** see [`knowledge/tools/AUTHORING_GUIDE.md`](knowledge/tools/AUTHORING_GUIDE.md) for the concrete conventions — how tier maps to the `hot` injection flag, domain-prefix naming, parameter naming so the argument-repair middleware and self-correcting errors work, and the session-resolution pattern. Loadable live as the `mcp-tool-authoring` skill.

---

## Three primitives (composition, not tiers)

Tier is an *enforcement/injection* axis. **Composition** — how capability is
packaged and combined — is a separate axis with three primitives. An earlier
design tried to encode complexity into the tier number (library → wrapper →
workflow); that conflated two things. The clean model:

| Primitive | Is | Use it for | Where |
|---|---|---|---|
| **MCP tool** | One atomic, typed, enforced capability | A single deterministic action with a verifiable contract | `@mcp.tool()` in `ai_hydro/mcp/` |
| **Skill** | A workflow playbook composing tools | A multi-step procedure / methodology the agent follows | `knowledge/tools/skills/`, `knowledge/workflows/*.yaml` |
| **Package knowledge** | Reference / domain facts | Definitions, datasets, equations the agent reasons *with* | `knowledge/concepts/`, `knowledge/datasets/`, etc. |

Decision rule when adding capability:

- Is it **one verifiable action**? → an MCP tool (pick the tier; keep it atomic).
- Is it **a sequence of existing tools / a methodology**? → a skill, not a new
  tool. Don't hardcode a multi-step pipeline as one mega-tool.
- Is it **knowledge the model should reason with, not execute**? → package
  knowledge. (DESIGN_PRINCIPLES: the system provides the environment, not the
  hydrology — but reference data lives here when it's load-bearing.)

This is why long pipelines became *skills*, and why the agent's runtime view is
the three discovery primitives, not a complexity ladder. See
`AGENT_EXECUTION_MODEL.md` for how each is presented and executed.

---

## Load-bearing paths

These are the 6 canonical tool chains a real hydrology study executes. Enforcement contracts must hold across the full path, not just individual tools.

| Path | Tools (in order) |
|---|---|
| Watershed-to-signatures | `start_session` → `delineate_watershed` → `fetch_streamflow_data` → `extract_hydrological_signatures` → `write_research_interpretation` |
| Calibration | `fetch_forcing_data` → `train_hydro_model` → `get_training_status` → `get_model_results` → `add_claim` |
| Flood frequency | `fetch_streamflow_data` → skill: `flood-frequency-analysis` → `add_claim` |
| CAMELS comparison | `fetch_camels_us` → `extract_hydrological_signatures` → `search_experiments` → `write_research_interpretation` |
| Ungauged transcription | skill: `ungauged-basin-transcription` → `run_python` → `add_claim` |
| Capsule export | `get_session_raw_state` → `write_research_interpretation` → `export_session` |

New tools that do not appear in any of these paths require stronger justification for addition.

---

## Approval semantics for write-requiring tools

"Write requires approval" means the tool checks a record created by a prior, explicit human action in a channel the model cannot call. A boolean tool argument cannot do this: the agent supplies every argument, so `researcher_approved=True` was self-approvable (reproduced, evidence report §3b).

Pattern (from `promote_claim_to_registry`, ADR-002a):
```python
approval = find_approval(session_id, claim_id, claim_revision_digest(claim))
if approval is None:
    raise ApprovalRequiredError(...)   # code APPROVAL_REQUIRED, names `aihydro-approve`
```

The record is written only by the `aihydro-approve` CLI after the human types the claim's revision digest in a terminal; no MCP tool can write it, and editing the claim invalidates it. `researcher_approved` remains as a request flag and is never sufficient. Residual limit: a process running as the same OS user can still write the approvals file (see `docs/evidence-integrity.md`, "Human approval"). Use this model for destructive or irreversible operations.

---

## What this design is not

- **Not a hydrology teaching system.** The LLM knows hydrology. The system provides the environment it operates inside, not the knowledge it reasons with.
- **Not an ontology.** `MEMORY_TAXONOMY.md` is a diagnostic table, not a system architecture. Build only the layers you need; use the table to decide where new things belong.
- **Not a chatbot with tools.** The agent is a scientific reasoning engine constrained by typed contracts. The contracts are the product, not the chat interface.
