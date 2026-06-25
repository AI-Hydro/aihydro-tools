"""
Wave A3 — generate re-export shims for migrated modules in aihydro-tools.

Each entry in SHIMS is:
  (relative_path_in_tools, watershed_module, [extra_private_names])

Run from MCP/aihydro-tools/:
    python scripts/make_a3_shims.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parent.parent

SHIM_HEADER = '''\
"""Compatibility shim — implementation moved to aihydro-watershed (Wave A3)."""
from __future__ import annotations
'''

# (tools_rel_path, aihydro_watershed_module, extra_private_names)
SHIMS: list[tuple[str, str, list[str]]] = [
    # --- analysis flat modules ---
    ("ai_hydro/analysis/watershed.py",
     "aihydro_watershed.characterize.watershed", []),
    ("ai_hydro/analysis/geomorphic.py",
     "aihydro_watershed.characterize.geomorphic",
     ["_slope_horn_kernel", "_SLOPE_CHUNK_TRIGGER"]),  # used by test_chunked_geomorphic_cn.py
    ("ai_hydro/analysis/twi.py",
     "aihydro_watershed.characterize.twi",
     ["compute_twi_result"]),          # __all__ omits it; used by MCP tools + tests
    ("ai_hydro/analysis/_dem.py",
     "aihydro_watershed.characterize._dem",
     ["_geom_in_conus"]),          # used by forcing.py, inundation.py
    ("ai_hydro/analysis/curve_number.py",
     "aihydro_watershed.terrain.curve_number",
     ["_build_joint_cn_lookup", "_create_cn_lookup_table", "_create_cn_grid_from_data",
      "_classify_soil_hydrologic_group", "_vectorised_cn_lookup", "_CN_CHUNK_TRIGGER"]),  # tests
    ("ai_hydro/analysis/event_runoff.py",
     "aihydro_watershed.terrain.event_runoff", []),
    ("ai_hydro/analysis/erosion.py",
     "aihydro_watershed.terrain.erosion",
     ["_DEFAULT_C_FACTORS"]),         # used by test_erosion.py
    ("ai_hydro/analysis/signatures.py",
     "aihydro_watershed.signatures.signatures",
     ["_fetch_precipitation_data_bygeom", "_lyne_hollick_baseflow"]),  # used by test_hydrology_tools.py
    ("ai_hydro/analysis/baseflow.py",
     "aihydro_watershed.signatures.baseflow", []),
    ("ai_hydro/analysis/flow_duration.py",
     "aihydro_watershed.signatures.flow_duration", []),
    ("ai_hydro/analysis/flood_frequency.py",
     "aihydro_watershed.signatures.flood_frequency",
     ["_EULER_GAMMA"]),               # used by test_flood_frequency.py
    ("ai_hydro/analysis/drought_indices.py",
     "aihydro_watershed.signatures.drought_indices", []),

    # --- delineation subpackage ---
    ("ai_hydro/analysis/delineation/router.py",
     "aihydro_watershed.delineation.router",
     ["_should_escalate"]),        # used by test_delineation.py
    ("ai_hydro/analysis/delineation/merit_flowdir_pipeline.py",
     "aihydro_watershed.delineation.merit_flowdir_pipeline", []),
    ("ai_hydro/analysis/delineation/nldi_point.py",
     "aihydro_watershed.delineation.nldi_point",
     ["_normalize_nldi_basins"]),  # used by tools/analysis/watershed.py
    ("ai_hydro/analysis/delineation/merit_snap.py",
     "aihydro_watershed.delineation.merit_snap", []),
    ("ai_hydro/analysis/delineation/pysheds_pipeline.py",
     "aihydro_watershed.delineation.pysheds_pipeline", []),
    ("ai_hydro/analysis/delineation/dem_fetch.py",
     "aihydro_watershed.delineation.dem_fetch", []),
    ("ai_hydro/analysis/delineation/dem_conditioning.py",
     "aihydro_watershed.delineation.dem_conditioning", []),
    ("ai_hydro/analysis/delineation/types.py",
     "aihydro_watershed.delineation.types", []),
    ("ai_hydro/analysis/delineation/utils.py",
     "aihydro_watershed.delineation.utils", []),

    # --- data / merit modules ---
    ("ai_hydro/data/merit_download.py",
     "aihydro_watershed.merit.merit_download", []),
    ("ai_hydro/data/merit_manager.py",
     "aihydro_watershed.merit.merit_manager", []),
    ("ai_hydro/data/merit_map_layers.py",
     "aihydro_watershed.merit.merit_map_layers", []),
    ("ai_hydro/data/region_presets.py",
     "aihydro_watershed.merit.region_presets", []),
    ("ai_hydro/data/wbd_layers.py",
     "aihydro_watershed.merit.wbd_layers",
     ["_huc_code_from_row"]),     # used by hydro_search.py
]

# The delineation __init__.py is separate — it has a lazy-import wrapper
DELINEATION_INIT = """\
\"\"\"Compatibility shim — delineation engine moved to aihydro-watershed (Wave A3).\"\"\"
from __future__ import annotations

from typing import Any


def delineate_from_point(*args: Any, **kwargs: Any):
    \"\"\"Lazy import so pysheds/numba load only when delineation runs.\"\"\"
    from aihydro_watershed.delineation.router import delineate_from_point as _impl
    return _impl(*args, **kwargs)


__all__ = ["delineate_from_point"]
"""


def make_shim(ws_module: str, extras: list[str]) -> str:
    lines = [
        SHIM_HEADER,
        f"from {ws_module} import *  # noqa: F401, F403",
    ]
    if extras:
        private_imports = ", ".join(extras)
        lines.append(
            f"from {ws_module} import {private_imports}  # noqa: F401  (private names)"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for rel_path, ws_module, extras in SHIMS:
        dest = TOOLS_ROOT / rel_path
        shim = make_shim(ws_module, extras)
        if args.dry_run:
            print(f"[shim] {rel_path} → {ws_module}")
        else:
            dest.write_text(shim, encoding="utf-8")
            print(f"shimmed  {rel_path}")

    # Delineation __init__.py
    init_path = TOOLS_ROOT / "ai_hydro/analysis/delineation/__init__.py"
    if args.dry_run:
        print("[shim] ai_hydro/analysis/delineation/__init__.py (lazy wrapper)")
    else:
        init_path.write_text(DELINEATION_INIT, encoding="utf-8")
        print("shimmed  ai_hydro/analysis/delineation/__init__.py")

    if not args.dry_run:
        print("\n=== Done — now add aihydro-watershed path dep to pyproject.toml ===")


if __name__ == "__main__":
    main()
