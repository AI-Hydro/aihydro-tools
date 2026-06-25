# HydroResearch-Bench (HRB) / aihydro-bench

A task-level evaluation and certification suite for the AI-Hydro platform. HRB is
currently embedded in `aihydro-tools`; it is the scientific correctness,
regression, and governance gate for agent-facing hydrology tools.

## Scope

HRB is **not** a data-fetching layer and **not** a modelling library. Fixture tasks
must be deterministic and offline. Live tasks are explicitly marked and should
exercise data access through `aihydro-data` or the installed package under test,
not through benchmark-specific ad hoc fetchers.

## Overview

| Property | Value |
|---|---|
| Suite id | `hydroresearch-bench` |
| Schema version | 1 |
| Tasks | 79 (B-001 → B-079) |
| Fixture tasks, no network | 78 |
| Live tasks, nightly/manual | 1 |
| Categories | 16 |
| Tier breakdown | Tier 1: 62, Tier 2: 11, Tier 3: 6 |
| Default target package | `aihydro-tools` |

## Categories

| Category | Tasks | Description |
|---|---:|---|
| `validator` | 8 | Water-balance consistency checks |
| `signatures` | 5 | Hydrological signature computation |
| `ledger` | 4 | Claim lifecycle and listing |
| `enforcement` | 4 | Enforcement middleware and quality flags |
| `auditor` | 5 | Answer-auditor grammar + write gate |
| `uncertainty` | 3 | Uncertainty-first promotion gating |
| `capsule` | 2 | Capsule export + Defensibility Report |
| `prereg` | 2 | Pre-registration lock + idempotency |
| `validators` | 4 | Advanced validators |
| `experiments` | 2 | Experiment definition and registry |
| `registry` | 1 | Global claim registry |
| `skeptic` | 2 | Skeptic-agent checks |
| `literature` | 2 | Passage-index and literature grounding |
| `hrb` | 30 | End-to-end platform and flood/inundation benchmark contracts |
| `knowledge` | 4 | Knowledge card retrieval |
| `watershed` | 1 | Live watershed/API task |

## Running the bench

```bash
# Install (from repo root)
pip install -e ".[all,dev]"

# Run all fixture tasks (no network)
pytest tests/test_bench.py -m bench -v

# Run live tasks (requires live APIs)
pytest tests/test_bench.py -m bench_live -v

# Validate catalog/schema only
pytest tests/test_bench_schema.py -q

# Generate HTML scorecard (task catalog, no run)
python bench/gen_scorecard.py --out hrb_scorecard.html

# Generate scorecard + machine-readable certification after fixture run
python bench/gen_scorecard.py --run --out hrb_scorecard.html --json-out hrb_certification.json
```

## Scorecard + certification JSON

The `gen_scorecard.py` script produces a self-contained HTML scorecard. With
`--json-out`, it also writes a machine-readable certification payload containing:

- suite id and schema version
- task counts by mark, tier, and category
- schema-valid flag plus schema issues
- git SHA when available
- fixture pass/fail/skip counts when `--run` is used

CI (`bench.yml`) generates the scorecard after fixture runs and uploads it as the
`hrb-scorecard` artifact.

## Task format

Each task in `tasks.yaml` has or inherits:

```yaml
schema_version: 1
suite_id: hydroresearch-bench
default_target_package: aihydro-tools

tasks:
  - id: B-027
    name: "Auditor: fully cited prose passes with coverage=1.0"
    tier: 1                      # 1=scientific, 2=workflow, 3=infra
    mark: bench                  # bench (fixture) | bench_live (network)
    category: auditor
    call_style: session_op       # session_op | mcp_tool | compute_fn | enforcement_fn
    target_package: aihydro-tools # optional; inherited by default
    rationale: >
      Human-adjudicated expected behaviour, sourced from …
    setup:                       # optional: session slots to seed
      session_id: bench-aud-027
    call:
      tool: audit_interpretation
      kwargs:
        session_id: bench-aud-027
        text: "… [run:run_abc#value] …"
    assertions:
      - {path: passed, op: eq, expected: true}
      - {path: numeric_coverage, op: approx, expected: 1.0, tol: 0.01}
```

## Oracle operators

| Operator | Meaning |
|---|---|
| `eq` | Exact equality |
| `ne` | Not equal |
| `between` | `lo ≤ value ≤ hi` |
| `gt`, `ge`, `lt`, `le` | Numeric comparisons |
| `approx` | `|value − expected| ≤ tol` (default tol=1e-6) |
| `approx_pct` | `|value − expected| / |expected| ≤ pct` (default 5 %) |
| `present` | Value is not None |
| `absent` | Key absent or value is None |
| `contains` | `expected in value` (string or list) |
| `startswith` | String prefix match |
| `len_gte` | `len(value) ≥ expected` |
| `len_eq` | `len(value) == expected` |

## Governance rule

New tools, validators, session fields, or complexity layers need either:

1. a failing/insufficient HRB task, or
2. a reproducible session trace showing the simpler layer failed.

Hypothetical failures are not enough. The benchmark failure ID is the common
currency for product growth.

## Citing HRB

If you use HydroResearch-Bench in research, cite the AI-Hydro platform and the
relevant package release. When HRB becomes cross-package certification across
multiple repos, it can be extracted into a standalone `aihydro-bench` package.
