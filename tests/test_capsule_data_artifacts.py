"""
Capsule data/ must retain the served streamflow series (defect D2 of
e2e-proof-1): the session slot strips long arrays, so without this a reader
outside the platform cannot recompute a claim from the capsule.

Session is built through the public tools (real ``fetch_streamflow_data`` with a
stubbed aihydro-data backend, real ``extract_hydrological_signatures`` tool with
the real CAMELS flow-stats code), then exported. The independent check is a
stdlib Lyne-Hollick written here, not the capsule module's.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import fastmcp
import mcp.types as mcp_types
import pytest

from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

pytestmark = pytest.mark.skipif(
    int(fastmcp.__version__.split(".")[0]) >= 3,
    reason="real ai_hydro tools need the pinned FastMCP 2.x API",
)

SID = "capsule-data-session"
GAUGE = "01013500"
START, END = "1999-10-01", "2001-09-30"
SQUARE = json.dumps({"type": "Polygon", "coordinates": [[[-77.5, 39.2], [-77.4, 39.2], [-77.4, 39.3],
                                                         [-77.5, 39.3], [-77.5, 39.2]]]})


def call(server, name, arguments):
    handler = server._mcp_server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call", params=mcp_types.CallToolRequestParams(name=name, arguments=arguments))
    result = asyncio.run(handler(req))
    root = result.root if hasattr(result, "root") else result
    return root.isError, json.loads(root.content[0].text)


def _series():
    import pandas as pd

    dates = pd.date_range(START, END, freq="D")
    q = [5.0 + 4.0 * math.sin(i / 23.0) ** 2 + (30.0 if i % 97 == 0 else 0.0) + 0.001 * (i % 7)
         for i in range(len(dates))]
    q[40] = float("nan")  # one missing day must round-trip as an empty cell
    return pd.DataFrame({"date": dates, "streamflow": q})


def stdlib_lyne_hollick_bfi(q, a=0.925, passes=3):
    """Independent stdlib implementation (same published algorithm)."""
    def sweep(y, fwd):
        n = len(y)
        f = [0.0] * n
        b = list(y)
        idx = range(1, n) if fwd else range(n - 2, -1, -1)
        prev = 0 if fwd else n - 1
        for t in idx:
            f[t] = a * f[prev] + (1 + a) / 2 * (y[t] - y[prev])
            b[t] = y[t] - f[t] if f[t] > 0 else y[t]
            b[t] = min(max(b[t], 0.0), y[t])
            prev = t
        return b
    bf = list(q)
    for i in range(passes):
        bf = sweep(bf, i % 2 == 0)
    return sum(min(max(x, 0.0), v) for x, v in zip(bf, q)) / sum(q)


@pytest.fixture(params=["refetch", "retained"])
def exported(request, tmp_path, monkeypatch):
    return _build_and_export(request.param, tmp_path, monkeypatch)


def _build_and_export(mode, tmp_path, monkeypatch):
    import aihydro_data
    from ai_hydro.mcp import app
    from ai_hydro.session import chat_binding

    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    s = HydroSession(SID)
    s.set("watershed", {"data": {"area_km2": 250.0, "gauge_id": GAUGE, "geometry_geojson": json.loads(SQUARE)}, "meta": {"tool": "delineate_watershed"}})
    s.site_id, s.site_type = GAUGE, "usgs_gauge"
    s.save()

    df = _series()
    calls = []

    def stub_fetch(variable, geometry, start, end, **kw):
        calls.append((variable, geometry, start, end))
        return SimpleNamespace(data=df, product="NWIS_STREAMFLOW", source="direct_api",
                               citation="USGS NWIS (stub)", cache_hit=True,
                               fetched_at="2026-10-02T00:00:00+00:00", cache_key="stubkey")

    monkeypatch.setattr(aihydro_data, "fetch", stub_fetch)

    def real_signatures(**kwargs):
        import pandas as pd
        from aihydro_watershed.signatures.signatures import compute_flow_stats_camels

        q = pd.Series(list(kwargs["q_cms_series"]), dtype=float)
        return {"data": compute_flow_stats_camels(q), "meta": {"tool": "extract_hydrological_signatures"}}

    monkeypatch.setattr("ai_hydro.analysis.signatures.extract_hydrological_signatures", real_signatures)

    err, body = call(app.mcp, "fetch_streamflow_data",
                     {"session_id": SID, "gauge_id": GAUGE, "start_date": START, "end_date": END})
    assert not err and not body.get("error"), body
    assert calls, "stub backend was not used"
    # The slot really lost the series: this is the defect being fixed.
    assert not HydroSession.load(SID).streamflow["data"].get("q_cms")

    # The real fetch tool retains the served series beside the session file and
    # seals its digest into its own record (extra.retained_files).
    slot_meta = HydroSession.load(SID).streamflow["meta"]
    retained_path = Path(slot_meta["retained_series"]["path"])
    assert retained_path.is_file()
    if mode == "retained_swapped":
        # the retained file is replaced after the fetch sealed its digest
        body = json.loads(retained_path.read_text())
        body["q_cms"] = [None if v is None else v * 1.5 for v in body["q_cms"]]
        retained_path.write_text(json.dumps(body))

    # signatures must read the series via the slot; with it stripped and no
    # workspace file the tool would refetch, so hand the real series through
    # the stub by calling the tool (it falls back to q_cms_series=None -> real
    # function receives None). Provide it explicitly instead:
    def sigs_with_series(**kwargs):
        kwargs["q_cms_series"] = [v for v in df["streamflow"].tolist()]
        return real_signatures(**kwargs)

    monkeypatch.setattr("ai_hydro.analysis.signatures.extract_hydrological_signatures", sigs_with_series)
    # The tool consumes the slot only for the exact gauge and period fetched.
    err, body = call(app.mcp, "extract_hydrological_signatures",
                     {"session_id": SID, "start_date": START, "end_date": END})
    assert not err and not body.get("error"), body

    if mode.startswith("refetch"):
        # A session fetched before fetch-time retention: the slot has no
        # retained file, so export can only re-query the recorded request.
        slot = HydroSession.load(SID)
        entry = slot.streamflow
        entry["data"].pop("_data_file", None)
        entry["meta"].pop("retained_series", None)
        slot.set("streamflow", entry)
        slot.save()
        assert not HydroSession.load(SID).streamflow["data"].get("_data_file")

    if mode.startswith("refetch_diff"):
        other = df.copy()
        if mode == "refetch_diff_data":
            other["streamflow"] = [v * (1.0 + 0.5 * ((i // 30) % 2)) for i, v in enumerate(other["streamflow"])]
        monkeypatch.setattr(aihydro_data, "fetch", lambda *a, **k: SimpleNamespace(
            data=other, product="OTHER_PRODUCT" if mode == "refetch_diff_product" else "NWIS_STREAMFLOW",
            source="direct_api", citation="x", cache_hit=False,
            fetched_at="2026-10-03T00:00:00+00:00", cache_key="k2"))

    import ai_hydro.mcp.tools_session as ts

    result = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "capsule"))
    assert "error" not in result, result
    return Path(result["capsule_dir"]), result, df


def _read_series(path: Path):
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    return rows


def test_series_csv_in_capsule_with_manifest_digest(exported):
    cap, result, df = exported
    csv_path = cap / "data" / f"served_streamflow_{GAUGE}.csv"
    assert csv_path.is_file()
    manifest = json.loads((cap / "capsule_manifest.json").read_text())

    sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    in_files = {f["path"]: f["sha256"] for f in manifest["files"]}
    assert in_files[f"data/served_streamflow_{GAUGE}.csv"] == sha

    (art,) = manifest["data_artifacts"]
    assert art["path"] == f"data/served_streamflow_{GAUGE}.csv" and art["sha256"] == sha
    assert art["role"] == "served_data" and art["columns"] == ["date", "q_cms"]
    assert art["n_rows"] == len(df) and art["n_missing"] == 1
    slot_run = HydroSession.load(SID).streamflow["meta"]["run_id"]
    run_log = json.loads((cap / "run_log.json").read_text())
    if "retained_artifact" in art:
        assert art["produced_by_run_id"] == slot_run
        assert art["producer_record_digest"] == run_log[slot_run]["record"]["record_digest"]
        assert art["binding"] == "producer_sealed" and art["status"] == "exported"
        assert art["consistency_checks"]["producer_sealed_digest"]["status"] == "agrees"
    else:
        # a re-query is never attributed to the run that was merely its trigger
        assert art["produced_by_run_id"] is None and art["requested_by_run_id"] == slot_run
        assert "producer_record_digest" not in art
        assert art["binding"] == "self_attested" and art["status"] == "exported_from_cache"
        assert "re-queried at export; may differ" in (cap / "README.md").read_text()
        assert art["consistency_checks"]["product_vs_slot"]["status"] == "agrees"
    if "retained_artifact" in art:
        assert art["retrieval"]["mechanism"] == "retained_data_file"
        ra = art["retained_artifact"]
        assert ra["path"] == f"data/streamflow_{GAUGE}.json"
        assert hashlib.sha256((cap / ra["path"]).read_bytes()).hexdigest() == ra["sha256"]
        assert in_files[ra["path"]] == ra["sha256"]
        # the consumer's recorded series digest matches the exported series
        assert art["consistency_checks"]["served_series_digest"]["status"] == "agrees"
    else:
        assert art["retrieval"]["mechanism"] == "aihydro_data_refetch" and art["retrieval"]["cache_hit"] is True
    assert art["consistency_checks"]["n_rows_vs_recorded_n_days"]["status"] == "agrees"


def test_independent_stdlib_bfi_matches_tool_bfi(exported):
    cap, result, _ = exported
    rows = _read_series(cap / "data" / f"served_streamflow_{GAUGE}.csv")
    q = [float(r["q_cms"]) for r in rows if r["q_cms"] != ""]
    reported = HydroSession.load(SID).get("signatures")["data"]["baseflow_index"]
    assert abs(stdlib_lyne_hollick_bfi(q) - reported) < 1e-9
    chk = json.loads((cap / "capsule_manifest.json").read_text())["data_artifacts"][0]["consistency_checks"]
    assert chk["baseflow_index"]["status"] == "agrees"


def test_replay_status_makes_no_recomputation_claim(exported):
    cap, result, _ = exported
    assert result["replay_status"] == "archive_integrity" and result["recomputation"] == "not_performed"
    proc = subprocess.run([sys.executable, str(cap / "replay.py")], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "recomputed" not in proc.stdout.lower().replace("not recomputed", "")


def test_tampered_series_fails_replay(exported):
    cap, _, _ = exported
    p = cap / "data" / f"served_streamflow_{GAUGE}.csv"
    p.write_text(p.read_text().replace("\n", "\n9", 1))
    proc = subprocess.run([sys.executable, str(cap / "replay.py")], capture_output=True, text=True)
    assert proc.returncode != 0


def test_unobtainable_series_is_stated_not_omitted(tmp_path, monkeypatch):
    import aihydro_data
    import ai_hydro.mcp.tools_session as ts

    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()

    def boom(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(aihydro_data, "fetch", boom)
    s = HydroSession("nodata")
    s.set("streamflow", {"data": {"n_days": 800}, "meta": {
        "tool": "fetch_streamflow_data",
        "params": {"gauge_id": GAUGE, "start_date": START, "end_date": END}}})
    s.save()
    result = ts.export_session(session_id="nodata", capsule_path=str(tmp_path / "cap"))
    (art,) = result["data_artifacts"]
    assert art["status"] == "unavailable" and "path" not in art
    assert not list((Path(result["capsule_dir"]) / "data").glob("served_*"))
    assert "unavailable" in (Path(result["capsule_dir"]) / "README.md").read_text()


def test_replay_binds_retained_series_to_the_sealed_record(tmp_path, monkeypatch):
    cap, _, _ = _build_and_export("retained", tmp_path, monkeypatch)
    proc = subprocess.run([sys.executable, str(cap / "replay.py")], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout
    assert "matches the digest sealed by" in proc.stdout and "equals the retained series" in proc.stdout


def test_swapped_series_in_capsule_fails_replay_even_with_regenerated_manifest(tmp_path, monkeypatch):
    from ai_hydro.capsule.manifest import build_manifest

    cap, _, _ = _build_and_export("retained", tmp_path, monkeypatch)
    ra = cap / "data" / f"streamflow_{GAUGE}.json"
    body = json.loads(ra.read_text())
    body["q_cms"] = [None if v is None else v * 2 for v in body["q_cms"]]
    ra.write_text(json.dumps(body))
    # swap the derived CSV too and regenerate the manifest, as a same-user attacker would
    csv_p = cap / "data" / f"served_streamflow_{GAUGE}.csv"
    lines = csv_p.read_text().splitlines()
    csv_p.write_text("\n".join([lines[0]] + [
        f"{ln.split(',')[0]},{'' if ln.split(',')[1] == '' else repr(float(ln.split(',')[1]) * 2)}"
        for ln in lines[1:]]) + "\n")
    (cap / "capsule_manifest.json").write_text(json.dumps(build_manifest(cap)))
    proc = subprocess.run([sys.executable, str(cap / "replay.py")], capture_output=True, text=True)
    assert proc.returncode == 1 and "differs from the digest sealed by" in proc.stdout


def test_series_swapped_before_export_is_flagged_not_bound(tmp_path, monkeypatch):
    cap, _, _ = _build_and_export("retained_swapped", tmp_path, monkeypatch)
    (art,) = json.loads((cap / "capsule_manifest.json").read_text())["data_artifacts"]
    assert art["status"] == "exported_with_inconsistency" and art["binding"] == "self_attested"
    assert art["consistency_checks"]["producer_sealed_digest"]["status"] == "differs"
    proc = subprocess.run([sys.executable, str(cap / "replay.py")], capture_output=True, text=True)
    assert proc.returncode == 1 and "differs from the digest sealed by" in proc.stdout


def test_refetch_that_differs_from_what_the_run_saw_is_inconsistent(tmp_path, monkeypatch):
    cap, _, _ = _build_and_export("refetch_diff_data", tmp_path, monkeypatch)
    (art,) = json.loads((cap / "capsule_manifest.json").read_text())["data_artifacts"]
    assert art["status"] == "exported_with_inconsistency"
    assert art["produced_by_run_id"] is None and art["requested_by_run_id"]
    assert art["consistency_checks"]["baseflow_index"]["status"] == "differs"
    assert art["retrieval"]["cache_hit"] is False
    assert "re-queried at export; may differ" in (cap / "README.md").read_text()


def test_refetch_from_a_different_product_is_flagged(tmp_path, monkeypatch):
    cap, _, _ = _build_and_export("refetch_diff_product", tmp_path, monkeypatch)
    (art,) = json.loads((cap / "capsule_manifest.json").read_text())["data_artifacts"]
    assert art["consistency_checks"]["product_vs_slot"]["status"] == "differs"
    assert art["status"] == "exported_with_inconsistency"


def test_manifest_has_no_absolute_capsule_dir(exported):
    cap, _, _ = exported
    assert "capsule_dir" not in json.loads((cap / "capsule_manifest.json").read_text())


def test_listed_sealed_retained_file_that_is_missing_fails_replay(tmp_path, monkeypatch):
    cap, _, _ = _build_and_export("retained", tmp_path, monkeypatch)
    (cap / "data" / f"streamflow_{GAUGE}.json").unlink()
    proc = subprocess.run([sys.executable, str(cap / "replay.py")], capture_output=True, text=True)
    assert proc.returncode == 1
    assert "sealed by" in proc.stdout and "listed in the manifest, but missing" in proc.stdout


def test_sealed_file_the_manifest_never_listed_is_only_a_note(tmp_path, monkeypatch):
    from ai_hydro.capsule.manifest import build_manifest

    cap, _, _ = _build_and_export("retained", tmp_path, monkeypatch)
    for name in ("bundle.json", "ro-crate-metadata.json", "manifest-sha256.txt"):
        (cap / name).unlink()           # models a capsule that predates the crate (the bundle pins file digests)
    (cap / "data" / f"streamflow_{GAUGE}.json").unlink()
    (cap / "data" / f"served_streamflow_{GAUGE}.csv").unlink()
    (cap / "capsule_manifest.json").write_text(json.dumps(build_manifest(cap)))  # lists neither
    proc = subprocess.run([sys.executable, str(cap / "replay.py")], capture_output=True, text=True)
    assert "but not in this capsule" in proc.stdout
    assert "listed in the manifest, but missing" not in proc.stdout
    assert proc.returncode == 0, proc.stdout
