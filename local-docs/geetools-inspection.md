# geetools Inspection

Date: 2026-05-20

## Decision

Do not add `geetools` as a dependency now. Keep `earthengine-api` as the required baseline for `aihydro-tools[gee]`.

## Rationale

`geetools` is useful as a design reference for Earth Engine convenience APIs, but AI-Hydro needs stable MCP contracts and hydrology-aware validation more than broad helper surface area. Adding another GEE abstraction before contracts stabilize would increase dependency weight and make provenance harder to reason about.

## What geetools Adds

Potentially useful areas:

- Image and ImageCollection convenience operations.
- Date/range helpers.
- Batch/export helpers.
- Collection filtering and preprocessing utilities.
- Satellite workflow conveniences that may reduce boilerplate.
- Method-chaining extensions through the `geetools` namespace.

## Fit For AI-Hydro

Useful as reference:

- ImageCollection operation patterns.
- Export ergonomics.
- Date and band handling patterns.
- Server-side helper style.

Not yet justified as dependency:

- AI-Hydro already owns core GEE preview and timeseries logic through `earthengine-api`.
- Hydrology semantics such as precipitation sums, land-cover fractions, and ROI provenance must remain AI-Hydro-specific.
- MCP outputs cannot expose geetools objects; all outputs must be AI-Hydro contracts.

## License And Stability

Current inspection:

- `geetools` is MIT licensed, which is generally compatible with Apache-2.0 distribution.
- The project extends the Google Earth Engine Python API with processing/pre-processing helpers, so it should remain hidden behind AI-Hydro internals if adopted.
- Its convenience surface overlaps with direct `earthengine-api` calls and with future candidates such as `eemont`, `geedim`, and task helpers.

Before reconsidering dependency adoption, inspect maintenance cadence, Python version compatibility, dependency graph, API stability, and overlap with concrete AI-Hydro workflows.

## Recommendation

Status: `defer`.

Use `geetools` as a design reference for internal helper functions. Reconsider as an optional dependency only if a concrete workflow, such as export management or ImageCollection preprocessing, becomes meaningfully simpler and remains hidden behind `ai_hydro.gee` internals.

## Related Ecosystem Notes

- `geemap`: strong UX and notebook reference, not a backend dependency for AI-Hydro.
- `wxee`: inspect later for xarray/NetCDF/climate forcing workflows.
- `geedim`: inspect later for image export/download and cloud masking.
- `eemont`: inspect later for indices and preprocessing.
- `hydra-floods`: inspect later for water/flood workflows.
- `taskee`: inspect later for task monitoring UX, likely not as a direct dependency.

## Sources Checked

- [Awesome-GEE](https://github.com/opengeos/Awesome-GEE) and [rendered Awesome-GEE site](https://awesome.geemap.org/) as ecosystem/resource references.
- [earthengine-api](https://github.com/google/earthengine-api) as the baseline Python/JavaScript client library.
- [geetools](https://github.com/gee-community/geetools) and [geetools documentation](https://geetools.readthedocs.io/en/stable/autoapi/geetools/index.html) for license and API-shape inspection.
- [geemap](https://github.com/gee-community/geemap) as a UX/notebook design reference, not a backend dependency.
- [geedim](https://github.com/leftfield-geospatial/geedim) as a future export/cloud-mask inspection target.
