# aihydro-tools — Architecture

The MCP/agent surface for the AI-Hydro platform. It wraps the AI-Hydro
scientific packages for agent use through FastMCP, session/project/discovery
state, and the defensibility stack (claims → audit → uncertainty → capsule →
experiments → HydroResearch-Bench).

`aihydro-tools` is the meta/orchestration package: base install exposes the MCP
surface and core hydrology wrappers; heavier modelling capabilities remain behind
extras until their packages are published.

---

## Position in the ecosystem

```
   aihydro-core          ← zero-dep substrate
        ▲
   ┌────┴─────┬──────────────┐
   │          │              │
aihydro-  pygeoglim   aihydro-
data                   watershed
   │          │              │
   └────┬─────┴──────────────┘
        │
   aihydro-lsh       ← CAMELS recipe layer
        │
        ▼
  ╔═════════════════════════════════════════════════╗
  ║            aihydro-tools (ai_hydro)             ║  ← THIS PACKAGE
  ║  MCP surface · session · defensibility stack    ║
  ║  meta-package (Wave D)                          ║
  ╚═════════════════════════════════════════════════╝
        │
        ▼
  FastMCP server / console scripts
  Claude / agent clients
```

---

## Module map

```
ai_hydro/
│
├── __init__.py              Package root + MCP server bootstrap
│
├── core/
│   ├── types.py             Re-export shim → aihydro_core.contracts
│   │                        (HydroResult, HydroMeta, DataSource, HydroTool, ToolError)
│   └── digest.py            Content hash (delegates to aihydro_core.primitives.hashing)
│
├── data/                    DATA FETCH TOOLS
│   ├── fetch_runner.py      data_fetch() MCP tool (wraps aihydro_data.fetch)
│   ├── forcing.py           forcing download / time-series tools
│   ├── streamflow.py        streamflow fetch + NWIS gauge lookup
│   ├── soil.py              soil data tools
│   ├── landcover.py         land cover tools
│   ├── hydro_search.py      NLDI reach / NHD search tools
│   ├── async_dispatch.py    parallel data fetch dispatcher
│   └── merit_*/wbd_layers   MERIT tile management (re-exports → aihydro-watershed)
│
├── analysis/                ANALYSIS TOOLS (→ aihydro-watershed shims)
│   ├── signatures.py        → aihydro_watershed.signatures.signatures  (shim)
│   ├── baseflow.py          → aihydro_watershed.signatures.baseflow     (shim)
│   ├── flow_duration.py     → aihydro_watershed.signatures.flow_duration (shim)
│   ├── flood_frequency.py   → aihydro_watershed.signatures.flood_frequency (shim)
│   ├── drought_indices.py   → aihydro_watershed.signatures.drought_indices (shim)
│   ├── watershed.py         → aihydro_watershed.characterize.watershed    (shim)
│   ├── geomorphic.py        → aihydro_watershed.characterize.geomorphic   (shim)
│   ├── twi.py               → aihydro_watershed.characterize.twi          (shim)
│   ├── curve_number.py      → aihydro_watershed.terrain.curve_number      (shim)
│   ├── event_runoff.py      → aihydro_watershed.terrain.event_runoff      (shim)
│   ├── erosion.py           → aihydro_watershed.terrain.erosion            (shim)
│   ├── uncertainty.py       → aihydro_core.science.uncertainty             (shim)
│   └── inundation_*.py      Inundation analysis (NOT in aihydro-watershed — own module)
│
├── audit/                   DEFENSIBILITY — CLAIMS & AUDIT
│   ├── grammar.py           Claim parsing grammar (lark)
│   ├── models.py            Claim, AuditResult, Evidence typed models
│   └── resolver.py          Literature passage resolver ([lit:hash] references)
│
├── capsule/
│   └── manifest.py          Capsule export: manifest.json + replay.py + run_log
│
├── experiments/
│   └── models.py            ExperimentSpec, ExperimentResult, ExperimentTable
│
├── bench/                   HydroResearch-Bench / aihydro-bench
│   ├── tasks.yaml           Frozen task catalog (B-001 → B-079)
│   ├── schema.py            Catalog validation + certification JSON payload
│   ├── oracle.py            Assertion evaluator
│   └── gen_scorecard.py     HTML scorecard + `aihydro-bench` CLI
│
├── community/               Community-facing benchmark adapters
│
├── gee/                     Google Earth Engine auth + map layer tools
│   ├── auth.py
│   ├── cli.py
│   ├── contracts.py
│   ├── map_layers.py
│   ├── presets.py
│   └── timeseries.py
│
├── mcp/                     MCP SERVER
│   ├── __init__.py          FastMCP server init + tool loading
│   └── [tool modules]       144 tool registrations
│
└── citations.py             citation management tools
```

