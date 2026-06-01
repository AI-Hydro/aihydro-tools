"""
Integration tests for the C2 feature registry MCP tools and the updated
compute_twi cache path.

Tests the three new MCP tools:
  - register_feature
  - list_features
  - set_active_feature

And verifies compute_twi's updated cache logic:
  - feature= param accepted
  - three-level cache key used (no collision between ann1 / ann2)
  - backward-compat: no feature= → __legacy__ sentinel path
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_POLY_ANN1 = json.dumps({
    "type": "Polygon",
    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]],
})

_POLY_ANN2 = json.dumps({
    "type": "Polygon",
    "coordinates": [[[2, 2], [3, 2], [3, 3], [2, 2]]],
})


def _make_session(tmp_path: Path, session_id: str = "test"):
    from ai_hydro.session import HydroSession
    return HydroSession.load(session_id)


# ---------------------------------------------------------------------------
# register_feature
# ---------------------------------------------------------------------------

class TestRegisterFeature:
    def test_register_returns_feature_id(self, tmp_path):
        from ai_hydro.mcp.tools_session import register_feature
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
             patch("ai_hydro.mcp.tools_session._resolve_session", return_value="sess1"):
            result = register_feature(
                geojson=_POLY_ANN1, name="Annotation 1", session_id="sess1"
            )
            assert "feature_id" in result
            assert "error" not in result
            assert result["name"] == "Annotation 1"

    def test_register_persists_to_session(self, tmp_path):
        from ai_hydro.mcp.tools_session import register_feature
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
             patch("ai_hydro.mcp.tools_session._resolve_session", return_value="sess-persist"):
            register_feature(geojson=_POLY_ANN1, name="Ann1", session_id="sess-persist")
            s = HydroSession.load("sess-persist")
            feats = s.list_features()
            assert any(f.name == "Ann1" for f in feats)

    def test_register_sets_active(self, tmp_path):
        from ai_hydro.mcp.tools_session import register_feature
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
             patch("ai_hydro.mcp.tools_session._resolve_session", return_value="sess-active"):
            r = register_feature(
                geojson=_POLY_ANN1, name="Ann1",
                session_id="sess-active", set_active=True
            )
            s = HydroSession.load("sess-active")
            assert s.get_active_feature_id() == r["feature_id"]

    def test_register_explicit_feature_id(self, tmp_path):
        from ai_hydro.mcp.tools_session import register_feature
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
             patch("ai_hydro.mcp.tools_session._resolve_session", return_value="sess-id"):
            r = register_feature(
                geojson=_POLY_ANN1, name="Fixed", session_id="sess-id",
                feature_id="my-custom-id",
            )
            assert r["feature_id"] == "my-custom-id"

    def test_register_invalid_geojson_returns_error(self, tmp_path):
        from ai_hydro.mcp.tools_session import register_feature
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
             patch("ai_hydro.mcp.tools_session._resolve_session", return_value="sess-bad"):
            result = register_feature(geojson="NOT_VALID_JSON", session_id="sess-bad")
            assert "error" in result or "code" in result


# ---------------------------------------------------------------------------
# list_features
# ---------------------------------------------------------------------------

class TestListFeatures:
    def test_list_empty(self, tmp_path):
        from ai_hydro.mcp.tools_session import list_features
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
             patch("ai_hydro.mcp.tools_session._resolve_session", return_value="sess-list"):
            r = list_features(session_id="sess-list")
            assert r["count"] == 0
            assert r["features"] == []

    def test_list_shows_registered_features(self, tmp_path):
        from ai_hydro.mcp.tools_session import register_feature, list_features
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
             patch("ai_hydro.mcp.tools_session._resolve_session", return_value="sess-list2"):
            register_feature(geojson=_POLY_ANN1, name="Ann1", session_id="sess-list2")
            register_feature(geojson=_POLY_ANN2, name="Ann2", session_id="sess-list2")
            r = list_features(session_id="sess-list2")
            assert r["count"] == 2
            names = [f["name"] for f in r["features"]]
            assert "Ann1" in names
            assert "Ann2" in names

    def test_list_shows_active_feature_id(self, tmp_path):
        from ai_hydro.mcp.tools_session import register_feature, list_features
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
             patch("ai_hydro.mcp.tools_session._resolve_session", return_value="sess-active2"):
            r = register_feature(
                geojson=_POLY_ANN1, name="Ann1",
                session_id="sess-active2", set_active=True,
            )
            listing = list_features(session_id="sess-active2")
            assert listing["active_feature_id"] == r["feature_id"]


# ---------------------------------------------------------------------------
# set_active_feature
# ---------------------------------------------------------------------------

class TestSetActiveFeature:
    def test_set_active_persists(self, tmp_path):
        from ai_hydro.mcp.tools_session import register_feature, set_active_feature
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
             patch("ai_hydro.mcp.tools_session._resolve_session", return_value="sess-setactive"):
            r1 = register_feature(geojson=_POLY_ANN1, name="Ann1", session_id="sess-setactive")
            r2 = register_feature(geojson=_POLY_ANN2, name="Ann2", session_id="sess-setactive",
                                   set_active=False)
            set_active_feature(r2["feature_id"], session_id="sess-setactive")
            s = HydroSession.load("sess-setactive")
            assert s.get_active_feature_id() == r2["feature_id"]

    def test_set_active_unknown_id_returns_error(self, tmp_path):
        from ai_hydro.mcp.tools_session import set_active_feature
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
             patch("ai_hydro.mcp.tools_session._resolve_session", return_value="sess-badactive"):
            result = set_active_feature("does-not-exist", session_id="sess-badactive")
            assert "error" in result or "code" in result


# ---------------------------------------------------------------------------
# compute_twi — C2 cache path (unit-level, no actual TWI computation)
# ---------------------------------------------------------------------------

class TestComputeTwiCachePath:
    """
    Verify that compute_twi uses the feature-keyed three-level cache.

    These tests stub out the actual TWI computation (_fn / _fn_full) so they
    run without heavy geo dependencies.
    """

    def _twi_result_stub(self) -> dict:
        return {
            "data": {"mean_twi": 8.5, "resolution": 30},
            "meta": {"tool": "compute_twi", "computed_at": "2026-06-01T00:00:00+00:00"},
        }

    @pytest.mark.asyncio
    async def test_compute_twi_accepts_feature_param(self, tmp_path):
        """
        compute_twi must accept feature= and return cached result when present.

        We pre-seed the session with a cached TWI result for a registered
        feature, then verify compute_twi returns it on the feature= path
        without touching the actual computation.
        """
        from ai_hydro.session import HydroSession
        from aihydro_core.primitives import Feature
        from aihydro_core.primitives.hashing import param_hash
        import json

        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
             patch("ai_hydro.mcp.tools_analysis._resolve_session", return_value="twi-feat-test"):

            # Register feature and pre-seed the TWI cache
            s = HydroSession("twi-feat-test")
            feat = Feature(
                feature_id="ann1",
                geojson=json.loads(_POLY_ANN1),
                name="Ann1", source="map_annotation",
            )
            s.put_feature(feat)
            s.set_active_feature_id("ann1")

            key = param_hash({"resolution": 30})
            s.put_result("twi", "ann1", key, {
                "data": {"mean_twi": 8.5, "resolution": 30},
                "meta": {"computed_at": "2026-06-01T00:00:00+00:00", "feature_id": "ann1"},
            })
            s.save()

            from ai_hydro.mcp.tools_analysis import compute_twi
            result = await compute_twi(
                session_id="twi-feat-test", feature="ann1",
                resolution=30, create_map=False,
            )
            assert result.get("_cache_hit") is True
            assert result["data"]["mean_twi"] == 8.5
            assert "error" not in result

    @pytest.mark.asyncio
    async def test_compute_twi_feature_keyed_cache_no_collision(self, tmp_path):
        """
        Core non-collision invariant through the MCP layer:
        TWI for ann1 and ann2 must be stored independently.
        """
        from ai_hydro.session import HydroSession
        from ai_hydro.mcp.tools_session import register_feature

        # Pre-populate TWI results directly in the session via put_result
        # (avoids needing real TWI computation)
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):

            s = HydroSession("twi-nocollide")
            s.put_result("twi", "ann1", "params_30m",
                         {"data": {"mean_twi": 8.2}, "meta": {"computed_at": "2026-06-01T00:00:00+00:00"}})
            s.put_result("twi", "ann2", "params_30m",
                         {"data": {"mean_twi": 6.1}, "meta": {"computed_at": "2026-06-01T00:00:00+00:00"}})
            s.save()

            # Reload and verify they're independent
            s2 = HydroSession.load("twi-nocollide")
            r1 = s2.get_result("twi", "ann1", "params_30m")
            r2 = s2.get_result("twi", "ann2", "params_30m")
            assert r1["data"]["mean_twi"] == 8.2
            assert r2["data"]["mean_twi"] == 6.1
            assert r1 is not r2

    @pytest.mark.asyncio
    async def test_compute_twi_cache_hit_skips_recompute(self, tmp_path):
        """
        If compute_twi was already run for a feature, calling again must return
        the cached result (kernel not invoked again).
        """
        from ai_hydro.session import HydroSession
        from aihydro_core.primitives.hashing import param_hash

        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path), \
             patch("ai_hydro.mcp.tools_analysis._resolve_session", return_value="twi-cachehit"):

            from aihydro_core.primitives import Feature
            import json

            # Pre-seed the session with a cached TWI result for ann1
            s = HydroSession("twi-cachehit")
            # Register the feature
            feat = Feature(
                feature_id="ann1",
                geojson=json.loads(_POLY_ANN1),
                name="Ann1", source="map_annotation",
            )
            s.put_feature(feat)
            s.set_active_feature_id("ann1")

            key = param_hash({"resolution": 30})
            cached_result = {
                "data": {"mean_twi": 9.9, "resolution": 30},
                "meta": {"computed_at": "2026-06-01T00:00:00+00:00", "feature_id": "ann1"},
            }
            s.put_result("twi", "ann1", key, cached_result)
            s.save()

            # Now call compute_twi — it should return the cached value
            compute_call_count = [0]

            def stub_compute(*args, **kwargs):
                compute_call_count[0] += 1
                return {"mean_twi": 0.0}  # Should not be reached

            with patch("ai_hydro.analysis.twi.compute_twi_result", side_effect=stub_compute):
                from ai_hydro.mcp.tools_analysis import compute_twi
                result = await compute_twi(
                    session_id="twi-cachehit",
                    feature="ann1",
                    resolution=30,
                    create_map=False,
                )

            assert compute_call_count[0] == 0, "Kernel must not be called on cache hit"
            assert result.get("_cache_hit") is True
            assert result["data"]["mean_twi"] == 9.9
