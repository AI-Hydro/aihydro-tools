# AI-Hydro Tools — Project Status

> **This file is the single source of truth for the current state of `aihydro-tools`.**
> Keep it updated with every release. Agents and contributors should read this first.

---

## Current Version: `1.6.0` (in development — not yet published to PyPI)

> **Last public PyPI release:** `1.5.0`  
> **Next public release:** `1.6.0` will bundle all accumulated work since 1.5.0 (see CHANGELOG for full history).  
> **After that:** ongoing patch releases as `1.6.1`, `1.6.2`, etc.

---

## What This Package Does

`aihydro-tools` is an **agent-native MCP server** for computational hydrology research. It gives AI agents a structured set of tools, workflow skills, and library knowledge cards to act as a full research collaborator — not just a tool executor. The agent does the scientific judgment; the tools do the deterministic computation.

**The platform has seven capability layers:**

| Layer | What it is | Status |
|---|---|---|
| M1 — Core persona | Identity + reasoning policy in `app.py` | ✅ Categorical persona shipped in v1.6.0 |
| M2 — MCP tools | 36 typed, validated callables | ✅ 36 tools active |
| M3 — Library knowledge | API gotcha cards, consulted before writing Python | ✅ 15 cards, drift-detection live |
| M4 — Skills | Workflow playbooks for judgment-heavy tasks | ✅ 6 built-in v1 skills |
| M5 — CLI | Agent drives external CLIs (e.g. `swat`) via Bash | ✅ Persona-level guidance, `list_relevant_clis` tool |
| M6 — Context-file layer | Per-session `research.md` + `.aihydrorules` | ✅ Two-phase split live (get_session_raw_state + write_research_interpretation) |
| M7 — Sub-agent delegation | Parallelise batch work via sub-agents | 🔄 Persona-level guidance only; first sub-agent skill not yet built |
| M8 — MCP Resources | `aihydro://knowledge/library/{name}` URIs | ✅ Resource layer shipped in v1.6.0; façade tool kept for client compat |

---

## Tool Surface (36 tools)

| Module | Tools |
|---|---|
| `tools_analysis.py` | `delineate_watershed`, `fetch_streamflow_data`, `extract_hydrological_signatures`, `extract_geomorphic_parameters`, `compute_twi`, `create_cn_grid`, `fetch_forcing_data`, `fetch_camels_us`, `get_library_reference`, `show_on_map` |
| `tools_session.py` | `start_session`, `get_session_summary`, `clear_session`, `add_note`, `get_session_raw_state`, `write_research_interpretation`, `list_available_tools`, `export_session`, `list_relevant_clis` |
| `tools_execution.py` | `run_python` |
| `tools_modelling.py` | `train_hydro_model` (kickoff), `get_training_status`, `get_model_results` |
| `tools_project.py` | `start_project`, `get_project_summary`, `add_session_to_project`, `search_experiments`, `index_literature`, `search_literature`, `add_journal_entry`, `get_researcher_profile`, `update_researcher_profile`, `log_researcher_observation` |
| `tools_skills.py` | `list_skills`, `load_skill` |

---

## Skills (6 built-in)

Located in `ai_hydro/skills/`:

| Skill | What it encodes |
|---|---|
| `flood-frequency-analysis` | Gumbel vs GEV vs LP3 selection, USGS Bulletin 17C workflow |
| `baseflow-separation` | Lyne-Hollick vs UKIH method selection, BFI interpretation |
| `model-selection` | HBV-light vs LSTM vs regionalization decision guide |
| `calibration-diagnostics` | NSE/KGE decomposition, pathology recognition, next-step recommendations |
| `signature-interpretation` | FDC shape, BFI, runoff ratio → basin-storyline paragraph |
| `watershed-analysis-workflow` | End-to-end pipeline orchestrating all core analysis tools |

---

## Library Knowledge Cards (15 cards)

Under `ai_hydro/knowledge/library_refs/`. All cards include `version_compatible` semver range and runtime drift detection.

| Group | Cards |
|---|---|
| Original (v1.3.0) | `hydrofunctions`, `py3dep`, `pygeohydro`, `pygridmet`, `pynhd`, `pysheds`, `rasterio`, `xarray` |
| P1 (v1.7.0) | `torch`, `geopandas` |
| P2 (v2.0.0) | `pandas`, `numpy`, `shapely`, `matplotlib`, `folium` |

---

## What Changed in Each Version

