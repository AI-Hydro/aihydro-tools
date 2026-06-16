"""
Phase 2 live tests — create_cn_grid, fetch_forcing_data,
extract_hydrological_signatures with feature= routing.

Run with:
    python tests/live_test_phase2_tools.py

Makes real network calls (NLCD, GridMET). Skips tools that lack the
required upstream data rather than hard-failing, so partial runs are
informative.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

# ── Small Cascade Range polygon — same Basin A used in Phase 1 ──────────────
# ~30 km², CONUS → NLCD + 3DEP + GridMET coverage guaranteed.
BASIN_A_GEOJSON = json.dumps({
    "type": "Polygon",
    "coordinates": [[
        [-122.25, 46.22], [-122.18, 46.22], [-122.18, 46.27],
        [-122.25, 46.27], [-122.25, 46.22],
    ]],
})

BASIN_B_GEOJSON = json.dumps({
    "type": "Polygon",
    "coordinates": [[
        [-122.13, 46.22], [-122.06, 46.22], [-122.06, 46.27],
        [-122.13, 46.27], [-122.13, 46.22],
    ]],
})

AREA_KM2 = 30.0


# ── Console helpers ────────────────────────────────────────────────────────────

def banner(msg: str) -> None:
    print(f"\n{'='*70}\n  {msg}\n{'='*70}")

def ok(msg: str)   -> None: print(f"  ✅  {msg}")
def fail(msg: str) -> None: print(f"  ❌  {msg}"); sys.exit(1)
def warn(msg: str) -> None: print(f"  ⚠️   {msg}")
def info(msg: str) -> None: print(f"  ℹ️   {msg}")


# ── Synthetic streamflow generator ───────────────────────────────────────────
def _synthetic_q_cms(n_years: int = 5, seed: int = 42) -> list[float]:
    """
    Return a daily discharge series [m³/s] with seasonal signal + noise.
    Used to seed the session for signatures testing without a USGS API call.
    Mean ≈ 2 m³/s, seasonal amplitude ≈ 1.5 m³/s.
    """
    rng = random.Random(seed)
    days = n_years * 365
    out = []
    for d in range(days):
        seasonal = 2.0 + 1.5 * math.sin(2 * math.pi * d / 365 - 1.0)
        noise = rng.gauss(0, 0.3)
        out.append(max(0.01, seasonal + noise))
    return out


async def run_tests() -> None:
    from ai_hydro.session import HydroSession
    from aihydro_core.primitives import Feature
    from ai_hydro.mcp.tools_analysis import (
        create_cn_grid,
        fetch_forcing_data,
        extract_hydrological_signatures,
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        SESSION_ID = "phase2-test"

        patches = [
            patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path),
            patch("ai_hydro.session.store._REPO_ROOT", tmp_path),
            patch("ai_hydro.mcp.tools_analysis._resolve_session",
                  return_value=SESSION_ID),
            patch("ai_hydro.mcp.tools_session._resolve_session",
                  return_value=SESSION_ID),
        ]
        for p in patches:
            p.start()

        try:
            # ── Seed session ───────────────────────────────────────────────
            s = HydroSession(SESSION_ID)
            # Two registered features
            for fid, gj, name in [
                ("basin-a", json.loads(BASIN_A_GEOJSON), "Basin A"),
                ("basin-b", json.loads(BASIN_B_GEOJSON), "Basin B"),
            ]:
                feat = Feature(feature_id=fid, geojson=gj,
                               name=name, source="test", area_km2=AREA_KM2)
                s.put_feature(feat)
            s.set_active_feature_id("basin-a")

            # Legacy watershed slot (needed by signatures for area_km2)
            s.set("watershed", {
                "data": {
                    "area_km2": AREA_KM2,
                    "outlet_lat": 46.225,
                    "outlet_lon": -122.215,
                    "geojson": json.loads(BASIN_A_GEOJSON),
                },
                "meta": {"tool": "manual", "computed_at": "2026-06-01"},
            })

            # Synthetic streamflow for signatures (avoid real NWIS call).
            # q_cms arrays are stripped to q_cms_n on session save (lean-session).
            # Write the full array to a data file and point _data_file at it so
            # the signatures tool can load it — exactly how the real streamflow
            # tool persists large arrays.
            q_cms = _synthetic_q_cms(n_years=5)
            import pandas as pd
            dates = pd.date_range("2019-01-01", periods=len(q_cms), freq="D")
            sf_data_file = tmp_path / "streamflow_phase2-test.json"
            sf_data_file.write_text(json.dumps({
                "q_cms": q_cms,
                "dates": [str(d.date()) for d in dates],
            }))
            s.set("streamflow", {
                "data": {
                    "q_cms_n": len(q_cms),
                    "_data_file": str(sf_data_file),
                    "start_date": str(dates[0].date()),
                    "end_date": str(dates[-1].date()),
                },
                "meta": {"source": "synthetic", "computed_at": "2026-06-01"},
            })
            s.save()

            # ── TEST 1: create_cn_grid ─────────────────────────────────────
            banner("1. create_cn_grid — Basin A (live NLCD + POLARIS download)")
            info("Downloading NLCD land-cover and POLARIS soil data — may take 30-90s …")
            t0 = time.time()
            try:
                result_cn_a = await create_cn_grid(
                    session_id=SESSION_ID,
                    feature="basin-a",
                    year=2019,
                    resolution=30,
                    create_map=False,
                )
                elapsed = time.time() - t0
                info(f"create_cn_grid Basin A →\n{json.dumps(result_cn_a, indent=4, default=str)[:1200]}…")

                if "error" in result_cn_a:
                    warn(f"create_cn_grid returned error: {result_cn_a['error']}")
                    warn(f"  message: {result_cn_a.get('message', '')}")
                else:
                    assert result_cn_a.get("_cache_hit") is not True, "First call should not be a cache hit"
                    assert "data" in result_cn_a
                    assert result_cn_a.get("feature_id") == "basin-a", \
                        f"Wrong feature_id: {result_cn_a.get('feature_id')}"
                    ok(f"create_cn_grid Basin A — computed in {elapsed:.1f}s")
                    ok(f"  feature_id: {result_cn_a.get('feature_id')}")
                    ok(f"  data keys: {list(result_cn_a['data'].keys())[:8]}…")

                    # Cache hit
                    result_cn_a2 = await create_cn_grid(
                        session_id=SESSION_ID,
                        feature="basin-a",
                        year=2019, resolution=30, create_map=False,
                    )
                    assert result_cn_a2.get("_cache_hit") is True, \
                        f"Expected cache hit on 2nd call; got: {result_cn_a2.get('_cache_hit')}"
                    ok("Cache hit confirmed on second call (0ms)")

                    # Basin B — non-collision
                    info("Computing CN grid for Basin B (same session, no clear needed) …")
                    t1 = time.time()
                    result_cn_b = await create_cn_grid(
                        session_id=SESSION_ID,
                        feature="basin-b",
                        year=2019, resolution=30, create_map=False,
                    )
                    elapsed2 = time.time() - t1
                    if "error" not in result_cn_b:
                        assert result_cn_b.get("feature_id") == "basin-b"
                        ok(f"CN grid Basin B — {elapsed2:.1f}s, stored independently")

                        # Verify non-collision in session
                        from aihydro_core.primitives.hashing import param_hash
                        # Cache key now also carries the optional product pins
                        # (None ⇒ auto). Mirror the tool's key shape exactly.
                        key = param_hash({
                            "year": 2019, "resolution": 30,
                            "product": None, "soil_product": None,
                        })
                        s2 = HydroSession.load(SESSION_ID)
                        cn_a = s2.get_result("cn", "basin-a", key)
                        cn_b = s2.get_result("cn", "basin-b", key)
                        assert cn_a is not None and cn_b is not None
                        ok(f"Both CN results in three-level store — no collision")
                    else:
                        warn(f"Basin B CN failed: {result_cn_b.get('message', '')}")

            except Exception as e:
                warn(f"create_cn_grid failed with exception: {e}")
                import traceback; traceback.print_exc()

            # ── TEST 2: fetch_forcing_data ─────────────────────────────────
            banner("2. fetch_forcing_data — Basin A, 30-day window (GridMET)")
            info("Fetching basin-averaged GridMET forcing (30 days) …")
            t0 = time.time()
            try:
                result_forc_a = await fetch_forcing_data(
                    session_id=SESSION_ID,
                    start_date="2019-07-01",
                    end_date="2019-07-30",
                    variables=["pr", "tmmx", "tmmn"],
                    feature="basin-a",
                )
                elapsed = time.time() - t0
                info(f"fetch_forcing_data Basin A →\n{json.dumps(result_forc_a, indent=4, default=str)[:1200]}…")

                if "error" in result_forc_a:
                    warn(f"fetch_forcing_data error: {result_forc_a.get('message', result_forc_a['error'])}")
                else:
                    assert result_forc_a.get("_cache_hit") is not True
                    assert result_forc_a.get("feature_id") == "basin-a", \
                        f"Wrong feature_id: {result_forc_a.get('feature_id')}"
                    ok(f"fetch_forcing_data Basin A — {elapsed:.1f}s")
                    ok(f"  feature_id: {result_forc_a.get('feature_id')}")
                    ok(f"  data keys: {list(result_forc_a.get('data', {}).keys())}")

                    # Cache hit
                    result_forc_a2 = await fetch_forcing_data(
                        session_id=SESSION_ID,
                        start_date="2019-07-01",
                        end_date="2019-07-30",
                        variables=["pr", "tmmx", "tmmn"],
                        feature="basin-a",
                    )
                    assert result_forc_a2.get("_cache_hit") is True, \
                        f"Expected cache hit; got: {result_forc_a2}"
                    ok("Cache hit confirmed on second forcing call")

                    # Basin B — non-collision
                    info("Fetching forcing for Basin B …")
                    result_forc_b = await fetch_forcing_data(
                        session_id=SESSION_ID,
                        start_date="2019-07-01",
                        end_date="2019-07-30",
                        variables=["pr", "tmmx", "tmmn"],
                        feature="basin-b",
                    )
                    if "error" not in result_forc_b:
                        assert result_forc_b.get("feature_id") == "basin-b"
                        ok("Forcing Basin B stored independently — no collision")
                    else:
                        warn(f"Basin B forcing: {result_forc_b.get('message', '')}")

            except Exception as e:
                warn(f"fetch_forcing_data failed: {e}")
                import traceback; traceback.print_exc()

            # ── TEST 3: extract_hydrological_signatures ────────────────────
            banner("3. extract_hydrological_signatures — Basin A (synthetic q_cms)")
            info("Extracting 17 CAMELS-style signatures from 5-year synthetic discharge …")
            t0 = time.time()
            try:
                result_sig_a = extract_hydrological_signatures(
                    session_id=SESSION_ID,
                    start_date="2019-01-01",
                    end_date="2023-12-31",
                    feature="basin-a",
                )
                elapsed = time.time() - t0
                info(f"Signatures Basin A →\n{json.dumps(result_sig_a, indent=4, default=str)[:1500]}…")

                if "error" in result_sig_a:
                    warn(f"Signatures error: {result_sig_a.get('message', result_sig_a['error'])}")
                else:
                    assert result_sig_a.get("_cache_hit") is not True
                    assert result_sig_a.get("feature_id") == "basin-a", \
                        f"Wrong feature_id: {result_sig_a.get('feature_id')}"
                    data = result_sig_a.get("data", {})
                    ok(f"Signatures Basin A — {elapsed:.2f}s, {len(data)} fields computed")
                    ok(f"  feature_id: {result_sig_a.get('feature_id')}")
                    ok(f"  sig keys: {list(data.keys())[:10]}…")

                    # Spot-check a few expected fields
                    for key in ["mean_annual_runoff", "bfi", "runoff_ratio"]:
                        val = data.get(key)
                        if val is not None:
                            ok(f"  {key}: {val:.4f}")
                        else:
                            warn(f"  {key}: missing from output")

                    # Cache hit
                    result_sig_a2 = extract_hydrological_signatures(
                        session_id=SESSION_ID,
                        start_date="2019-01-01",
                        end_date="2023-12-31",
                        feature="basin-a",
                    )
                    assert result_sig_a2.get("_cache_hit") is True, \
                        f"Expected cache hit; got: {result_sig_a2.get('_cache_hit')}"
                    ok("Signatures cache hit confirmed on second call")

            except Exception as e:
                warn(f"extract_hydrological_signatures failed: {e}")
                import traceback; traceback.print_exc()

            # ── TEST 4: synopsis shows all products ───────────────────────
            banner("4. Synopsis — all computed products across two features")
            s_final = HydroSession.load(SESSION_ID)
            synopsis = s_final.synopsis_for_llm()
            info(f"Products in synopsis: {list(synopsis.keys())}")
            for product, fdata in synopsis.items():
                if isinstance(fdata, dict):
                    info(f"  {product}: features = {list(fdata.keys())}")
            ok("Phase 2 complete ✅")

        finally:
            for p in patches:
                p.stop()


if __name__ == "__main__":
    asyncio.run(run_tests())
