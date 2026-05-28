# GEE Capability Registry

Date: 2026-05-20

## Purpose

The GEE registry is hydrology-aware. It is not just a dataset catalog. Each preset defines valid scientific operations, units, reducers, temporal aggregation rules, citations, and limitations.

## Initial Presets

### `precip.chirps.daily`

- Dataset: `UCSB-CHC/CHIRPS/V3/DAILY_SAT`
- Band: `precipitation`
- Semantics: precipitation flux/accumulation.
- Spatial reducer: basin mean or sum.
- Temporal aggregation: daily, monthly sum, annual sum.
- Notes: use basin spatial mean for areal precipitation and temporal sums for monthly/annual depth.

### `dem.srtm`

- Dataset: `USGS/SRTMGL1_003`
- Band: `elevation`
- Semantics: static terrain.
- Spatial reducers: mean, min, max, median.
- Temporal aggregation: static only.
- Notes: no date range required for future DEM workflows.

### `ndvi.modis`

- Dataset: `MODIS/061/MOD13Q1`
- Band: `NDVI`
- Semantics: vegetation index.
- Spatial reducers: mean or median.
- Temporal aggregation: daily-compatible observations, monthly median, annual median.
- Notes: cloud/quality screening should be considered before trend interpretation.

### `landcover.nlcd`

- Dataset: `USGS/NLCD_RELEASES/2021_REL/NLCD`
- Band: `landcover`
- Semantics: categorical land cover.
- Spatial reducers: fractions or mode.
- Temporal aggregation: static.
- Notes: never use numeric mean on class codes.

### `landcover.esa_worldcover`

- Dataset: `ESA/WorldCover/v200`
- Band: `Map`
- Semantics: categorical global land cover.
- Spatial reducers: fractions or mode.
- Temporal aggregation: static.
- Notes: useful outside CONUS.

### `climate.era5_land`

- Dataset: `ECMWF/ERA5_LAND/DAILY_AGGR`
- Bands: `temperature_2m`, `total_precipitation_sum`
- Semantics: climate reanalysis.
- Spatial reducers: mean, min, max.
- Temporal aggregation: daily, monthly mean/sum, annual mean/sum.
- Notes: temperature and precipitation require different temporal semantics and unit handling.

## Capability Groups

- Basin climate forcing: ERA5-Land, future GRIDMET/Daymet GEE paths.
- Rainfall time series: CHIRPS daily basin workflows.
- DEM/topography: SRTM and future terrain derivatives.
- Land cover fractions: NLCD and ESA WorldCover categorical summaries.
- NDVI/vegetation dynamics: MODIS NDVI and future Landsat/Sentinel composites.
- Surface water/flood extent: future JRC Global Surface Water and Sentinel-1 workflows.
- Export/task monitoring: future raster exports and task lifecycle records.