| Version | Key deliveries |
|---|---|
| **1.6.0** *(next public release)* | All work since 1.5.0: two-phase session split, run_python, resource layer, 6 skills, async train_hydro_model, 15 library cards, capsule export — see CHANGELOG for full detail |
| **1.7.0** | `train_hydro_model` async rewrite; `get_training_status`; 6 built-in skills; P1 cards; CI knowledge-compat workflow |
| **1.6.0** | Two-phase session split; `run_python`; MCP resource layer; skills infrastructure; `separate_baseflow`; `list_relevant_clis`; persona rewrite |
| **1.5.2** | Deleted dead `workflows/` stubs; removed Tier-2/Tier-3 vocabulary |
| **1.5.1** | Map layer support (`show_on_map`, raster push for TWI + CN grid) |
| **1.5.0** | BibTeX citation registry; `export_session` with citations |
| ≤1.4.0 | Core tools, session, project, literature, researcher profile |

---

## What's In Scope Next (Post-2.0)

Tracked in `local-docs/architecture/OPEN_QUESTIONS.md`.

**Versioning:** work continues as `1.6.1`, `1.6.2`, etc. (patch releases off `1.6.x`).

### 🔴 Priority — Sub-Agent Delegation Skills
The first skill that actually **spawns sub-agents** (M7) is unbuilt. Needed for:
- Batch CAMELS-attribute extraction across many gauges (tens of minutes, embarrassingly parallel)
- Parallel calibration comparisons (NSGA-II vs SCE-UA)
- Literature scans over large document sets

**Blocker first:** HydroSession concurrency (§7.1) — sub-agents can't safely write to a shared `session.json`. Proposed fix: each sub-agent writes to its own shard; orchestrator merges on return.

### 🟡 Medium — Session Staleness / Invalidation (§7.2) — `1.6.2`
Sessions from months ago are auto-injected with stale "pending tasks" and "active hypotheses". Need either:
- Per-field TTL (computed slots never stale; pending tasks stale after N days)
- Researcher-triggered "archive" action to freeze a session
- Agent prompted to review + purge stale content on reopen

### 🟡 Medium — Workspace-Tier Skill Quality Bar (§7.4) — `1.6.3`
Researcher-authored `.aihydrorules/skills/*.md` are not reviewed. Options:
- Passive: agent skepticism in persona ("workspace skills are researcher-authored; verify if they conflict with built-in skills")
- Active: linting (check `citations:` field, `when_to_use:` not empty, tools referenced exist)

---

## What's Explicitly Deferred (Out of Scope for Now)

These items are documented here so they aren't lost, but are **not on the active roadmap**:

| Item | Why deferred |
|---|---|
| `swatplus-builder` MCP integration (Phase 3D) | Maintained separately; CLI integration is current primary path |
| `camels-attrs` batch workflow / M6 session slots | Maintained separately |
| Map / visualization robustness (`show_on_map`) | Low priority vs. core research workflow |
| AI-modelling integration (NeuralHydrology, HydroDL2) | Major scope; revisit after sub-agent skills land |
| M3 façade deprecation | Waiting on MCP client resource support to mature (§7.6) |
| Parameter registry protocol (§7.5) | Blocked on swatplus-builder Phase 3D timeline |

---

## Architecture References

All design documents live in `local-docs/architecture/`:

| File | What it contains |
|---|---|
| `ARCHITECTURE.md` | Full 7-mechanism design with decision rubric (R1–R8, TB1–TB8) |
| `REFACTOR_ROADMAP.md` | Phase 1–4 tasks and verification checklists |
| `AUDIT_FINDINGS.md` | Per-tool audit + concrete action items (the "what to build" specs) |
| `OPEN_QUESTIONS.md` | Unresolved design decisions with options and tradeoffs |
| `EXTENDED_MECHANISMS_PLAN.md` | M6, M7, M8 playbook — how the mechanisms are *used* |
| `PHASE_PROMPTS.md` | Canonical executor prompts for dispatching refactor phases |

---

## Companion Libraries (Maintained Separately)

| Library | Repo | Role |
|---|---|---|
| `swatplus-builder` | `~/PyQSwatPlus/swatplus-builder` | End-to-end SWAT+ model building and calibration |
| `camels-attrs` | Separate repo | CAMELS-style attribute extraction for USGS gauges |

Integration contracts documented in:
- `local-docs/architecture/swatplus-builder-aihydro-compatibility.md`
- `local-docs/architecture/camels-attrs-aihydro-compatibility.md`

---

## Running the Server

```bash
# Install
pip install aihydro-tools[all]

# Start MCP server
aihydro-mcp

# Diagnose
aihydro-mcp --diagnose

# Run tests (non-live)
pytest tests/ -m "not live" -q
```

---

*Last updated: 2026-05-01 — v2.0.0 merged to main.*
