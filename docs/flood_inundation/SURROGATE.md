# Flood Inundation — Surrogate Training Substrate (Phase 3 optional)

Export compact **HAND → physics/proxy extent** pairs and train a lightweight **morphology baseline** surrogate. Full SWE-GNN-style graph training remains deferred.

## When to use

After a completed physics validation job:

```
run_inundation_physics_validation(return_period=100)
wait_for_job("<job_id>")
export_inundation_surrogate_dataset(physics_job_id="<job_id>")
train_inundation_surrogate(dataset_path=".../surrogate_<job>.json")
wait_for_job("<train_job_id>")
get_inundation_surrogate_result("<train_job_id>")
```

Offline bench / CI:

```
export_inundation_surrogate_dataset(synthetic_mode=True)
train_inundation_surrogate(synthetic_mode=True)
```

Or combine export+train:

```
train_inundation_surrogate(physics_job_id="<physics_job_id>")
```

## Dataset contract

Written JSON (`surrogate_*.json`):

| Field | Description |
|-------|-------------|
| `feature_schema` | `discharge_m3s`, `stage_likely_m`, `hand_area_km2`, `cell_size_m`, `grid_shape` |
| `target_schema` | `physics_inundated_mask_rle` |
| `samples[].hand_mask_rle` | Input HAND extent (RLE) |
| `samples[].target.inundated_mask_rle` | Physics or proxy reference (RLE) |
| `caveat` | Proxy targets are not operational maps |

Physics jobs also write `validation_masks.npz` beside `status.json` for export.

## Model contract

`surrogate_model.json` from training:

| Field | Description |
|-------|-------------|
| `framework` | `morphology_baseline` |
| `dilation_iterations` | Tuned 8-neighbor dilation count |
| `train_csi` | Mean CSI vs physics target after tuning |
| `hand_csi_baseline` | Raw HAND CSI before tuning |

Apply at inference with `apply_morphology_surrogate(hand_mask, iterations=...)`.

## HRB

| ID | Check |
|----|-------|
| B-078 | Synthetic export + RLE round-trip |
| B-079 | Morphology train improves CSI on synthetic fixture |

## Files

- `ai_hydro/analysis/inundation_surrogate.py` — RLE codec, export, morphology trainer
- `ai_hydro/analysis/inundation_surrogate_runner.py` — async train subprocess
- `ai_hydro/analysis/inundation_physics_runner.py` — saves `validation_masks.npz`
- `ai_hydro/mcp/tools_inundation_physics.py` — export + train MCP tools

## Deferred

- Multi-basin fleet export via experiment substrate
- SWE-GNN-style graph model (torch / pyg)
- SFINCS depth fields (currently extent masks only)
