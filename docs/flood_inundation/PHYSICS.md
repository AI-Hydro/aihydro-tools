# Flood Inundation — Physics Validation Tier (Phase 3)

Validate-tier jobs compare **HAND + SRC** extents to **2D physics** (SFINCS via HydroMT, or LISFLOOD-FP) using the same contingency metrics as GFM hindcast validation (CSI, POD, FAR).

## When to use

| Tool | Tier | Role |
|------|------|------|
| `run_inundation_physics_validation` | 1 | Kick off detached benchmark job |
| `wait_for_job` | 3 | Block until complete (preferred) |
| `get_inundation_physics_result` | 3 | Read benchmark report |

Typical agent flow:

```
delineate_watershed(gauge_id="01031500")
compute_flood_frequency()
run_inundation_physics_validation(return_period=100)
wait_for_job("<job_id>")
get_inundation_physics_result("<job_id>")
```

## Output contract

`partial_results` / `report` includes:

- `hand` — discharge, stage, area, scope (HAND + SRC)
- `physics` — method (`sfincs`, `lisflood_fp`, or `morphological_proxy`)
- `benchmark` — CSI/POD/FAR vs physics reference
- `backend` — install availability probe
- `caveat` / `proxy_note` when proxy used

## Graceful degradation

SFINCS/LISFLOOD-FP is **optional**. When packages or mesh automation are unavailable:

1. Job still completes with HAND baseline.
2. Benchmark uses an explicit **morphological dilation proxy** (never mislabeled as operational physics).
3. `proxy_note` explains the limitation.

Automated session→SFINCS mesh build is deferred; package detection alone does not run a full solver yet.

Completed physics jobs write `validation_masks.npz` for `export_inundation_surrogate_dataset`.

## HRB

| ID | Check |
|----|-------|
| B-070 | Identical masks → CSI = 1 |
| B-071 | Backend probe returns engine + availability |
| B-072 | Synthetic job report contract |

## Files

- `ai_hydro/analysis/inundation_physics.py` — core benchmark logic
- `ai_hydro/analysis/inundation_physics_runner.py` — async subprocess
- `ai_hydro/mcp/tools_inundation_physics.py` — MCP tools
