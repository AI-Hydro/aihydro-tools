---
name: hydro-skill-creator
description: Create new AI-Hydro workflow skills and iteratively improve them. Use when the user wants to capture a hydrological workflow as a reusable skill, turn a completed analysis into a SKILL.md, author a new skill from scratch, or improve an existing skill's instructions or triggering accuracy.
when_to_use: When the user says "save this as a skill", "create a skill for X", "turn this workflow into a skill", wants to capture a repeatable hydrology workflow, or asks to improve an existing AI-Hydro skill
domain: general
tools_used:
  - save_skill
  - get_session_summary
tags:
  - skill-authoring
  - workflow-capture
  - meta
  - agent-created
---

# AI-Hydro Skill Creator

Create reusable hydrological workflow skills that guide the AI-Hydro agent through multi-step analyses. Skills are SKILL.md files following the Agent Skills open standard — they get injected into the agent's system prompt and guide future conversations.

## When to Use This Workflow

- User just completed a multi-step analysis and says "save this as a skill"
- User wants to create a skill for a specific hydrology workflow from scratch
- User wants to improve an existing skill's instructions or triggering

## The Creation Process

1. **Capture intent** — understand what the skill should do
2. **Draft the SKILL.md** — write frontmatter + workflow steps
3. **Test it** — run the agent with the skill on a realistic prompt
4. **Iterate** — refine based on what worked and what didn't
5. **Save** — call `save_skill()` to persist to `~/.aihydro/skills/agent-created/`

## Step 1 — Capture Intent

### If capturing from a completed workflow:

Extract from the current conversation:
- Which MCP tools were called, in what order
- What parameters the user provided vs. what was inferred
- Where the user corrected course — those corrections are the skill's value
- What interpretation or judgment calls were made
- The output format the user found useful

### If creating from scratch:

Ask conversationally:
1. What hydrological analysis should this skill guide? (e.g., "drought index computation", "dam break flood routing")
2. What AI-Hydro MCP tools does it use? (e.g., `fetch_streamflow_data`, `extract_hydrological_signatures`)
3. When should the agent trigger this skill? Be specific — what phrases or contexts?
4. What domain expertise should the instructions encode? (interpretation thresholds, common pitfalls, regional norms)

## Step 2 — Write the SKILL.md

### Frontmatter (required)

```yaml
---
name: kebab-case-id
description: One sentence — what this skill does
when_to_use: Specific trigger conditions (user phrases, data states, analysis contexts)
domain: frequency-analysis | baseflow | modelling | interpretation | composition | general
tools_used:
  - list_mcp_tools_used
tags:
  - relevant
  - keywords
---
```

### Body Structure

Organize as a step-by-step workflow the agent can follow:

```markdown
# Skill Title

## Purpose
One paragraph: what this skill accomplishes and why it matters.

## When This Workflow Applies
Bullet list of trigger conditions.

## Step 1 — [Action]
What to do, which tool to call, what parameters to use.
Include code examples where the tool call pattern matters.

## Step 2 — [Interpret / Validate]
How to read the results. Include reference tables for thresholds.
Flag common anomalies and their likely causes.

## Step 3 — [Report / Visualize]
What output format to use. Template for narrative if applicable.
```

### Writing Guidelines

- **Explain the why.** Don't just say "check BFI > 0.8" — explain that high BFI indicates permeable geology and groundwater-fed baseflow, which affects flood response.
- **Include reference tables.** Hydrologists think in thresholds and ranges. Tables like "BFI 0.5–0.8 = mixed geology" are the skill's core value.
- **Encode domain pitfalls.** "If the record is < 15 years, flag uncertainty on all signatures" — this is expert knowledge that prevents misinterpretation.
- **Use imperative form.** "Fetch streamflow data" not "You should fetch streamflow data."
- **Keep it under 300 lines.** If longer, split into a main SKILL.md and reference sections.
- **Include code examples** for tool calls and data processing steps — the agent follows them.

### AI-Hydro–Specific Patterns

Skills in AI-Hydro have access to these MCP tools — use them in your workflow:

| Tool | Use for |
|---|---|
| `fetch_streamflow_data` | USGS daily streamflow by gauge ID |
| `extract_hydrological_signatures` | BFI, FDC slope, seasonality, runoff ratio |
| `delineate_watershed` | Watershed boundary from pour point |
| `extract_geomorphic_parameters` | Slope, area, elevation stats |
| `compute_twi` | Topographic wetness index |
| `create_cn_grid` | SCS curve number from land cover + soils |
| `extract_camels_attributes` | CAMELS benchmark data for comparison |
| `fetch_forcing_data` | Precipitation, temperature, PET |
| `train_hydro_model` | HBV-light or LSTM training |
| `get_model_results` | Retrieve trained model outputs |
| `start_session` / `get_session_summary` | Session state management |

### Description Optimization

The `description` field in frontmatter is what triggers the skill. Make it specific and slightly "pushy" — the agent tends to under-trigger rather than over-trigger.

**Bad:** `"Analyze streamflow data"`
**Good:** `"Compute drought indices (SPI, SPEI, PDSI) from streamflow and climate data. Use whenever the user mentions drought, dry spells, low-flow analysis, water scarcity assessment, or asks about drought severity classification."`

## Step 3 — Test the Skill

After writing the draft:

1. Think of 2–3 realistic prompts a user would say that should trigger this skill
2. Mentally walk through: would the agent follow the steps correctly?
3. Check: are the tool calls in the right order? Are parameters specified?
4. Check: are interpretation thresholds accurate? Cross-reference with hydrology literature.

## Step 4 — Save the Skill

### Via MCP tool (agent self-authoring):

```python
save_skill(
    name="drought-index-analysis",
    description="Compute drought indices (SPI, SPEI, PDSI) from streamflow and climate data...",
    content="# Drought Index Analysis\n\n## Purpose\n...",
    domain="interpretation",
    tags=["drought", "SPI", "SPEI", "low-flow"]
)
```

This writes to `~/.aihydro/skills/agent-created/<id>/SKILL.md` and appears immediately in the Skills panel.

### Via marketplace submission:

For community distribution, submit to `github.com/AI-Hydro/Skills`:
1. Create `skills/<skill-id>/SKILL.md` + `manifest.json`
2. Open a PR or issue with the skill attached

## Common Skill Domains

| Domain | Examples |
|---|---|
| `frequency-analysis` | Return periods, IDF curves, extreme value distributions |
| `baseflow` | Baseflow separation, recession analysis, groundwater recharge |
| `modelling` | Model setup, calibration, validation, ensemble runs |
| `interpretation` | Reading signatures, comparing catchments, anomaly detection |
| `composition` | Multi-step workflows spanning several tools |
| `general` | Data quality checks, unit conversions, report generation |

## Anti-Patterns to Avoid

- **Don't write a textbook.** The skill guides an agent, not a student. Steps should be actionable.
- **Don't hardcode gauge IDs.** Use variables and let the user provide specifics.
- **Don't skip error handling.** Include "if X fails, try Y" fallbacks for common issues (missing data, short records, API timeouts).
- **Don't forget CAMELS context.** Always situate results relative to regional norms when CAMELS data is available — isolated numbers are hard to interpret.
