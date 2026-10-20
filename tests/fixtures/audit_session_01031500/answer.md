Investigation complete for CAMELS-US gauged basin **USGS 01031500 — Piscataquis River Near Dover-Foxcroft, Maine**.

## Steps performed and tools used

| Step | Tool | Result |
|---|---|---|
| Confirm gauge | `ask_followup_question` | Gauge ID provided: 01031500 |
| Start/verify session | `start_session` | Session bound; gauge resolved as usgs_gauge |
| Verify CAMELS membership + fetch attributes | `fetch_camels_us` | `in_camels: true`, 60 attributes returned — gauge is unambiguous and found |
| Delineate watershed | `delineate_watershed` | 774.15 km², gauge at 45.175°N / −69.3147°W, HUC-02 = 01 |
| Compute signatures | `extract_hydrological_signatures` | 17 signatures + bootstrap 90% CIs |
| Record claim | `add_claim` | Claim `c-01031500-signatures-20261020` recorded and verified present via `list_claims` |

## Key computed values (all from the two runs cited below)

- Watershed area: **774.15 km²** `[run:wshed.20261020.01031500.5fcfedd8#data.area_km2]`
- Runoff ratio: **0.61** `[run:sigs.20261020.01031500.dd4ed1e7#data.runoff_ratio]`
- Baseflow index: **0.45** `[run:sigs.20261020.01031500.dd4ed1e7#data.baseflow_index]`
- Streamflow elasticity: **1.76** `[run:sigs.20261020.01031500.dd4ed1e7#data.stream_elas]`
- Mean daily discharge: **2.02 m³/s** `[run:sigs.20261020.01031500.dd4ed1e7#data.q_mean]` over 7,305 daily observations
- Analysis period: 1989-10-01 to 2009-09-30 (CAMELS default)

The conclusion was recorded as claim `c-01031500-signatures-20261020` (status: `tested`, confidence: `high`), with evidence spans citing the watershed run and the signatures run for each number.

## Data sources used

- **USGS NLDI** — watershed delineation (run `wshed.20261020.01031500.5fcfedd8`)
- **USGS NWIS** — observed daily streamflow (retained inside run `sigs.20261020.01031500.dd4ed1e7`)
- **GridMET (`GRIDMET_PRECIP`)** — basin-averaged daily precipitation for runoff ratio and elasticity
- **CAMELS-US** (Addor et al., 2017 via `pygeohydro.get_camels()`) — static attributes used to confirm the gauge exists in CAMELS and is not ambiguous

## What could NOT be checked

- **Stationarity / trend analysis:** the signatures run's `stationarity` validator returned `insufficient_data` because the session's `streamflow` slot did not retain the full date/discharge arrays (they were stored compact). This limitation is recorded on the claim. It can be resolved by re-fetching streamflow with `fetch_streamflow_data` (or `data_fetch`) and re-running.
- **Registry promotion:** the claim is recorded in the session ledger but was **not promoted** to the global claim registry. The promotion gate reported blocking issues (basin_refs not bound, status `tested` not eligible, area_km2 evidence not a finite scalar in the compacted watershed run, and one unverified uncertainty flag) — these are advisory from `add_claim` and do not affect the session-level claim record, but I am stating them plainly rather than implying registry promotion occurred.
