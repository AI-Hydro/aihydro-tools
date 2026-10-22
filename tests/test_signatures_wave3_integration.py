"""Science Wave 3, PR-4b: the REAL aihydro-watershed 0.2.0 signatures output through the tools wrapper and gates.

Nothing here is a hand-written fixture of watershed's output shape: the recorded USGS Water Data API body for
01013500 WY1990-2009 (tests/fixtures/usgs_waterdata, 7,305 days, 2,100 ESTIMATED) is served through the
real ``fetch_streamflow_data`` -> ``extract_hydrological_signatures`` path (aihydro-data adapter and
aihydro-watershed 0.2.0 run for real; only the transport and the precipitation fetch are replaced, offline). The
precipitation is SYNTHETIC (a seasonal series labelled GRIDMET_PRECIP): these tests check mechanics, never
hydrology. CAMELS comparisons are against the packaged CAMELS-US v1.2 tables.

Covered: the retained series and slot health; the four auto-run validators bound to the series by digest; the
`camels_reference` block and the single `reference_comparison` location; the forcing and comparison gates on
real `_uncertainty` scope and `reference_comparison` output.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import ai_hydro.mcp  # noqa: F401  (registers every tool)
from ai_hydro.claims import status_gate
from ai_hydro.mcp.tools_analysis import extract_hydrological_signatures, fetch_streamflow_data
from ai_hydro.mcp.tools_ledger import add_claim, update_claim_status
from ai_hydro.mcp.tools_validators import (
    check_record_length, check_regulated_basin, check_stationarity, check_usgs_qualification_codes,
)
from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

from _usgs_replay import Replay

SID = "w3-pr4b-real"
GAUGE = "01013500"
START, END = "1989-10-01", "2009-09-30"
AREA_KM2 = 2253.29                       # the recorded site record's drainage area
SQUARE = json.dumps({"type": "Polygon", "coordinates": [[[-77.5, 39.2], [-77.4, 39.2], [-77.4, 39.3],
                                                         [-77.5, 39.3], [-77.5, 39.2]]]})
RAT = "Synthetic-precipitation integration fixture rationale, long enough to pass."


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    """Fetch (recorded USGS body) then signatures, once; yields the tool results and the session id."""
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("w3pr4b")
    from ai_hydro.session import chat_binding
    from aihydro_data.sources import usgs_waterdata as uw
    import aihydro_watershed.signatures.signatures as impl

    mp.setenv("HOME", str(tmp / "home"))
    mp.setenv("AIHYDRO_HOME", str(tmp / "aihydro_home"))
    mp.setattr(store, "_SESSIONS_DIR", tmp / "sessions")
    mp.setattr(store, "SESSIONS_DIR", tmp / "sessions")
    mp.setattr(chat_binding, "_store", ChatBindingStore(tmp / "chat_studies.json"))
    mp.setattr(uw, "_http_get", Replay())               # the recorded bodies only; an unrecorded request fails

    def fake_precip(geom, start_date, end_date):
        idx = pd.date_range(start_date, end_date, freq="D")
        doy = idx.dayofyear.to_numpy()
        p = pd.Series(2.6 * (1.0 + 0.4 * np.sin(2 * np.pi * doy / 365.25)), index=idx)
        p.attrs["product"] = "GRIDMET_PRECIP"
        impl._PRECIP_DIAG.d = {"product": "GRIDMET_PRECIP"}
        return p

    mp.setattr(impl, "_fetch_precipitation_data_bygeom", fake_precip)
    (tmp / "sessions").mkdir()
    s = HydroSession(SID)                                # no workspace: the series goes to the session data dir
    s.site_id, s.site_type = GAUGE, "usgs_gauge"
    # geometry in the watershed slot (not geometry_geojson=): an explicit geometry is stored under the
    # "__override__" feature, which session.get("signatures") (the validators' lookup) does not read.
    s.set("watershed", {"data": {"area_km2": AREA_KM2, "gauge_id": GAUGE, "geometry_geojson": json.loads(SQUARE)},
                        "meta": {"tool": "delineate_watershed"}})
    s.save()
    fetched = fetch_streamflow_data(session_id=SID, gauge_id=GAUGE, start_date=START, end_date=END)
    assert not fetched.get("error"), fetched
    sigs = extract_hydrological_signatures(session_id=SID, start_date=START, end_date=END)
    assert not sigs.get("error"), sigs
    try:
        yield {"sid": SID, "fetched": fetched, "sigs": sigs, "dir": tmp}
    finally:
        mp.undo()


def _row(real):
    return HydroSession.load(SID).get("_run_log")[real["sigs"]["_run_id"]]


# ------------------------------------------------------------------ retained series and slot health

def test_served_series_is_a_digest_bound_session_file_and_is_not_in_the_result(real):
    from aihydro_core.records import digest_bytes
    d, meta = real["sigs"]["data"], real["sigs"]["meta"]
    assert "_served_series" not in d
    ret = meta["retained_series"]
    assert set(ret) == {"path", "digest"} and d["_data_file"] == ret["path"]
    assert digest_bytes(Path(ret["path"]).read_bytes()) == ret["digest"]
    body = json.loads(Path(ret["path"]).read_text())
    assert len(body["dates"]) == len(body["q_cms"]) == 7305 and body["dates"][0] == START
    assert sum(1 for c in body["qualifier_codes"] if "e" in c.split(",")) == 2100       # the ESTIMATED days
    # the series itself is the one the signatures digested
    from ai_hydro.validators.series_binding import series_digest
    assert series_digest(body["dates"], body["q_cms"]) == d["streamflow_digest"]
    # nothing array-sized reaches the sealed row or the cached slot
    assert "_served_series" not in json.dumps(_row(real)["key_outputs"])
    assert "_served_series" not in json.dumps(HydroSession.load(SID).get("signatures"))


def test_slot_health_reads_the_signatures_slot_as_ok(real):
    out = HydroSession.load(SID).summary()
    assert out["slot_health"]["signatures"] == "ok", out.get("degraded")
    assert "signatures" in out["computed"] and not [x for x in out.get("degraded", []) if x["slot"] == "signatures"]


def test_retained_file_is_listed_in_the_sealed_record_for_the_capsule(real):
    """Through the server (recording middleware on) so the run gets a sealed record."""
    import asyncio
    import fastmcp
    from ai_hydro.mcp import app

    sid = "w3-pr4b-sealed"
    s = HydroSession(sid)
    s.site_id, s.site_type = GAUGE, "usgs_gauge"
    s.set("watershed", {"data": {"area_km2": AREA_KM2, "gauge_id": GAUGE, "geometry_geojson": json.loads(SQUARE)},
                        "meta": {"tool": "delineate_watershed"}})
    s.save()

    def call(name, args):
        async def go():
            async with fastmcp.Client(app.mcp) as client:
                res = await client.call_tool(name, args, raise_on_error=False)
                return res.is_error, json.loads(res.content[0].text)
        return asyncio.run(go())

    err, f = call("fetch_streamflow_data", {"session_id": sid, "gauge_id": GAUGE, "start_date": START, "end_date": END})
    assert not err and not f.get("error"), f
    err, body = call("extract_hydrological_signatures", {"session_id": sid, "start_date": START, "end_date": END})
    assert not err and not body.get("error"), body
    rec = HydroSession.load(sid).get("_run_log")[body["_run_id"]]["record"]
    ret = body["meta"]["retained_series"]
    files = (rec.get("extra") or {}).get("retained_files") or []
    assert any(f["digest"] == ret["digest"] and f["role"] == "artifact" for f in files), files
    assert not any(f["path"].startswith("/") for f in files)               # a ref, never an absolute path
    # the signatures consumed the slot (edge declared) and the retained series carries the slot's USGS codes
    assert rec["parents"] and body["data"]["_data_file"] == ret["path"]


# ------------------------------------------------------------------ validators run automatically, on that series

def test_four_validators_run_after_signatures_and_judge_the_signatures_series(real):
    flags = {f["validator"]: f for f in real["sigs"]["quality_flags"] if isinstance(f, dict)}
    for name in ("record_length", "usgs_qualification_codes", "stationarity", "regulated_basin"):
        assert name in flags, sorted(flags)
    digest = real["sigs"]["data"]["streamflow_digest"]
    for name in ("record_length", "usgs_qualification_codes", "stationarity"):
        series = flags[name]["metadata"]["series"]
        assert series["source"] == "signatures" and series["series_digest"] == digest
        assert series["start"] == START and series["end"] == END and series["n_days"] == 7305
        assert "signatures streamflow_digest" in series["verified_against"]
    assert flags["record_length"]["status"] == "pass"                      # 7305 days, WY1990-2009
    assert flags["stationarity"]["metadata"]["first_water_year"] == 1990   # core water_year: labelled by end year
    assert flags["stationarity"]["metadata"]["last_water_year"] == 2009
    # 2,100 estimated days must surface as a warning (never a pass) on the real series
    q = flags["usgs_qualification_codes"]
    assert q["status"] == "warning" and q["metadata"]["qualifier_counts"].get("e") == 2100
    # the quality flags are also in the sealed run row
    names = {x["validator"] for x in _row(real)["key_outputs"]["_quality_flags"]}
    assert {"record_length", "usgs_qualification_codes", "stationarity", "regulated_basin"} <= names


def test_standalone_validators_resolve_the_same_series_by_source_and_digest(real):
    digest = real["sigs"]["data"]["streamflow_digest"]
    for fn in (check_record_length, check_usgs_qualification_codes, check_stationarity):
        r = fn(SID, source="signatures", series_digest=digest)
        assert r["status"] != "insufficient_data", r
        assert r["metadata"]["series"]["series_digest"] == digest
        assert "caller series_digest" in r["metadata"]["series"]["verified_against"]


def test_a_streamflow_slot_for_another_period_is_a_series_mismatch_never_validated(real):
    """Session whose streamflow slot is 2000-2020 while the signatures used 1989-2009."""
    digest = real["sigs"]["data"]["streamflow_digest"]
    from aihydro_core.records import digest_bytes
    s = HydroSession.load(SID)
    original = s.streamflow
    idx = pd.date_range("2000-10-01", "2020-09-30", freq="D")
    other = {"dates": [f"{d:%Y-%m-%d}" for d in idx], "q_cms": [1.0 + (i % 7) for i in range(len(idx))],
             "units": "m3/s", "n_days": len(idx), "gauge_id": GAUGE}
    path = store.write_session_data_file(SID, "streamflow_other_period.json", other)   # the session JSON is lean
    s.set("streamflow", {"data": {**{k: v for k, v in other.items() if k not in ("dates", "q_cms")},
                                  "_data_file": path},
                         "meta": {"tool": "fetch_streamflow_data",
                                  "retained_series": {"path": path, "digest": digest_bytes(Path(path).read_bytes())},
                                  "params": {"gauge_id": GAUGE, "start_date": "2000-10-01", "end_date": "2020-09-30"}}})
    s.save()
    try:
        for fn in (check_record_length, check_usgs_qualification_codes, check_stationarity):
            r = fn(SID, source="streamflow", series_digest=digest)
            assert r["status"] == "insufficient_data", (fn.__name__, r)
            assert r["metadata"]["quality_flag"] == "series_mismatch" and r["metadata"]["series_mismatch"] is True
        # ... while the signatures-bound validators are untouched by the slot and judge 1989-2009
        r = check_record_length(SID, source="signatures")
        assert r["status"] == "pass" and r["metadata"]["series"]["start"] == START
        # the default (no digest asked for) still validates the slot, as before
        assert check_record_length(SID)["metadata"]["n_days"] == len(idx)
    finally:
        s = HydroSession.load(SID)
        s.set("streamflow", original)
        s.save()


# ------------------------------------------------------------------ the CAMELS reference

def test_camels_reference_block_and_the_single_comparison_location(real):
    d = real["sigs"]["data"]
    blk = d["camels_reference"]
    assert blk["available"] and blk["reference"] == "CAMELS-US v1.2" and blk["gauge_id"] == GAUGE
    src = blk["source"]
    assert src["kind"] == "packaged" and src["license"] == "CC-BY-4.0" and src["hash"].startswith("sha256:")
    assert src["files"]["camels_hydro.txt"]["md5"] == "55ebdeb36c42ee7acdb998229c3edb3a"
    assert blk["period"]["used"] == [START, END] and blk["period"]["ref"] == ["1989-10-01", "2009-09-30"]
    assert blk["period"]["differs"] is False
    assert blk["area"]["ref_km2"] == pytest.approx(2252.7) and blk["area"]["differs"] is False
    assert blk["climate_context"]["frac_snow"] == pytest.approx(0.313440357191799)
    assert blk["climate_context"]["aridity"] == pytest.approx(0.63055865946247)
    assert blk["climate_context"]["p_seasonality"] == pytest.approx(0.187940258706929)
    assert blk["forcing"]["precipitation_product"] == "GRIDMET_PRECIP" and blk["forcing"]["differs"] is True
    assert "Daymet" in blk["forcing"]["disclosure"] and "GRIDMET_PRECIP" in blk["forcing"]["disclosure"]
    # the table lives in ONE place: data.reference_comparison -> run row key_outputs.reference_comparison
    rc = d["reference_comparison"]
    assert set(rc) == {"table", "context", "climate_context"}
    assert status_gate.reference_comparison(_row(real)) == rc
    assert "reference_comparison" not in json.dumps({k: v for k, v in blk.items() if k != "table_location"})
    t = rc["table"]
    # real watershed values vs the real CAMELS file row for this gauge
    assert t["slope_fdc"]["reference"] == pytest.approx(1.52821853538976)
    assert t["slope_fdc"]["definition_comparable"] is True and t["baseflow_index"]["definition_comparable"] is True
    assert abs(t["baseflow_index"]["rel_diff"]) < 1e-5            # Ladson kernel reproduces the file
    assert abs(t["slope_fdc"]["rel_diff"]) < 1e-5                  # CAMELS-file slope definition
    assert t["slope_fdc"]["can_claim_difference"] is True and t["baseflow_index"]["can_claim_difference"] is True
    # forcing-dependent rows cannot carry a difference claim under another forcing
    for name in ("runoff_ratio", "stream_elas"):
        assert t[name]["can_claim_difference"] is False and "forcing_differs" in t[name]["blockers"]


def test_session_camels_slot_overrides_the_packaged_table_and_both_hashes_are_recorded(real):
    from ai_hydro.science.camels_reference import build_camels_reference
    s = HydroSession.load(SID)
    packaged, _ = build_camels_reference(real["sigs"]["data"], GAUGE, AREA_KM2, START, END, s)
    attrs = {"q_mean": 1.5, "slope_fdc": 1.5, "baseflow_index": 0.5, "area_gages2": 2252.7,
             "frac_snow": 0.25, "aridity": 0.7, "p_seasonality": 0.2}
    s.set("camels", {"data": {"gauge_id": GAUGE, "in_camels": True, "attributes": attrs},
                     "meta": {"tool": "fetch_camels_us", "source": "CAMELS-US via pygeohydro.get_camels()"}})
    blk, rc = build_camels_reference(real["sigs"]["data"], GAUGE, AREA_KM2, START, END, s)
    assert blk["source"]["kind"] == "session_slot" and blk["source"]["hash"].startswith("sha256:")
    assert blk["overrode"] == {"kind": "packaged", "hash": packaged["source"]["hash"]}
    assert rc["table"]["q_mean"]["reference"] == 1.5 and blk["climate_context"]["frac_snow"] == 0.25
    # a slot for ANOTHER gauge is not used: the packaged table answers and says why the slot was ignored
    s.set("camels", {"data": {"gauge_id": "01031500", "in_camels": True, "attributes": attrs}, "meta": {}})
    blk2, _ = build_camels_reference(real["sigs"]["data"], GAUGE, AREA_KM2, START, END, s)
    assert blk2["source"]["kind"] == "packaged" and "01031500" in blk2["session_slot_ignored"]


def test_unavailable_reference_says_so_and_never_raises(real):
    from ai_hydro.science.camels_reference import build_camels_reference
    blk, rc = build_camels_reference(real["sigs"]["data"], "09999999", AREA_KM2, START, END, None)
    assert blk["available"] is False and "not one of the CAMELS-US v1.2 basins" in blk["reason"] and rc is None
    blk, rc = build_camels_reference(real["sigs"]["data"], None, AREA_KM2, START, END, None)
    assert blk["available"] is False and "station number" in blk["reason"] and rc is None


# ------------------------------------------------------------------ gates on real scope + real comparison output

def _claim(real, cid, metric, text, **over):
    spans = [{"source_type": "run", "source_id": real["sigs"]["_run_id"], "metric_ref": metric}]
    r = add_claim(session_id=SID, claim_id=cid, statement=text, claim_type="empirical_result", status="proposed",
                  confidence="medium", confidence_rationale=RAT, basins=[GAUGE], period="WY1990-2009",
                  evidence_spans=spans, **over)
    assert r["status"] == "recorded", r
    return r


def _support(cid):
    return update_claim_status(SID, cid, "supported", "medium", RAT, uncertainty_verified=True)


def test_real_ci_scope_survives_into_the_sealed_run_row(real):
    ci = _row(real)["evidence"]["uncertainty"]["baseflow_index"]
    assert ci["method"] == "bootstrap_group" and "block_size" not in ci and ci["n_groups"] == 20
    assert ci["scope"]["varies"] == ["water_year_sample"]
    assert "forcing_product" in ci["scope"]["excludes"] and "exchangeable_years" in ci["scope"]["assumes"]


def test_plain_claim_citing_a_run_with_a_comparison_block_is_accepted(real):
    _claim(real, "plain", "baseflow_index", "The baseflow index over WY1990-2009 was 0.585.")
    assert _support("plain")["status"] == "updated"


def test_comparison_claim_with_a_forcing_excluded_ci_is_refused(real):
    _claim(real, "cmp_ci", "baseflow_index", "The baseflow index agrees with the CAMELS value.",
           comparison_ref="CAMELS-US v1.2")
    res = _support("cmp_ci")
    assert res["error"] == "forcing_scope_gate", res
    assert res["metrics"][0]["metric_ref"] == "baseflow_index" and "_traceback" not in res
    assert HydroSession.load(SID).claims["cmp_ci"]["status"] == "proposed"


def test_can_claim_difference_false_refuses_a_comparison_claim(real):
    assert _row(real)["key_outputs"]["reference_comparison"]["table"]["runoff_ratio"]["can_claim_difference"] is False
    _claim(real, "cmp_rr", "runoff_ratio", "The runoff ratio is higher than the CAMELS reference value.",
           comparison_ref="CAMELS-US v1.2")
    res = _support("cmp_rr")
    assert res["error"] == "comparison_gate" and "can_claim_difference=false" in res["teaching_error"]["explanation"]
    # ... and the same metric stated plainly is not affected by the flag
    _claim(real, "plain_rr", "runoff_ratio", "The runoff ratio over the paired water years was computed.")
    assert _support("plain_rr")["status"] == "updated"


def test_comparison_claim_on_a_comparable_metric_without_a_ci_passes_the_structural_gates(real):
    """slope_fdc has no interval, reproduces the CAMELS file and shares its period/definition/area: no gate fires."""
    assert real["sigs"]["data"]["reference_comparison"]["table"]["slope_fdc"]["can_claim_difference"] is True
    _claim(real, "cmp_slope", "slope_fdc", "slope_fdc agrees with the CAMELS-US v1.2 value.",
           comparison_ref="CAMELS-US v1.2")
    assert _support("cmp_slope")["status"] == "updated"


def test_gate_reads_exactly_the_documented_location(real):
    rc = _row(real)["key_outputs"]["reference_comparison"]
    row = {"key_outputs": {"x": 1}, "evidence": {"data": {"reference_comparison": rc}}}
    assert status_gate.reference_comparison(row) is None          # not searched for elsewhere
    assert status_gate.reference_comparison({"key_outputs": {"nested": {"reference_comparison": rc}}}) is None
    assert status_gate.reference_comparison({"key_outputs": {"reference_comparison": rc}}) == rc


def test_regulated_basin_binds_to_the_basin_of_the_series(real):
    r = check_regulated_basin(SID, source="signatures")
    assert r["metadata"]["series"]["series_digest"] == real["sigs"]["data"]["streamflow_digest"]
    s = HydroSession.load(SID)
    s.set("watershed", {"data": {"area_km2": 1.0, "gauge_id": "01109000"}, "meta": {}})
    s.save()
    try:
        r = check_regulated_basin(SID, source="signatures")
        assert r["status"] == "insufficient_data" and r["metadata"]["series_mismatch"] is True
    finally:
        s = HydroSession.load(SID)
        s.set("watershed", {"data": {"area_km2": AREA_KM2, "gauge_id": GAUGE}, "meta": {"tool": "delineate_watershed"}})
        s.save()
