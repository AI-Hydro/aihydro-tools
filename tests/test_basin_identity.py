"""Basin identity (tool integrity T1-B; audit findings D01-D05, D33, D46).

Each test reproduces the audit probe (``tool-audit/data/probes/p2.py``) with the compute
kernels stubbed, so it runs offline: a used session that gets a new basin must never answer
with the old basin's numbers, ``feature=`` must bind or refuse, and sessions written before
the stamp existed must keep loading, flagged and never silently trusted.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
import uuid
from pathlib import Path

import pytest

PA = {"type": "Polygon", "coordinates": [[[-69, 46], [-68, 46], [-68, 47], [-69, 47], [-69, 46]]]}
PB = {"type": "Polygon", "coordinates": [[[-100, 35], [-99, 35], [-99, 36], [-100, 36], [-100, 35]]]}
REPO = Path(__file__).resolve().parent.parent


def _call(tool, **kw):
    f = getattr(tool, "fn", tool)
    r = f(**kw)
    return asyncio.run(r) if inspect.iscoroutine(r) else r


def _new_session(poly=PA, area=1000.0, **attrs) -> str:
    from ai_hydro.session import HydroSession
    from ai_hydro.session.store import _SESSIONS_DIR

    sid = "bi-" + uuid.uuid4().hex[:8]
    s = HydroSession.load(sid)
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    p = _SESSIONS_DIR / f"{sid}.geojson"
    p.write_text(json.dumps(poly))
    s.set("watershed", {"data": {"area_km2": area, "gauge_id": attrs.get("site_id"),
                                 "geometry_geojson_path": str(p)}, "meta": {}})
    for k, v in attrs.items():
        setattr(s, k, v)
    s.save()
    return sid


@pytest.fixture
def ta():
    import ai_hydro.mcp.tools_analysis as m
    return m


@pytest.fixture
def twi_calls(monkeypatch):
    """compute_twi kernel stub that records the centroid it was asked about."""
    from shapely.geometry import shape
    from ai_hydro.core import HydroMeta, HydroResult
    import ai_hydro.analysis.twi as twimod

    calls: list = []

    def fake_twi(watershed_geojson, resolution=30):
        c = shape(watershed_geojson).centroid
        calls.append((round(c.x, 2), round(c.y, 2)))
        return HydroResult(data={"twi_mean": c.x}, meta=HydroMeta(tool="x", version="0"))

    monkeypatch.setattr(twimod, "compute_twi_result", fake_twi)
    return calls


@pytest.fixture
def redelineate(monkeypatch):
    """Stub the point delineation (basin B) and the map push."""
    from ai_hydro.core import HydroMeta, HydroResult
    import ai_hydro.analysis.delineation as dl
    import ai_hydro.mcp.map_events as me

    def fake(lat, lon, expected_area_km2=None, method="auto", verbose=False, name=None):
        return HydroResult(data={"geometry_geojson": PB, "area_km2": 999.0, "method_used": "stub",
                                 "quality_flags": []}, meta=HydroMeta(tool="x", version="0"))

    monkeypatch.setattr(dl, "delineate_from_point", fake)
    monkeypatch.setattr(me, "push_layer", lambda *a, **k: True)
    monkeypatch.setattr(me, "push_gauge_point", lambda *a, **k: True)


def _put_streamflow(sid, n=3000):
    from ai_hydro.session import HydroSession
    import pandas as pd

    s = HydroSession.load(sid)
    dates = pd.date_range("2000-01-01", periods=n, freq="D").strftime("%Y-%m-%d").tolist()
    s.set("streamflow", {"data": {"dates": dates, "q_cms": [1.0 + (i % 50) for i in range(n)]},
                         "meta": {"params": {"gauge_id": "01013500"}}})
    s.save()


# ── D01 / D02: a new basin in a used session ───────────────────────────────────────────────

def test_d01_twi_cache_not_served_after_redelineation(ta, twi_calls, redelineate):
    sid = _new_session(site_id="01013500", site_type="usgs_gauge")
    r1 = _call(ta.compute_twi, session_id=sid, create_map=False)
    assert not r1.get("error") and twi_calls == [(-68.5, 46.5)]
    r1b = _call(ta.compute_twi, session_id=sid, create_map=False)
    assert r1b["_cache_hit"] is True and len(twi_calls) == 1          # same basin: still a hit

    _call(ta.delineate_watershed_from_point, lat=35.5, lon=-99.5, session_id=sid)

    r2 = _call(ta.compute_twi, session_id=sid, create_map=False)
    assert not r2.get("_cache_hit"), "basin B was answered from basin A's cache"
    assert twi_calls[-1] == (-99.5, 35.5) and len(twi_calls) == 2
    assert r2["data"]["twi_mean"] == pytest.approx(-99.5)


def test_d02_watershed_change_clears_gauge_and_dependent_slots(ta, redelineate):
    from ai_hydro.session import HydroSession

    sid = _new_session(site_id="01013500", site_type="usgs_gauge")
    _put_streamflow(sid)
    s = HydroSession.load(sid)
    s.set("geomorphic", {"data": {"DA_km2": 1000.0}, "meta": {}})
    s.set("camels", {"data": {"gauge_ids": ["01013500"]}, "meta": {}})
    s.save()

    _call(ta.delineate_watershed_from_point, lat=35.5, lon=-99.5, session_id=sid)

    s = HydroSession.load(sid)
    assert s.site_id == "" and s.streamflow is None and s.geomorphic is None
    assert s.camels is not None, "camels (multi-basin by design) must survive a basin change"
    change = s.extra["basin_changes"][-1]
    assert change["cleared_site_id"] == "01013500"
    assert {"streamflow", "geomorphic"} <= set(change["dropped_slots"])


def test_d02_flood_frequency_and_signatures_do_not_reuse_old_gauge(ta, redelineate, monkeypatch):
    import ai_hydro.analysis.signatures as sg
    from ai_hydro.core import HydroMeta, HydroResult

    sid = _new_session(site_id="01013500", site_type="usgs_gauge")
    _put_streamflow(sid)
    _call(ta.delineate_watershed_from_point, lat=35.5, lon=-99.5, session_id=sid)

    ffa = _call(ta.compute_flood_frequency, session_id=sid)
    assert ffa["error"] and ffa["code"] == "MISSING_PREREQUISITES"

    seen: list = []
    monkeypatch.setattr(sg, "extract_hydrological_signatures",
                        lambda **kw: seen.append(kw) or HydroResult(data={}, meta=HydroMeta(tool="x", version="0")))
    out = _call(ta.extract_hydrological_signatures, session_id=sid, precipitation="skip")
    assert not seen, "signature kernel ran for basin B on basin A's gauge/flow"
    assert out["error"]


def test_same_gauge_redelineation_keeps_site_id(ta, redelineate):
    from ai_hydro.session import HydroSession
    s_id = _new_session(site_id="01013500")
    s = HydroSession.load(s_id)
    ws = json.loads(json.dumps(s.watershed))
    ws["data"]["gauge_id"] = "01013500"
    s.set("watershed", ws)            # unchanged geometry, same gauge
    assert s.site_id == "01013500"


def test_stale_slot_is_not_served_even_if_it_survives(ta):
    """A slot stamped for another basin is skipped on read (guards a hand-edited or racing writer)."""
    from ai_hydro.session import HydroSession
    sid = _new_session(site_id="01013500")
    _put_streamflow(sid)
    s = HydroSession.load(sid)
    s._slots["streamflow"]["__legacy__"][""]["meta"]["basin_identity"] = {"geometry_digest": "deadbeef"}
    assert s.get("streamflow") is None and s.streamflow is None


# ── D03 / D04: feature= binds or refuses ───────────────────────────────────────────────────

def test_d03_twi_unregistered_feature_refused(ta, twi_calls):
    sid = _new_session(site_id="01013500")
    r = _call(ta.compute_twi, session_id=sid, create_map=False, feature="basin_that_does_not_exist")
    assert r["error"] and r["code"] == "FEATURE_NOT_RESOLVED"
    assert "basin_that_does_not_exist" in r["message"] and not twi_calls


def test_d03_shared_resolver_refuses_for_every_feature_aware_tool(ta):
    from ai_hydro.session import HydroSession
    sid = _new_session()
    s = HydroSession.load(sid)
    with pytest.raises(ta.FeatureNotResolvedError) as ei:
        ta._feature_cache_resolve(s, "cn", {"year": 2021}, "nonexistent", None)
    assert ei.value.to_dict()["code"] == "FEATURE_NOT_RESOLVED"
    # blank means "no feature", a geometry override skips the registry
    assert ta._feature_cache_resolve(s, "cn", {}, "  ", None)[0] == "__legacy__"
    assert ta._feature_cache_resolve(s, "cn", {}, "nonexistent", json.dumps(PB))[0] == "__override__"


@pytest.mark.parametrize("tool_name,kw", [
    ("create_cn_grid", {"create_map": False}),
    ("fetch_soil_attributes_ssurgo", {}),
    ("fetch_forcing_data", {"start_date": "2000-01-01", "end_date": "2000-12-31"}),
    ("extract_geomorphic_parameters", {}),
    ("extract_hydrological_signatures", {"precipitation": "skip"}),
])
def test_d03_unregistered_feature_refused_by_tool(ta, tool_name, kw):
    sid = _new_session(site_id="01013500")
    r = _call(getattr(ta, tool_name), session_id=sid, feature="nope", **kw)
    assert r["error"] and r["code"] == "FEATURE_NOT_RESOLVED", r


def test_d03_registered_feature_computes_on_its_own_geometry(ta, twi_calls):
    from aihydro_core.features.registry import FeatureRegistry
    from ai_hydro.session import HydroSession

    sid = _new_session()
    s = HydroSession.load(sid)
    FeatureRegistry(s).register(PB, name="other_basin", feature_id="other_basin")
    s.save()
    r = _call(ta.compute_twi, session_id=sid, create_map=False, feature="other_basin")
    assert not r.get("error") and twi_calls == [(-99.5, 35.5)] and r["feature_id"] == "other_basin"
    # and the session basin's own TWI is not the feature's
    r2 = _call(ta.compute_twi, session_id=sid, create_map=False, feature=None)
    assert r2["feature_id"] in ("__legacy__", "other_basin")


def test_d04_inundation_binds_feature_geometry(ta, monkeypatch):
    from aihydro_core.features.registry import FeatureRegistry
    from ai_hydro.session import HydroSession
    import ai_hydro.analysis.inundation as inu

    sid = _new_session()
    s = HydroSession.load(sid)
    FeatureRegistry(s).register(PB, name="other_basin", feature_id="other_basin")
    s.save()

    def stop(geom, resolution=30):
        raise RuntimeError(f"HAND stack requested for {geom.centroid.x:.2f},{geom.centroid.y:.2f}")
    monkeypatch.setattr(inu, "prepare_hand_stack", stop)

    r = _call(ta.map_flood_inundation, session_id=sid, discharge_m3s=500, feature="other_basin")
    assert "-99.50,35.50" in r["message"], r

    bad = _call(ta.map_flood_inundation, session_id=sid, discharge_m3s=500, feature="missing")
    assert bad["error"] and bad["code"] == "FEATURE_NOT_RESOLVED"

    # a feature that is not the session basin cannot borrow the session's flow slots
    need = _call(ta.map_flood_inundation, session_id=sid, return_period=100, feature="other_basin")
    assert need["code"] == "FEATURE_DISCHARGE_REQUIRED"


# ── D05 / D33 / D46: same mechanism ────────────────────────────────────────────────────────

def test_d05_signature_override_of_another_basin_refused(ta, monkeypatch):
    import ai_hydro.analysis.signatures as sg
    seen: list = []
    monkeypatch.setattr(sg, "extract_hydrological_signatures", lambda **kw: seen.append(kw))
    sid = _new_session(site_id="01013500", site_type="usgs_gauge")
    r = _call(ta.extract_hydrological_signatures, session_id=sid, geometry_geojson=json.dumps(PB),
              precipitation="skip")
    assert r["error"] and r["code"] == "GEOMETRY_OVERRIDE_NOT_SESSION_BASIN" and not seen


def test_d33_ssurgo_leaves_session_soil_slot_for_other_geometry(ta, monkeypatch):
    import aihydro_watershed.terrain.ssurgo as ss
    import aihydro_watershed.delineation.nldi_point as nd
    from aihydro_core.features.registry import FeatureRegistry
    from ai_hydro.session import HydroSession

    sid = _new_session()
    s = HydroSession.load(sid)
    FeatureRegistry(s).register(PB, name="other_basin", feature_id="other_basin")
    FeatureRegistry(s).register(PA, name="third", feature_id="third")   # >1 feature: none is implicitly active
    s.save()
    monkeypatch.setattr(nd, "is_conus", lambda lat, lon: True)
    summary = {"product": "p", "source": "s", "kw_si_mean": 0.04, "kw_mean": 0.3,
               "sand_pct_mean": 10, "silt_pct_mean": 20, "clay_pct_mean": 30}
    monkeypatch.setattr(ss, "ssurgo_catchment_attributes", lambda g: {"summary": summary})

    other = _call(ta.fetch_soil_attributes_ssurgo, session_id=sid, feature="other_basin")
    assert other["soil_slot_written"] is False and HydroSession.load(sid).get("soil") is None
    own = _call(ta.fetch_soil_attributes_ssurgo, session_id=sid)
    assert not own.get("error") and HydroSession.load(sid).get("soil")["data"]["kw_si"] == 0.04


def test_d46_slug_is_signed(ta):
    slugs = {ta._watershed_slug(None, 47.2375, -68.5828), ta._watershed_slug(None, -47.2375, 68.5828),
             ta._watershed_slug(None, 47.2375, 68.5828), ta._watershed_slug(None, -47.2375, -68.5828)}
    assert len(slugs) == 4
    assert ta._watershed_slug(None, 47.2375, 68.5828) == "basin_47p237_68p583"     # unchanged name


# ── Existing sessions on disk ──────────────────────────────────────────────────────────────

def _write_legacy_session(sid: str, tmp_poly: Path) -> None:
    """A v2 session file as written before basin identity existed: no stamps anywhere."""
    from ai_hydro.session.store import _SESSIONS_DIR
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_poly.write_text(json.dumps(PA))
    legacy = lambda v: {"__legacy__": {"": v}}                                   # noqa: E731
    raw = {
        "session_id": sid, "site_id": "01013500", "site_type": "usgs_gauge", "site_name": "x",
        "_hydro_slots_v2": True, "_features": {}, "active_feature_id": None,
        "watershed": legacy({"data": {"area_km2": 1000.0, "geometry_geojson_path": str(tmp_poly)}, "meta": {}}),
        "streamflow": legacy({"data": {"q_cms_n": 3000}, "meta": {}}),
        "twi": {"__legacy__": {"abc": {"data": {"twi_mean": 7.0}, "meta": {}}}},
    }
    (_SESSIONS_DIR / f"{sid}.json").write_text(json.dumps(raw))


def test_legacy_session_loads_and_is_flagged_not_trusted(ta, twi_calls, tmp_path):
    from ai_hydro.session import HydroSession

    sid = "legacy-" + uuid.uuid4().hex[:6]
    _write_legacy_session(sid, tmp_path / "ws.geojson")
    s = HydroSession.load(sid)                                      # never crashes

    assert s.streamflow is not None                                 # still readable ...
    assert "streamflow" in s.unverified_slots()                     # ... and flagged
    assert s.identity_note()["basin_identity"]["status"] == "unverified"
    summ = s.summary()
    assert "streamflow" in summ["basin_identity"]["slots"]
    # cache lookups never trust an unstamped result: it is a miss, so the tool recomputes
    assert s.get_result("twi", "__legacy__", "abc") is None
    r = _call(ta.compute_twi, session_id=sid, create_map=False)
    assert not r.get("_cache_hit") and twi_calls
    # the recomputed result is stamped, so the next call is a verified hit
    again = _call(ta.compute_twi, session_id=sid, create_map=False)
    assert again["_cache_hit"] is True and len(twi_calls) == 1


def test_results_derived_from_unverified_inputs_stay_unverified(ta):
    """A derived result must not launder an unstamped input into a verified stamp."""
    from ai_hydro.session import HydroSession

    sid = "legacy-" + uuid.uuid4().hex[:6]
    p = Path(__import__("tempfile").mkdtemp()) / "ws.geojson"
    _write_legacy_session(sid, p)
    ffa = _call(ta.compute_flood_frequency, session_id=sid)         # short record: error, nothing stored
    assert ffa["error"] or ffa["basin_identity"]["status"] == "unverified"
    s = HydroSession.load(sid)
    s.get("streamflow")                                             # consume the unstamped slot
    s.set("flood_frequency", {"data": {}, "meta": {}})
    res = s.get("flood_frequency")
    assert res["meta"]["basin_identity_inputs_unverified"] == ["streamflow"]
    assert "flood_frequency" in s.unverified_slots()


def test_stamps_survive_save_and_load(ta, twi_calls):
    from ai_hydro.session import HydroSession
    sid = _new_session()
    _call(ta.compute_twi, session_id=sid, create_map=False)
    s = HydroSession.load(sid)
    stamp = s.get_result("twi", "__legacy__", next(iter(s._slots["twi"]["__legacy__"])))["meta"]["basin_identity"]
    assert stamp["geometry_digest"] == s.basin_identity()["geometry_digest"]


def test_run_log_rows_of_dropped_slots_survive(ta, redelineate):
    """Sealed-run evidence lives in the run log, not in the slot a basin change drops."""
    from ai_hydro.session import HydroSession
    from ai_hydro.session.store import _run_log_read_all

    sid = _new_session(site_id="01013500")
    _put_streamflow(sid)
    before = set(_run_log_read_all(sid))
    assert before
    _call(ta.delineate_watershed_from_point, lat=35.5, lon=-99.5, session_id=sid)
    assert HydroSession.load(sid).streamflow is None
    assert before <= set(_run_log_read_all(sid))


# ── Scan: one loader ───────────────────────────────────────────────────────────────────────

#: Slots that are not basin-dependent: identity source, multi-basin by design, or not a basin result.
_NON_BASIN_SLOTS = {"watershed", "camels", "model", "_research_plan", "_run_log", "my_plugin_result"}
#: Only these modules may reach into ``_slots`` (everything else goes through the loader).
_SLOTS_ACCESS_ALLOWED = {"ai_hydro/session/store.py", "ai_hydro/validators/series_binding.py",
                         "ai_hydro/session/basin_identity.py"}


def _py_files():
    for p in sorted((REPO / "ai_hydro").rglob("*.py")):
        yield p, str(p.relative_to(REPO))


#: ``getattr(session, "_slots", ...)`` reads that are allowed, with the reason. ``identity.py`` collects the
#: verified ``BasinRef`` of every retained watershed result (all features, not just the one ``get`` serves) for
#: claim scoping; it reads the ``watershed`` slot only, which is the identity SOURCE and is not basin-dependent.
_SLOTS_GETATTR_ALLOWED = {"ai_hydro/identity.py": "retained BasinRefs of the watershed slot (identity source)"}


def test_scan_no_module_reads_slots_around_the_loader():
    offenders = []
    for p, rel in _py_files():
        for node in ast.walk(ast.parse(p.read_text())):
            if rel not in _SLOTS_ACCESS_ALLOWED and isinstance(node, ast.Attribute) and node.attr == "_slots":
                offenders.append(f"{rel}:{node.lineno}")
            # the same bypass spelled getattr(obj, "_slots", ...)
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("getattr", "hasattr")
                    and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "_slots"
                    and rel not in _SLOTS_ACCESS_ALLOWED and rel not in _SLOTS_GETATTR_ALLOWED):
                offenders.append(f"{rel}:{node.lineno} (getattr)")
    assert not offenders, f"read session slots around HydroSession.get/get_result: {offenders}"


def test_scan_getattr_slots_allow_list_has_no_stale_entries():
    for rel in _SLOTS_GETATTR_ALLOWED:
        assert '"_slots"' in (REPO / rel).read_text(), f"{rel} no longer reads _slots: drop it from the allow-list"


def test_scan_series_binding_reads_signatures_through_the_loader_contract():
    """series_binding may read ``_slots`` only for ``signatures`` and judges stamps itself via the
    session (select_signatures_result takes explicit run_id / feature / digest)."""
    src = (REPO / "ai_hydro/validators/series_binding.py").read_text()
    assert src.count("_slots") == 1 and '_slots", {}).get("signatures")' in src


def test_scan_every_written_slot_is_classified():
    """A new session slot must be declared basin-dependent or explicitly exempt, so it cannot
    silently bypass the identity stamp."""
    from ai_hydro.session.basin_identity import BASIN_DEPENDENT_SLOTS

    pat = re.compile(r"""(?:\.set\(|_session_store\(\s*\w+\s*,\s*|put_result\(\s*|record_result\(\s*)["']([a-z_]+)["']""")
    written = set()
    for p, rel in _py_files():
        if rel.startswith("ai_hydro/session/"):
            continue
        written |= set(pat.findall(p.read_text()))
    unknown = written - BASIN_DEPENDENT_SLOTS - _NON_BASIN_SLOTS
    assert not unknown, f"classify these slots in basin_identity.BASIN_DEPENDENT_SLOTS or _NON_BASIN_SLOTS: {sorted(unknown)}"


def test_scan_get_result_and_get_are_gated():
    from ai_hydro.session.store import HydroSession
    assert "_bi.judge" in inspect.getsource(HydroSession.get_result)
    assert "_bi.judge" in inspect.getsource(HydroSession._resolve_slot)
    assert "_resolve_slot" in inspect.getsource(HydroSession.get)
    assert "_bi.stamp" in inspect.getsource(HydroSession.put_result)


def test_scan_feature_aware_tools_use_the_shared_resolver():
    """Every tool taking ``feature=`` resolves it through _feature_cache_resolve or
    _resolve_registered_feature; none calls FeatureRegistry.resolve (which registers unknown refs)."""
    src = (REPO / "ai_hydro/mcp/tools_analysis.py").read_text()
    assert "registry.resolve(" not in src.replace("_active_registered_feature", "")
    tree = ast.parse(src)
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                a.arg == "feature" for a in node.args.args + node.args.kwonlyargs):
            body = ast.get_source_segment(src, node) or ""
            if node.name in ("_batch_feature_run",):
                continue
            if "_feature_cache_resolve(" not in body and "_resolve_registered_feature(" not in body:
                missing.append(node.name)
    assert not missing, f"feature= tools not using the shared resolver: {missing}"


# ── Review follow-ups (#22): same-gauge re-delineation, unverified carry-over, override without a basin ──

PA_SHIFTED = {"type": "Polygon", "coordinates": [[[-69, 46], [-68, 46], [-68, 47.0000004], [-69, 47], [-69, 46]]]}


def test_a_same_gauge_redelineation_keeps_the_gauge_record_and_drops_polygon_derived_slots():
    from ai_hydro.session import HydroSession

    sid = _new_session(site_id="01013500", site_type="usgs_gauge")
    _put_streamflow(sid)
    s = HydroSession.load(sid)
    s.set("geomorphic", {"data": {"DA_km2": 1000.0}, "meta": {}})
    s.save()
    s = HydroSession.load(sid)
    s.set("watershed", {"data": {"area_km2": 1000.0, "gauge_id": "01013500", "geometry_geojson": PA_SHIFTED},
                        "meta": {}})
    assert s.site_id == "01013500"
    assert s.get("streamflow") is not None, "the gauge's own record must survive a new outline of the same gauge"
    assert s.get("geomorphic") is None, "a result computed from the old polygon must not"
    assert "streamflow" not in s.extra["basin_changes"][-1]["dropped_slots"]


def test_a_different_gauge_still_drops_streamflow():
    from ai_hydro.session import HydroSession

    sid = _new_session(site_id="01013500", site_type="usgs_gauge")
    _put_streamflow(sid)
    s = HydroSession.load(sid)
    s.set("watershed", {"data": {"area_km2": 5.0, "gauge_id": "09999999", "geometry_geojson": PB}, "meta": {}})
    assert s.get("streamflow") is None and s.site_id == ""


def test_a_recompute_in_a_legacy_session_does_not_leave_the_session_unverified():
    from ai_hydro.session import HydroSession

    sid = _new_session(site_id="01013500", site_type="usgs_gauge")
    s = HydroSession.load(sid)
    # a legacy (unstamped) streamflow slot, as an old session file holds it
    s._slots.setdefault("streamflow", {}).setdefault("__legacy__", {})[""] = {
        "data": {"q_cms": [1.0]}, "meta": {"computed_at": "2025-01-01T00:00:00+00:00"}}
    assert s.get("streamflow") is not None and "streamflow" in s._unverified_reads
    s.put_result("streamflow", "__legacy__", "", {"data": {"q_cms": [2.0]}, "meta": {}})   # fresh recompute
    assert "streamflow" not in s._unverified_reads and s.identity_note() == {}
    s.put_result("signatures", "__legacy__", "k", {"data": {"q_mean": 1.0}, "meta": {}})
    assert "basin_identity_inputs_unverified" not in s._slots["signatures"]["__legacy__"]["k"]["meta"]
    assert s.unverified_slots() == []


def test_an_override_geometry_with_no_session_watershed_is_a_structured_refusal(ta):
    from ai_hydro.session import HydroSession

    HydroSession("no-basin").save()
    r = _call(ta.extract_hydrological_signatures, session_id="no-basin", geometry_geojson=json.dumps(PA))
    assert r["error"] is True and r["code"] == "NO_SESSION_BASIN" and r["recovery"]
    assert "NoneType" not in json.dumps(r)
