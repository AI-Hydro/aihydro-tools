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

Every `@mcp.tool()` carries a `tier` field in its registration metadata. Enforcement depends on tier.

| Tier | What belongs here | Enforcement |
|---|---|---|
| `"scientific"` | Tools whose outputs a paper could cite — signatures, watershed, modelling, validators | Mandatory: provenance + quality checks + uncertainty + citations + claim binding when inside a workflow |
| `"workflow"` | Data fetch, session ops, export, project management | Provenance + citations |
| `"infrastructure"` | Ledger ops, knowledge access, skill loading, session housekeeping | Provenance timestamp only |

Rules:
- Tier is assigned at tool registration, not inferred.
- A tool that returns a number a hydrologist might argue about is Tier 1. When in doubt, assign Tier 1.
- Validators are Tier 3 — they produce `ValidatorResult`, not `HydroResult`, and do not need their own validators.
- The escape hatch `acknowledged_compromise=True` is available only on Tier 1 tools, is logged, and surfaces in the capsule README as a flagged exception.

> **Authoring a tool that respects these contracts:** see [`knowledge/tools/AUTHORING_GUIDE.md`](knowledge/tools/AUTHORING_GUIDE.md) for the concrete conventions — how tier maps to the `hot` injection flag, domain-prefix naming, parameter naming so the argument-repair middleware and self-correcting errors work, and the session-resolution pattern. Loadable live as the `mcp-tool-authoring` skill.

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

"Write requires approval" does not mean real-time human sign-off in an agentic loop — there is no human in the approval path at inference time. It means the tool checks a session-level flag set by a prior, explicit human-triggered action before executing.

Pattern (from `promote_claim_to_registry`):
```python
def promote_claim_to_registry(session_id: str, claim_id: str, researcher_approved: bool = False) -> dict:
    if not researcher_approved:
        raise ValueError("Researcher approval is required.")
```

This is the model for all destructive or irreversible operations. The `researcher_approved` flag is set by the researcher explicitly in the tool call, not by the agent. The agent cannot self-approve.

---

## What this design is not

- **Not a hydrology teaching system.** The LLM knows hydrology. The system provides the environment it operates inside, not the knowledge it reasons with.
- **Not an ontology.** `MEMORY_TAXONOMY.md` is a diagnostic table, not a system architecture. Build only the layers you need; use the table to decide where new things belong.
- **Not a chatbot with tools.** The agent is a scientific reasoning engine constrained by typed contracts. The contracts are the product, not the chat interface.
