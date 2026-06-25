# Flood Inundation — Agent Workflow (v2)

End-to-end recipes for the RALP lead loop. Canonical workflow manifest: `Workflows/workflow.flood_inundation.workflow.yaml`.

## Core path (Phases 0–1)

```
delineate_watershed(gauge_id="01031500")
compute_flood_frequency()
map_flood_inundation(return_period=100)
```

Map: low / likely / high extent bands, depth raster, stage scrubber metadata, exposure placeholder.

## Hindcast validation (Phase 2)

```
map_flood_inundation(
  return_period=100,
  hindcast_date="2023-07-15",
  validate_gfm=True
)
```

Reports CSI/POD/FAR vs GFM when available; pushes orange observed layer with swipe hint.

## Hydrograph animation + 3D (Phase 2–4)

```
compute_design_hydrograph(return_period=100)
map_flood_inundation_hydrograph(max_frames=12, push_extent_polygons=True)
```

Map UI:
- 🕐 time slider filters dated layers
- 🌊 → Enable 3D view → Terrarium terrain → Play hydrograph
- Camera follows `flowdir_main_stem` when HAND stack has fdir/acc (else AOI axis)

## Physics validate-tier (Phase 3)

```
run_inundation_physics_validation(return_period=100)
wait_for_job("<job_id>")
get_inundation_physics_result("<job_id>")
```

Compares HAND extent to morphological proxy (or SFINCS when installed). Results cached on session as `inundation_physics`.

## Surrogate export (Phase 3 optional)

```
export_inundation_surrogate_dataset(physics_job_id="<job_id>")
# or synthetic_mode=True for offline bench
```

See `docs/flood_inundation/SURROGATE.md`. Full SWE-GNN training deferred.

```
train_inundation_surrogate(dataset_path=".../surrogate_<job>.json")
# or
train_inundation_surrogate(synthetic_mode=True)
wait_for_job("<job_id>")
get_inundation_surrogate_result("<job_id>")
```

## Claims & skeptic

After `map_flood_inundation`:

```
draft_claim_from_run(...)
run_skeptic(...)
```

Assumptions to declare: fluvial HAND only, synthetic SRC, DEM resolution, Manning band.

## HRB coverage

| Phase | Bench IDs |
|-------|-----------|
| 1 | B-061–B-067 |
| 2 | B-068–B-069 |
| 3 | B-070–B-072, B-078–B-079 |
| 4 | B-073–B-077 |

Run: `PYTHONPATH=. pytest tests/test_bench.py -m bench -k B-06`
