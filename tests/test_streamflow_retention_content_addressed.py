"""Retained streamflow files are content-addressed and never overwritten (review H0).

Defect: ``persist_streamflow_slot`` wrote ``streamflow_<gauge>.json`` into the shared
workspace with a plain overwrite. A run record seals the digest of the exact file it
retained, so a second fetch of the same gauge (any window, any session in that workspace)
rewrote a file an earlier SEALED run still referenced, and capsule export then downgraded
that run's binding to "differs".

Here two fetches of one gauge with different content run through the real
``fetch_streamflow_data`` (server handler, recording middleware on); only the network is
replaced, at the USGS adapter's single transport seam, by SYNTHETIC daily values in the
API's own response shape (the labelled stand-in: the recorded fixtures cover one window per
gauge).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

import fastmcp
import mcp.types as mcp_types
import numpy as np
import pandas as pd
import pytest

from _usgs_replay import Replay, ok
from ai_hydro.session import chat_binding, store
from ai_hydro.session import run_records as rr
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.refs import resolve_ref
from ai_hydro.session.store import HydroSession

pytestmark = pytest.mark.skipif(
    int(fastmcp.__version__.split(".")[0]) >= 3,
    reason="real ai_hydro tools need the pinned FastMCP 2.x API",
)

GAUGE = "01013500"
SQUARE = {"type": "Polygon", "coordinates": [[[-77.5, 39.2], [-77.4, 39.2], [-77.4, 39.3],
                                              [-77.5, 39.3], [-77.5, 39.2]]]}
NAME_RE = re.compile(rf"streamflow_{GAUGE}_[0-9a-f]{{12}}\.json$")


def call(server, name, arguments):
    handler = server._mcp_server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call", params=mcp_types.CallToolRequestParams(name=name, arguments=arguments))
    result = asyncio.run(handler(req))
    root = result.root if hasattr(result, "root") else result
    return root.isError, json.loads(root.content[0].text)


def _daily_body(start, end):
    """SYNTHETIC daily discharge (ft^3/s) as a deterministic function of the date."""
    idx = pd.date_range(start, end, freq="D")
    cfs = 200 + 150 * np.abs(np.sin(idx.dayofyear.to_numpy() / 30.0)) + (idx.year.to_numpy() - 1990) * 3.0
    feats = [{"type": "Feature", "id": f"f{i}", "properties": {
        "time": d.strftime("%Y-%m-%d"), "value": repr(float(v)), "unit_of_measure": "ft^3/s",
        "approval_status": "Approved", "qualifier": None, "time_series_id": "ts-synthetic",
        "monitoring_location_id": f"USGS-{GAUGE}", "parameter_code": "00060", "statistic_id": "00003",
        "last_modified": "2026-10-17T00:00:00+00:00"}} for i, (d, v) in enumerate(zip(idx, cfs))]
    return {"type": "FeatureCollection", "features": feats, "numberReturned": len(feats), "links": []}


@pytest.fixture
def world(tmp_path, monkeypatch):
    from ai_hydro.mcp import app

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "home" / ".aihydro"))
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    ws = tmp_path / "workspace"                 # ONE workspace folder shared by every session
    ws.mkdir()

    def override(url, params):
        if url.endswith("/collections/daily/items"):
            start, end = params["time"].split("/")
            return ok(_daily_body(start, end))
        return None                             # site record: the recorded fixture

    from aihydro_data.sources import usgs_waterdata as uw
    monkeypatch.setattr(uw, "_http_get", Replay(override))

    def new_session(sid):
        s = HydroSession(sid)
        s.site_id, s.site_type = GAUGE, "usgs_gauge"
        s.workspace_dir = str(ws)
        s.set("watershed", {"data": {"area_km2": 250.0, "gauge_id": GAUGE},
                            "meta": {"tool": "delineate_watershed"}})
        s.save()

    def fetch(sid, start, end):
        err, body = call(app.mcp, "fetch_streamflow_data",
                         {"session_id": sid, "gauge_id": GAUGE, "start_date": start, "end_date": end})
        assert not err and not body.get("error"), body
        slot = HydroSession.load(sid).streamflow["meta"]
        return {"sid": sid, "run_id": slot["run_id"], "path": Path(slot["retained_series"]["path"]),
                "digest": slot["retained_series"]["digest"]}

    return ws, new_session, fetch


def _sealed_file(sid, run_id):
    """The retained-file entry a run's SEALED record carries, and whether the record seal holds."""
    row = HydroSession.load(sid).get("_run_log")[run_id]
    files = (row["record"].get("extra") or {}).get("retained_files") or []
    (f,) = [x for x in files if "streamflow_" in x["path"]]
    return f, rr.verify_run_log_entry(row)["record_ok"]


