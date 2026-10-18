"""U5a: no tool description exceeds 1500 chars, and the trimmed ones keep their usage info."""
from __future__ import annotations

import asyncio

from ai_hydro.mcp import mcp


def _registered() -> dict:
    return asyncio.run(mcp.get_tools())


def test_no_tool_description_exceeds_1500_chars():
    too_long = {n: len(t.description or "") for n, t in _registered().items()
                if len(t.description or "") > 1500}
    assert not too_long, too_long


def test_trimmed_descriptions_keep_their_required_usage_info():
    tools = _registered()
    must = {
        "add_claim": ["basin_refs", "evidence_spans", "prereg_id", "uncertainty_gate",
                      "LOCAL_PATH_IN_TEXT", "promotion_check", "BASIN_REF_REQUIRED"],
        "data_fetch": ["variable", "geometry", "start", "end", "mode", "product", "aggregation",
                       "cache", "session_id", "gauge_id", "GAUGE_REQUIRED", "GAUGE_AMBIGUOUS",
                       "_run_id", "USE_ASYNC_TOOL"],
        "compute_spectral_index": ["index_name", "sensor", "frequency", "mask_clouds",
                                   "create_map", "native_resolution", "trend_slope_per_year"],
        "create_cn_grid": ["year", "resolution", "feature", "geometry_geojson", "product",
                           "soil_product", "dual_hsg", "hsg_method"],
        "map_flood_inundation": ["discharge_m3s", "return_period", "use_design_peak",
                                 "use_session_peak", "hindcast_date", "validate_gfm",
                                 "use_worldpop", "manning_n", "reference_extent_geojson"],
        "run_autoresearch": ["hypothesis", "backend", "strategy", "budget_hours",
                             "max_experiments", "proxy_epochs", "space_overrides_json",
                             "prereg_id", "comparison_metric", "full_retrain", "get_leaderboard"],
        "show_on_map": ["geojson", "name", "layer_id", "layer_type", "style_preset",
                        "fill_color", "stroke_color", "fill_opacity", "auto_zoom"],
        "fetch_forcing_data": ["start_date", "end_date", "variables", "product",
                               "geometry_geojson", "feature", "_data_file"],
        "export_session": ["capsule", "defensibility_report", "bibtex", "capsule_path",
                           "replay.py", "--allowed-signers", "bundle_id"],
    }
    for name, words in must.items():
        desc = tools[name].description
        assert all(w in desc for w in words), (name, [w for w in words if w not in desc])
