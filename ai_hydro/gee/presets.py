from __future__ import annotations

from ai_hydro.gee.contracts import DatasetPreset


PRESETS: dict[str, DatasetPreset] = {
    "precip.chirps.daily": DatasetPreset(
        preset_id="precip.chirps.daily",
        dataset_id="UCSB-CHC/CHIRPS/V3/DAILY_SAT",
        bands=["precipitation"],
        variable_type="flux_accumulated",
        allowed_spatial_reducers=["mean", "sum"],
        allowed_temporal_aggregations=["daily", "monthly_sum", "annual_sum", "monthly", "yearly"],
        default_visualization={
            "min": 0,
            "max": 300,
            "palette": ["081d58", "225ea8", "41b6c4", "a1dab4", "ffffcc"],
        },
        scale_m=5000,
        units="mm/day",
        output_units={"daily": "mm/day", "monthly_sum": "mm/month", "annual_sum": "mm/year"},
        citation="UCSB Climate Hazards Center. CHIRPS daily precipitation dataset.",
        hydrologic_use_notes=(
            "Use basin spatial mean for areal precipitation; use temporal sums for monthly "
            "or annual precipitation depths."
        ),
        default_workflows=["basin_precip_timeseries", "rainfall_dashboard"],
        known_limitations=["Satellite-gauge product; uncertainty varies by region and gauge density."],
    ),
    "dem.srtm": DatasetPreset(
        preset_id="dem.srtm",
        dataset_id="USGS/SRTMGL1_003",
        bands=["elevation"],
        variable_type="static_elevation",
        allowed_spatial_reducers=["mean", "min", "max", "median"],
        allowed_temporal_aggregations=["static"],
        default_visualization={"min": 0, "max": 3000, "palette": ["1a9850", "fee08b", "d73027"]},
        scale_m=30,
        units="m",
        output_units={"static": "m"},
        citation="NASA/USGS Shuttle Radar Topography Mission digital elevation data.",
        hydrologic_use_notes="Use for basin elevation, relief, slope, aspect, flow routing, and terrain derivatives.",
        default_workflows=["terrain_summary", "dem_preview"],
        known_limitations=["Void filling and vertical error can affect steep or vegetated terrain."],
        temporal=False,
    ),
    "ndvi.modis": DatasetPreset(
        preset_id="ndvi.modis",
        dataset_id="MODIS/061/MOD13Q1",
        bands=["NDVI"],
        variable_type="vegetation_index",
        allowed_spatial_reducers=["mean", "median"],
        allowed_temporal_aggregations=["daily", "monthly_median", "annual_median", "monthly", "yearly"],
        default_visualization={"min": 0, "max": 9000, "palette": ["d73027", "fee08b", "1a9850"]},
        scale_m=250,
        units="scaled_ndvi",
        output_units={"monthly_median": "scaled_ndvi", "annual_median": "scaled_ndvi"},
        citation="NASA LP DAAC MODIS Vegetation Indices MOD13Q1 product.",
        hydrologic_use_notes="Use compositing/cloud quality screening before vegetation trend interpretation.",
        default_workflows=["vegetation_dynamics", "ndvi_trend"],
        known_limitations=["Scaled integer NDVI; quality flags should be considered for trend studies."],
    ),
    "landcover.nlcd": DatasetPreset(
        preset_id="landcover.nlcd",
        dataset_id="USGS/NLCD_RELEASES/2021_REL/NLCD",
        bands=["landcover"],
        variable_type="categorical_land_cover",
        allowed_spatial_reducers=["fractions", "mode"],
        allowed_temporal_aggregations=["static"],
        default_visualization={},
        scale_m=30,
        units="class",
        output_units={"fractions": "percent"},
        citation="USGS National Land Cover Database.",
        hydrologic_use_notes="Compute categorical class fractions; never interpret class codes with numeric means.",
        default_workflows=["landcover_fractions", "basin_attributes"],
        known_limitations=["CONUS-focused product; class schema and release years vary."],
        temporal=False,
        categorical=True,
    ),
    "landcover.esa_worldcover": DatasetPreset(
        preset_id="landcover.esa_worldcover",
        dataset_id="ESA/WorldCover/v200",
        bands=["Map"],
        variable_type="categorical_land_cover",
        allowed_spatial_reducers=["fractions", "mode"],
        allowed_temporal_aggregations=["static"],
        default_visualization={},
        scale_m=10,
        units="class",
        output_units={"fractions": "percent"},
        citation="ESA WorldCover global land cover product.",
        hydrologic_use_notes="Use for global land-cover fractions where NLCD is unavailable.",
        default_workflows=["landcover_fractions", "global_basin_attributes"],
        known_limitations=["Global classification uncertainty varies by biome and region."],
        temporal=False,
        categorical=True,
    ),
    "climate.era5_land": DatasetPreset(
        preset_id="climate.era5_land",
        dataset_id="ECMWF/ERA5_LAND/DAILY_AGGR",
        bands=["temperature_2m", "total_precipitation_sum"],
        variable_type="climate_reanalysis",
        allowed_spatial_reducers=["mean", "min", "max"],
        allowed_temporal_aggregations=["daily", "monthly_mean", "monthly_sum", "annual_mean", "annual_sum", "monthly", "yearly"],
        default_visualization={},
        scale_m=11132,
        units="varies_by_band",
        output_units={"temperature_2m": "K", "total_precipitation_sum": "m"},
        citation="ECMWF ERA5-Land reanalysis.",
        hydrologic_use_notes=(
            "Use spatial mean over basin; temperature usually uses temporal mean/min/max, "
            "while precipitation uses temporal sums."
        ),
        default_workflows=["basin_climate_forcing", "forcing_comparison"],
        known_limitations=["Reanalysis product; model biases and unit conversions must be documented."],
    ),
}


def list_presets() -> list[DatasetPreset]:
    return list(PRESETS.values())


def get_preset(preset_id: str) -> DatasetPreset:
    try:
        return PRESETS[preset_id]
    except KeyError as exc:
        raise ValueError(f"Unknown GEE dataset preset: {preset_id}") from exc


def find_preset(dataset_id: str, band: str | None = None) -> DatasetPreset | None:
    for preset in PRESETS.values():
        if preset.dataset_id != dataset_id:
            continue
        if band is None or band in preset.bands:
            return preset
    return None
