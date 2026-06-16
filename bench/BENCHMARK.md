# HydroResearch-Bench (HRB)

A task-level evaluation suite for the AI-Hydro platform — 60 deterministic
benchmark tasks that verify scientific correctness, defensibility infrastructure,
and end-to-end research workflows.

## Overview

| Property | Value |
|---|---|
| Tasks | 60 (B-001 → B-060) |
| Network-required ("live") | 1 (B-020, nightly only) |
| Fixture tasks (no network) | 59 |
| Categories | 16 |
| Tier breakdown | Tier 1: 48, Tier 3: 12 |

## Categories

| Category | Tasks | Description |
|---|---|---|
| `validator` | B-001–B-008 | Water-balance consistency checks |
| `signatures` | B-009–B-013 | Hydrological signature computation |
| `ledger` | B-014–B-016, B-026 | Claim lifecycle (add, update, list) |
| `enforcement` | B-021–B-023, B-025 | Enforcement middleware (run_id injection, quality flags) |
| `auditor` | B-027–B-031 | Answer-auditor grammar + write gate |
| `uncertainty` | B-032–B-034 | Uncertainty-first promotion gating |
| `capsule` | B-035–B-036 | Capsule export + Defensibility Report |
| `prereg` | B-037–B-038 | Pre-registration lock + idempotency |
| `validators` | B-039–B-042 | Advanced validators (record-length, qual-codes, regulated-basin, stationarity) |
| `experiments` | B-043–B-044 | Experiment definition and registry |
| `registry` | B-045 | Global claim registry (promote + persistence) |
| `skeptic` | B-046–B-047 | Skeptic-agent scope overreach detection |
| `literature` | B-048–B-049 | Passage-index graceful degradation |
| `hrb` | B-050–B-060 | End-to-end HydroResearch-Bench tasks |
| `knowledge` | B-017–B-019, B-024 | Knowledge card retrieval |
| `watershed` | B-020 | Live USGS delineation (nightly) |

## Running the bench

```bash
# Install (from repo root)
pip install -e ".[all,dev]"

# Run all fixture tasks (no network)
pytest tests/test_bench.py -m bench -v

# Run live tasks (requires USGS / GridMET APIs)
pytest tests/test_bench.py -m bench_live -v

# Generate HTML scorecard (task catalog, no run)
python bench/gen_scorecard.py --out hrb_scorecard.html

# Generate HTML scorecard after running tests
python bench/gen_scorecard.py --run --out hrb_scorecard.html
```

## Scorecard

The `gen_scorecard.py` script produces a self-contained HTML scorecard showing
pass/fail status for every task. In CI (`bench.yml`), the scorecard is generated
after every fixture run and uploaded as a GitHub Actions artifact (`hrb-scorecard`).

```
hrb_scorecard.html   ← single self-contained HTML, no external deps
```

## Task format

Each task in `tasks.yaml` has:

```yaml
- id: B-027
  name: "Auditor: fully cited prose passes with coverage=1.0"
  tier: 1                      # 1=scientific, 2=workflow, 3=infra
  mark: bench                  # bench (fixture) | bench_live (network)
  category: auditor
  rationale: >
    Human-adjudicated expected behaviour, sourced from …
  setup:                       # optional: session slots to seed
    session_id: bench-aud-027
    slots:
      run_log:
        data:
          run_abc: {tool_name: compute_signatures, ...}
  call:
    tool: audit_interpretation  # MCP tool name
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

## Citing HRB

If you use HydroResearch-Bench in your research, please cite the platform paper
and `aihydro-tools` via `CITATION.cff`.
