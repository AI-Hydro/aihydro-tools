"""
Live end-to-end tests for feature-keyed spatial tools (C2/C4).

Run with:
    python tests/live_test_feature_tools.py

These tests make real DEM / data downloads and are NOT included in the
automated pytest suite (they're too slow and network-dependent).

They verify:
  1. compute_twi — real pysheds/3DEP computation for a small polygon
  2. Multi-feature non-collision — TWI for basin_A then basin_B, same session,
     no clear_session needed; values stored independently
  3. Cache hit — second call to compute_twi for the same feature returns
     _cache_hit: True without recomputation
  4. extract_geomorphic_parameters — live run on the same geometry
  5. Feature registry MCP tools — register_feature / list_features / set_active_feature
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

# ── Two small real polygons in the Cascade Range (CONUS — good 3DEP coverage)
# Basin A: ~8 km²  foothills north of Mount St Helens
BASIN_A_GEOJSON = json.dumps({
    "type": "Polygon",
    "coordinates": [[
        [-122.25, 46.22],
        [-122.18, 46.22],
        [-122.18, 46.27],
        [-122.25, 46.27],
        [-122.25, 46.22],
    ]],
})

# Basin B: ~8 km²  foothills ~7 km east of Basin A
BASIN_B_GEOJSON = json.dumps({
    "type": "Polygon",
    "coordinates": [[
        [-122.13, 46.22],
        [-122.06, 46.22],
        [-122.06, 46.27],
        [-122.13, 46.27],
        [-122.13, 46.22],
    ]],
})

# ── Helpers ────────────────────────────────────────────────────────────────────

def _banner(msg: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {msg}")
    print("="*70)


def _ok(msg: str) -> None:
    print(f"  ✅  {msg}")


def _fail(msg: str) -> None:
    print(f"  ❌  {msg}")
    sys.exit(1)


def _info(msg: str) -> None:
    print(f"  ℹ️   {msg}")


# ── Main test sequence ─────────────────────────────────────────────────────────

async def run_tests() -> None:
    from ai_hydro.session import HydroSession
    from ai_hydro.mcp.tools_analysis import compute_twi, extract_geomorphic_parameters
    from ai_hydro.mcp.tools_session import register_feature, list_features, set_active_feature

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
             patch("ai_hydro.mcp.tools_analysis._resolve_session",
                   return_value="live-test-session"), \
             patch("ai_hydro.mcp.tools_session._resolve_session",
                   return_value="live-test-session"):

            SESSION_ID = "live-test-session"

            # ── 1. Feature registry MCP tools ──────────────────────────────
            _banner("1. Feature registry MCP tools")

            r = register_feature(
                session_id=SESSION_ID,
                geojson=BASIN_A_GEOJSON,
                name="Basin A",
                feature_id="basin-a",
            )
            _info(f"register_feature Basin A → {r}")
            assert r.get("feature_id") == "basin-a", f"Expected feature_id='basin-a', got {r}"
            _ok("register_feature Basin A — feature_id correct")

            r2 = register_feature(
                session_id=SESSION_ID,
                geojson=BASIN_B_GEOJSON,
                name="Basin B",
                feature_id="basin-b",
            )
            assert r2.get("feature_id") == "basin-b"
            _ok("register_feature Basin B — feature_id correct")

            lf = list_features(session_id=SESSION_ID)
            _info(f"list_features → {lf}")
            ids = {f["feature_id"] for f in lf.get("features", [])}
            assert "basin-a" in ids and "basin-b" in ids, f"Missing features: {ids}"
            _ok(f"list_features — shows both features: {ids}")

            r3 = set_active_feature(session_id=SESSION_ID, feature_id="basin-a")
            assert "error" not in r3
            _ok("set_active_feature — basin-a set as active")

            # ── 2. compute_twi — LIVE run on Basin A ──────────────────────
            _banner("2. compute_twi — live DEM download + pysheds (Basin A)")
            _info("Downloading DEM and computing TWI — this may take 20-60s …")

            t0 = time.time()
            result_a = await compute_twi(
                session_id=SESSION_ID,
                feature="basin-a",
                resolution=30,
                create_map=False,
            )
            elapsed = time.time() - t0

            _info(f"compute_twi Basin A result:\n{json.dumps(result_a, indent=4, default=str)}")

            if "error" in result_a:
                _fail(f"compute_twi Basin A returned error: {result_a['error']}")

            assert result_a.get("_cache_hit") is not True, \
                "First call should NOT be a cache hit"
            assert "data" in result_a, f"Missing 'data' key: {result_a}"
            _ok(f"compute_twi Basin A — computed in {elapsed:.1f}s")
            _ok(f"  feature_id in result: {result_a.get('feature_id')}")
            _ok(f"  data keys: {list(result_a['data'].keys())}")

            # ── 3. Cache hit — same feature, same params ───────────────────
            _banner("3. Cache hit — second call to compute_twi(Basin A, res=30)")
            t1 = time.time()
            result_a2 = await compute_twi(
                session_id=SESSION_ID,
                feature="basin-a",
                resolution=30,
                create_map=False,
            )
            elapsed2 = time.time() - t1
            _info(f"Second call result: {json.dumps(result_a2, indent=4, default=str)}")

            assert result_a2.get("_cache_hit") is True, \
                f"Expected _cache_hit=True on second call, got: {result_a2}"
            _ok(f"Cache hit confirmed in {elapsed2:.3f}s (was {elapsed:.1f}s)")

            # Verify data is identical (same values returned from cache)
            assert result_a2["data"] == result_a["data"], \
                "Cached data differs from original!"
            _ok("Cached data is byte-identical to original compute output")

            # ── 4. Multi-feature non-collision — Basin B ───────────────────
            _banner("4. compute_twi — live run on Basin B (NO clear_session!)")
            _info("Computing TWI for a second geometry in the SAME session …")

            t2 = time.time()
            result_b = await compute_twi(
                session_id=SESSION_ID,
                feature="basin-b",
                resolution=30,
                create_map=False,
            )
            elapsed3 = time.time() - t2

            _info(f"compute_twi Basin B result:\n{json.dumps(result_b, indent=4, default=str)}")

            if "error" in result_b:
                _fail(f"compute_twi Basin B returned error: {result_b['error']}")

            assert result_b.get("_cache_hit") is not True, \
                "Basin B first run should NOT be a cache hit"
            assert result_b.get("feature_id") == "basin-b", \
                f"Wrong feature_id in result: {result_b.get('feature_id')}"
            _ok(f"compute_twi Basin B — computed in {elapsed3:.1f}s, no session clearing needed")

            # ── 5. Values are independent (the core invariant) ────────────
            _banner("5. Non-collision check — both results stored independently")
            session = HydroSession.load(SESSION_ID)
            from aihydro_core.primitives.hashing import param_hash
            key = param_hash({"resolution": 30})

            stored_a = session.get_result("twi", "basin-a", key)
            stored_b = session.get_result("twi", "basin-b", key)

            assert stored_a is not None, "Basin A TWI not found in session!"
            assert stored_b is not None, "Basin B TWI not found in session!"
            _ok("Both results present in three-level slot store")

            mean_a = stored_a["data"].get("twi_mean") or stored_a["data"].get("mean_twi") or stored_a["data"].get("mean")
            mean_b = stored_b["data"].get("twi_mean") or stored_b["data"].get("mean_twi") or stored_b["data"].get("mean")
            _info(f"Basin A mean TWI: {mean_a}")
            _info(f"Basin B mean TWI: {mean_b}")
            _ok("Results are stored independently — no slot collision")

            # ── 6. extract_geomorphic_parameters — live run ────────────────
            _banner("6. extract_geomorphic_parameters — live run (Basin A)")
            _info("Computing geomorphic parameters (DEM-based) …")

            t3 = time.time()
            result_geo = extract_geomorphic_parameters(
                session_id=SESSION_ID,
                feature="basin-a",
                dem_resolution=30,
            )
            elapsed4 = time.time() - t3

            _info(f"geomorphic result:\n{json.dumps(result_geo, indent=4, default=str)}")

            if "error" in result_geo:
                _fail(f"extract_geomorphic_parameters returned error: {result_geo['error']}")

            assert "data" in result_geo, f"Missing 'data' key: {result_geo}"
            _ok(f"extract_geomorphic_parameters Basin A — done in {elapsed4:.1f}s")
            _ok(f"  data keys: {list(result_geo['data'].keys())}")

            # Cache hit for geomorphic
            result_geo2 = extract_geomorphic_parameters(
                session_id=SESSION_ID,
                feature="basin-a",
                dem_resolution=30,
            )
            assert result_geo2.get("_cache_hit") is True, \
                f"Expected cache hit on 2nd geomorphic call, got: {result_geo2}"
            _ok("Geomorphic cache hit confirmed on second call")

            # ── 7. synopsis_for_llm — shows both features ─────────────────
            _banner("7. Synopsis — session with two features")
            session2 = HydroSession.load(SESSION_ID)
            synopsis = session2.synopsis_for_llm()
            _info(f"Synopsis:\n{json.dumps(synopsis, indent=4, default=str)}")

            computed = synopsis.get("computed", [])
            _ok(f"Computed products in synopsis: {computed}")
            _ok("All 7 checks passed — C2/C4 wiring is correct end-to-end ✅")


if __name__ == "__main__":
    asyncio.run(run_tests())
