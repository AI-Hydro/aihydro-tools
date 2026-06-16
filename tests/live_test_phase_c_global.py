"""
Phase C global-fallback live test — landcover + soil + CN grid OUTSIDE CONUS.

Proves the global-robustness promise for the CN data products:

  1. Global fallback (the new capability):
       - landcover → ESA WorldCover / Dynamic World (GEE) or ESA WorldCover STAC
       - soil      → ISRIC SoilGrids (GEE)
     for a small European basin AND a small S-Asian basin, where NLCD/POLARIS
     have NO coverage and the pre-migration code hard-failed.

  2. User-selectable product (auto by default):
       - auto call  → served by the region's auto primary
       - pinned call (product="ESA_WORLDCOVER_STAC") → result reflects the pin
       - both succeed and report which product served them.

  3. End-to-end CN grid computes outside CONUS (ESA WorldCover remapped to NLCD
     codes × SoilGrids texture → distributed Curve Number).

Run with:
    python tests/live_test_phase_c_global.py

Makes real network calls (Planetary Computer STAC and/or GEE). Requires GEE
auth for the SoilGrids path; the landcover path works auth-free via STAC.
"""
from __future__ import annotations

import sys
import traceback


# ── Tiny non-CONUS basins (≈10 km²) ─────────────────────────────────────────
# European Alps (Austria/Tyrol)
EU_BASIN = {
    "type": "Polygon",
    "coordinates": [[
        [11.30, 47.20], [11.35, 47.20], [11.35, 47.24],
        [11.30, 47.24], [11.30, 47.20],
    ]],
}
# S-Asian Himalaya (central Nepal)
SASIA_BASIN = {
    "type": "Polygon",
    "coordinates": [[
        [85.00, 27.60], [85.05, 27.60], [85.05, 27.64],
        [85.00, 27.64], [85.00, 27.60],
    ]],
}


def ok(m): print(f"  ✅  {m}")
def fail(m): print(f"  ❌  {m}")
def warn(m): print(f"  ⚠️   {m}")
def info(m): print(f"  ℹ️   {m}")
def banner(m): print(f"\n{'='*70}\n  {m}\n{'='*70}")


def _gdf(geojson):
    import geopandas as gpd
    from shapely.geometry import shape
    return gpd.GeoDataFrame(geometry=[shape(geojson)], crs="EPSG:4326")


def test_landcover_global(name, geojson) -> bool:
    from ai_hydro.data.landcover import fetch_lulc_data
    gdf = _gdf(geojson)
    passed = True

    # ── auto routing ────────────────────────────────────────────────────────
    info(f"[{name}] landcover auto-routing …")
    try:
        ds = fetch_lulc_data(gdf, year=2021)
        prod = ds.attrs.get("_adata_product")
        region = ds.attrs.get("_adata_region")
        cover = ds["cover_2021"]
        import numpy as np
        vals = cover.values
        uniq = np.unique(vals[np.isfinite(vals)]).astype(int).tolist()
        ok(f"[{name}] landcover served by {prod} (source={ds.attrs.get('_adata_source')}, region={region})")
        ok(f"[{name}]   NLCD-coded classes present: {uniq[:12]}")
        assert prod and not str(prod).startswith("NLCD"), \
            f"Expected a global product outside CONUS, got {prod}"
        # Remapped codes must be NLCD-style (11..95), never raw ESA (10,20,…100)
        assert all(11 <= c <= 95 for c in uniq), f"Non-NLCD codes leaked: {uniq}"
    except Exception as e:
        fail(f"[{name}] landcover auto failed: {e}")
        traceback.print_exc()
        passed = False

    # ── product pin (auth-free STAC) ─────────────────────────────────────────
    info(f"[{name}] landcover pinned to ESA_WORLDCOVER_STAC …")
    try:
        ds2 = fetch_lulc_data(gdf, year=2021, product="ESA_WORLDCOVER_STAC")
        prod2 = ds2.attrs.get("_adata_product")
        ok(f"[{name}] pinned product served: {prod2}")
        assert prod2 == "ESA_WORLDCOVER_STAC", f"Pin not honoured: {prod2}"
    except Exception as e:
        fail(f"[{name}] landcover pin failed: {e}")
        traceback.print_exc()
        passed = False

    return passed


def test_soil_global(name, geojson) -> bool:
    from ai_hydro.data.soil import fetch_soil_data_polaris
    gdf = _gdf(geojson)
    info(f"[{name}] soil auto-routing (expect SoilGrids) …")
    try:
        ds = fetch_soil_data_polaris(gdf)
        prod = ds.attrs.get("_adata_product")
        dvars = list(ds.data_vars)
        ok(f"[{name}] soil served by {prod} (source={ds.attrs.get('_adata_source')})")
        ok(f"[{name}]   texture vars: {dvars}")
        assert prod and prod != "POLARIS", f"Expected SoilGrids outside CONUS, got {prod}"
        assert any(str(v).startswith("sand") for v in dvars), \
            f"No sand fraction in soil vars: {dvars}"
        return True
    except Exception as e:
        fail(f"[{name}] soil auto failed: {e}")
        traceback.print_exc()
        return False


def test_cn_global(name, geojson) -> bool:
    """End-to-end CN grid outside CONUS."""
    from ai_hydro.analysis.curve_number import create_curve_number_grid_from_geometry
    from shapely.geometry import shape
    info(f"[{name}] full CN grid (ESA WorldCover × SoilGrids) …")
    try:
        res = create_curve_number_grid_from_geometry(
            geometry=shape(geojson),
            year=2021,
            resolution=30,
            save_outputs=False,
            create_visualizations=False,
        )
        stats = res.get("statistics", {})
        prov = res.get("data_provenance", {})
        cn_mean = stats.get("cn_mean")
        ok(f"[{name}] CN grid computed — mean CN={cn_mean:.1f}")
        ok(f"[{name}]   provenance: landcover={prov.get('landcover_product')}, "
           f"soil={prov.get('soil_product')}, region={prov.get('region')}")
        assert cn_mean is not None and 30 <= cn_mean <= 100, f"CN mean out of range: {cn_mean}"
        return True
    except Exception as e:
        fail(f"[{name}] CN grid failed: {e}")
        traceback.print_exc()
        return False


def main() -> None:
    results: dict[str, bool] = {}

    for name, geojson in [("EU-Alps", EU_BASIN), ("S-Asia-Himalaya", SASIA_BASIN)]:
        banner(f"GLOBAL LANDCOVER — {name}")
        results[f"{name}:landcover"] = test_landcover_global(name, geojson)

        banner(f"GLOBAL SOIL — {name}")
        results[f"{name}:soil"] = test_soil_global(name, geojson)

        banner(f"GLOBAL CN GRID — {name}")
        results[f"{name}:cn"] = test_cn_global(name, geojson)

    banner("SUMMARY")
    for k, v in results.items():
        (ok if v else fail)(f"{k}: {'PASS' if v else 'FAIL'}")
    n_pass = sum(results.values())
    print(f"\n  {n_pass}/{len(results)} checks passed")
    sys.exit(0 if n_pass == len(results) else 1)


if __name__ == "__main__":
    main()