def _disk_digest(sid, ref):
    p = resolve_ref(ref, sid, HydroSession.load(sid).workspace_dir)
    assert p is not None and p.is_file(), f"sealed file {ref} is gone"
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def test_two_fetches_with_different_content_keep_both_sealed_runs_verifiable(world):
    ws, new_session, fetch = world
    new_session("study-a")
    a = fetch("study-a", "2000-01-01", "2000-12-31")
    new_session("study-b")                      # a second session in the SAME workspace
    b = fetch("study-b", "2000-01-01", "2001-06-30")

    assert a["path"] != b["path"], "different content must not share a file name"
    assert NAME_RE.search(a["path"].name) and NAME_RE.search(b["path"].name), (a["path"], b["path"])
    assert a["path"].is_file() and b["path"].is_file()
    assert not list(ws.glob("*.tmp-*")), "scratch files must not be left behind"

    for run in (a, b):
        sealed, record_ok = _sealed_file(run["sid"], run["run_id"])
        assert record_ok, "the sealed record itself must verify"
        assert sealed["digest"] == run["digest"]
        # the file the SEALED record points at still has the sealed bytes
        assert _disk_digest(run["sid"], sealed["path"]) == sealed["digest"], run


def test_capsule_export_after_a_refetch_reports_no_differs_for_either_session(world, tmp_path):
    import ai_hydro.mcp.tools_session as ts

    _, new_session, fetch = world
    new_session("study-a")
    fetch("study-a", "2000-01-01", "2000-12-31")
    new_session("study-b")
    fetch("study-b", "2000-01-01", "2001-06-30")

    for sid in ("study-a", "study-b"):
        out = ts.export_session(session_id=sid, capsule_path=str(tmp_path / f"capsule-{sid}"))
        assert "error" not in out, out
        cap = Path(out["capsule_dir"])
        manifest = json.loads((cap / "capsule_manifest.json").read_text())
        (art,) = manifest["data_artifacts"]
        assert art["consistency_checks"]["producer_sealed_digest"]["status"] == "agrees", (sid, art)
        assert art["binding"] == "producer_sealed" and art["status"] == "exported"
        assert "differs" not in json.dumps(art["consistency_checks"]), (sid, art["consistency_checks"])
        assert NAME_RE.search(art["retained_artifact"]["path"])
        digest = hashlib.sha256((cap / art["retained_artifact"]["path"]).read_bytes()).hexdigest()
        assert digest == art["retained_artifact"]["sha256"]


def test_the_same_series_fetched_twice_reuses_one_file(world):
    ws, new_session, fetch = world
    new_session("study-a")
    first = fetch("study-a", "2000-01-01", "2000-12-31")
    again = fetch("study-a", "2000-01-01", "2000-12-31")
    assert again["path"] == first["path"] and again["digest"] == first["digest"]
    assert len(list(ws.glob(f"streamflow_{GAUGE}_*.json"))) == 1


def test_an_existing_file_is_never_overwritten(tmp_path):
    """Direct unit check of the rename step: a name collision with different bytes escalates
    to the full digest instead of replacing the file."""
    from ai_hydro.mcp import streamflow_slot as ss

    ws = tmp_path
    scratch = ws / f"streamflow_{GAUGE}.tmp-aaaa.json"
    scratch.write_bytes(b'{"q_cms": [1, 2, 3]}')
    full = hashlib.sha256(scratch.read_bytes()).hexdigest()
    squatter = ws / f"streamflow_{GAUGE}_{full[:12]}.json"
    squatter.write_bytes(b"different bytes already holding the 12-hex name")
    out = Path(ss._content_address(str(scratch), GAUGE))
    assert out.name == f"streamflow_{GAUGE}_{full}.json" and out.read_bytes() == b'{"q_cms": [1, 2, 3]}'
    assert squatter.read_bytes() == b"different bytes already holding the 12-hex name"
    assert not scratch.exists()


def test_old_records_that_name_the_legacy_unhashed_file_still_resolve(world):
    """Backward compatibility: a slot/record written before this change points at
    ``streamflow_<gauge>.json``; consumers read by recorded path + digest, not by name."""
    ws, new_session, fetch = world
    new_session("study-a")
    run = fetch("study-a", "2000-01-01", "2000-12-31")
    legacy = ws / f"streamflow_{GAUGE}.json"
    legacy.write_bytes(run["path"].read_bytes())            # what the old writer would have left
    s = HydroSession.load("study-a")
    entry = s.streamflow
    entry["data"]["_data_file"] = str(legacy)
    entry["meta"]["retained_series"] = {"path": str(legacy), "digest": run["digest"]}
    s.set("streamflow", entry)
    s.save()

    from ai_hydro.mcp.tools_analysis import extract_hydrological_signatures
    r = extract_hydrological_signatures(session_id="study-a", start_date="2000-01-01",
                                        end_date="2000-12-31", precipitation="skip",
                                        geometry_geojson=json.dumps(SQUARE))
    assert not r.get("error"), r
    assert r["data"].get("streamflow_observation") is not None
    assert legacy.read_bytes() == run["path"].read_bytes()   # untouched