---

## MCP server architecture

```
FastMCP server ("aihydro-tools")
       │
       ├── Context injection middleware
       │     adds _chat_id + _workspace to every tool call via add_middleware()
       │     (NOT monkeypatching _call_tool_mcp — that binds at init; fixed v0.3.4)
       │
       ├── Tool groups loaded at startup:
       │     data/*           → fetch, streamflow, soil, landcover, MERIT
       │     analysis/*       → signatures, watershed, terrain, uncertainty
       │     audit/*          → claim chips, grammar, resolver
       │     capsule/*        → manifest, export, replay
       │     experiments/*    → define, run, table, aggregate
       │     bench/*          → HRB schema, oracle, scorecard/certification
       │     gee/*            → auth, timeseries, map layers
       │     delineation/*    → all via aihydro-watershed shims
       │
       └── Entry-point discovered tools (Wave C5 +):
             [aihydro.tools] lsh = "aihydro_lsh.mcp:register_tools"
             → lsh_attributes, lsh_families, lsh_attribute_info,
               lsh_batch, lsh_compare_to_ref, lsh_recipe_status
```

---

## Shim pattern (A3 re-wiring)

Analysis modules that moved into `aihydro-watershed` are kept as compatibility
shims.  Each shim is generated by `scripts/make_a3_shims.py` and follows the
pattern:

```python
# ai_hydro/analysis/signatures.py — compatibility shim
from aihydro_watershed.signatures.signatures import *
from aihydro_watershed.signatures.signatures import (
    _SOURCES_GEOGLOWS,       # private symbols not in __all__
    _SOURCES_PRECIP_GLOBAL,
)
```

The shims ensure existing MCP tool code continues to work without changes.
Direct test patches that reference `ai_hydro.data.*` keep their target;
tests that patch the actual implementation use `aihydro_watershed.*` paths.

---

## Defensibility stack

```
User claim: "NRMSE = 0.84 for the Potomac basin"
       │
       ▼
audit/grammar.py       parse claim → Claim(metric="NRMSE", value=0.84, basin="Potomac")
       │
       ▼
audit/resolver.py      resolve citations ([lit:hash] → passage text + source)
       │
       ▼
ClaimStore.put(claim)  JSONL claim registry (~/.aihydro/claims.jsonl)
       │
       ▼
Auditor.audit(claim)   4 deterministic checks:
                         range check (NRMSE ∈ [0, 1])
                         unit check  (dimensionless)
                         basin match (Potomac gauge exists in registry)
                         staleness   (within 30-day window)
       │
       ▼
UncertaintyProvider    bootstrap CI on the metric
  .bootstrap_ci(data)  → UncertaintyResult(estimate=0.84, lower=0.79, upper=0.88)
       │
       ▼
capsule/manifest.py    bundle: run_log + code + data hash + replay.py
       │
       ▼
DefensibilityReport    6-section Markdown: metadata, claims, audit, uncertainty,
                       experiment table, pre-registration
```

---

## Session / project / discovery layer

| Tool group | Function | Session-scoped? |
|---|---|---|
| `session_*` | create/load/list sessions | Yes |
| `project_*` | project metadata + workspace | Yes |
| `discovery_*` | search datasets, gauges, products | No |
| `data_fetch` | variable → HydroResult | No |
| `claim_*` | promote / check / list claims | Per claim registry |
| `experiment_*` | define / run / aggregate | Per experiment DB |
| `aihydro-bench` CLI | HRB scorecard + certification JSON | Shared bench |

---

## Packaging boundaries

Base install must remain resolvable from published packages. Therefore:

- `aihydro-core[contracts]`, `aihydro-data`, and `aihydro-watershed` are base
  dependencies with bounded compatible lines.
- `aihydro-modelling` remains behind the `modelling` extra until it is published
  on PyPI.
- `bench/` is packaged as an importable support package so the `aihydro-bench`
  console script can run from installed wheels.

Hydrologic data access belongs in `aihydro-data`. Benchmark tasks are offline by
default; any live benchmark should route through `aihydro-data` or the installed
package under test rather than ad hoc network code in the benchmark harness.
