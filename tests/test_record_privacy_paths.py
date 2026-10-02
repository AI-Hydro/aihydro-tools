"""Sealed records and capsules carry no absolute local paths (privacy).

A retained file is referenced by a location-independent ref
(``session-data:<name>`` / ``workspace:<rel>``); the digest stays its identity.
Full real-tool session (mocked NWIS): fetch -> signatures -> export.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ai_hydro.session import refs
from ai_hydro.session.store import HydroSession

from test_run_record_lineage_real_tools import (  # noqa: F401  (fixtures/helpers)
    END, GAUGE, SID, SQUARE, START, _call, _rows, world,
)

_ABS = re.compile(r"(?<![\w:/.-])(/(?:Users|home|private|var|tmp|root|opt)/[^\s\"',]*|[A-Za-z]:\\\\[^\s\"',]*)")


def _leaks(text: str, *needles: str) -> list[str]:
    found = [n for n in needles if n and n in text]
    return found + _ABS.findall(text)


def test_to_ref_resolve_ref_roundtrip(tmp_path, monkeypatch):
    from ai_hydro.session import store
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    f = tmp_path / "sid.data.streamflow_1.json"
    assert refs.to_ref(f) == "session-data:sid.data.streamflow_1.json"
    assert refs.resolve_ref(refs.to_ref(f)) == f
    ws = tmp_path / "ws"
    assert refs.to_ref(ws / "a" / "x.json", ws) == "workspace:a/x.json"
    assert refs.resolve_ref("workspace:a/x.json", workspace_dir=ws) == (ws / "a" / "x.json").resolve()
    assert refs.resolve_ref("workspace:../../etc/passwd", workspace_dir=ws) is None
    assert refs.resolve_ref("/legacy/abs/path.json") == Path("/legacy/abs/path.json")  # legacy still resolves
    assert refs.ref_name("session-data:sid.data.streamflow_1.json") == "streamflow_1.json"
    assert refs.ref_name(f) == "streamflow_1.json"
    assert refs.ref_name("workspace:a/x.json") == "x.json"


def test_run_log_and_capsule_contain_no_absolute_paths(world, tmp_path):
    server, _ = world
    is_err, fetched = _call(server, "fetch_streamflow_data",
                            {"session_id": SID, "gauge_id": GAUGE, "start_date": START, "end_date": END})
    assert not is_err and not fetched.get("error"), fetched
    is_err, sigs = _call(server, "extract_hydrological_signatures",
                         {"session_id": SID, "start_date": START, "end_date": END,
                          "geometry_geojson": SQUARE})
    assert not is_err and not sigs.get("error"), sigs

    rows = _rows()
    log_text = json.dumps({k: v.get("record") for k, v in rows.items()}, default=str)
    home = str(tmp_path)
    assert not _leaks(log_text, home), _leaks(log_text, home)

    fetch_rec = next(v["record"] for v in rows.values() if (v.get("record") or {}).get("tool") == "fetch_streamflow_data")
    (rf,) = fetch_rec["extra"]["retained_files"]
    assert rf["path"].startswith("session-data:") and rf["digest"].startswith("sha256:")
    sig_rec = rows[sigs["_run_id"]]["record"]
    assert any(r["ref"].startswith("session-data:") for r in sig_rec["input_refs"])

    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "capsule"))
    assert "error" not in res, res
    cap = Path(res["capsule_dir"])
    bad = {}
    for f in cap.rglob("*"):
        if f.is_file() and f.suffix in {".json", ".md", ".py", ".txt", ".csv", ".yaml", ".yml"}:
            hits = _leaks(f.read_text(errors="replace"), home)
            if hits:
                bad[str(f.relative_to(cap))] = hits[:3]
    assert not bad, bad

    # Retained file still bound to its sealed digest by name.
    manifest = json.loads((cap / "capsule_manifest.json").read_text())
    assert manifest["data_artifacts"][0]["binding"] == "producer_sealed"


def test_error_text_paths_are_scrubbed_before_sealing(tmp_path, monkeypatch):
    from ai_hydro.session import store
    from ai_hydro.session.run_records import _minimal_entry, scrub_error_text
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sess")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    home = Path.home()
    e = _minimal_entry("r", "t", "no-such-session", None, FileNotFoundError(
        f"[Errno 2] No such file: '/Users/bob/Secret Project/data.csv' and {home}/x.json "
        f"and {tmp_path}/sess/s.data.q.json and C:\\Users\\bob\\p\\a.csv and \\\\srv\\share\\b.csv"))
    s = e["error_summary"]
    for leak in ("/Users/bob", "Secret Project", str(home), str(tmp_path), "C:\\Users", "\\\\srv"):
        assert leak not in s, (leak, s)
    assert "~/x.json" in s and "session-data:s.data.q.json" in s and "<abs>/data.csv" in s
    assert "<abs>/a.csv" in s and "<abs>/b.csv" in s
    assert scrub_error_text("HTTP 404 from https://x.org/a/b and and/or ratio 3/4") .startswith("HTTP 404 from https://x.org/a/b")


def test_portable_handles_windows_paths():
    h = Path(r"C:\Users\bob")
    assert refs._portable_str(r"C:\Users\bob\proj\x.csv", Path("/s"), None, h) == "~/proj/x.csv"
    assert refs._portable_str(r"c:\users\BOB\x.csv", Path("/s"), None, h) == "~/x.csv"
    assert refs._portable_str(r"C:\ws\a\b.json", Path("/s"), Path(r"C:\ws"), h) == "workspace:a/b.json"
    assert refs._portable_str(r"C:\Users\bob\.aihydro\sessions\x.data.y.json", Path(r"C:\Users\bob\.aihydro\sessions"), None, h) == "session-data:x.data.y.json"
    assert refs._portable_str("not a path", Path("/s"), None, h) == "not a path"


def _scan_capsule(cap: Path, *needles: str) -> dict:
    bad = {}
    for f in cap.rglob("*"):
        if f.is_file() and f.suffix in {".json", ".md", ".py", ".txt", ".csv", ".yaml", ".yml", ".bib"}:
            hits = _leaks(f.read_text(errors="replace"), *needles)
            if hits:
                bad[str(f.relative_to(cap))] = hits[:3]
    return bad


def _delineate_world(world, monkeypatch, tmp_path):
    """Fresh session (no watershed slot) with delineate_watershed's NLDI call mocked."""
    sid = "privacy-full"
    s = HydroSession(sid)
    s.site_id, s.site_type = GAUGE, "usgs_gauge"
    s.save()

    class _R:
        def to_dict(self):
            return {"data": {"area_km2": 250.0, "gauge_id": GAUGE, "gauge_name": "Mock",
                             "gauge_lat": 39.25, "gauge_lon": -77.45,
                             "geometry_geojson": json.loads(SQUARE)},
                    "meta": {"tool": "delineate_watershed", "source": "mock NLDI"}}

    monkeypatch.setattr("ai_hydro.analysis.watershed.delineate_watershed", lambda **k: _R())
    return sid


def test_full_session_delineate_fetch_signatures_export_leaks_nothing(world, tmp_path, monkeypatch):
    server, _ = world
    sid = _delineate_world(world, monkeypatch, tmp_path)
    is_err, ws = _call(server, "delineate_watershed", {"session_id": sid, "gauge_id": GAUGE})
    assert not is_err and not ws.get("error"), ws
    for tool, args in (
        ("fetch_streamflow_data", {"session_id": sid, "gauge_id": GAUGE, "start_date": START, "end_date": END}),
        ("extract_hydrological_signatures", {"session_id": sid, "start_date": START, "end_date": END,
                                             "geometry_geojson": SQUARE}),
    ):
        is_err, res = _call(server, tool, args)
        assert not is_err and not res.get("error"), res

    rows = HydroSession.load(sid).get("_run_log")
    ws_row = next(r for r in rows.values() if r.get("tool_name") == "delineate_watershed")
    assert ws_row["key_outputs"]["geometry_geojson_path"].startswith("session-data:")
    assert not _leaks(json.dumps(rows, default=str), str(tmp_path))
    from ai_hydro.session import run_records as rr
    assert all(rr.verify_run_log_entry(r)["record_ok"] for r in rows.values() if r.get("record"))

    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=sid, capsule_path=str(tmp_path / "capsule"))
    assert "error" not in res, res
    cap = Path(res["capsule_dir"])
    assert not _scan_capsule(cap, str(tmp_path))
    assert json.loads((cap / "capsule_manifest.json").read_text())["privacy"]["legacy_paths_scrubbed_on_export"] == 0


def test_legacy_sealed_row_with_path_is_redacted_in_export_and_replay_notes_it(world, tmp_path, monkeypatch):
    """A pre-fix sealed row holding a path is never rewritten in the store; the
    export carries a redacted stub with its record_digest; replay: not FAIL, not PASS."""
    import subprocess, sys
    from ai_hydro.session import run_records as rr, store
    server, _ = world
    sid = _delineate_world(world, monkeypatch, tmp_path)
    _call(server, "delineate_watershed", {"session_id": sid, "gauge_id": GAUGE})
    # Forge a legacy sealed row directly in the db (bypassing the write-time scrubber).
    leak = str(tmp_path / "home" / ".aihydro" / "sessions" / f"{sid}.geojson")
    body = {"run_id": "legacy_1", "tool_name": "legacy_tool", "timestamp": "2026-01-01T00:00:00+00:00",
            "key_outputs": {"geometry_geojson_path": leak}}
    rec = rr.build_run_record(run_id="legacy_1", tool="legacy_tool", session_id=sid, entry=body)
    import sqlite3
    conn = store._run_log_connect(sid)
    conn.execute("INSERT OR REPLACE INTO runs (run_id, timestamp, entry_json) VALUES (?,?,?)",
                 ("legacy_1", body["timestamp"], json.dumps({**body, "record": rec.to_dict()})))
    conn.commit(); conn.close()

    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=sid, capsule_path=str(tmp_path / "capsule"))
    cap = Path(res["capsule_dir"])
    assert not _scan_capsule(cap, str(tmp_path))
    rl = json.loads((cap / "run_log.json").read_text())
    assert rl["legacy_1"]["redacted_for_privacy"] and rl["legacy_1"]["record_digest"] == rec.record_digest
    priv = json.loads((cap / "capsule_manifest.json").read_text())["privacy"]
    assert priv["legacy_paths_scrubbed_on_export"] == 1 and priv["rows_redacted_for_privacy"] == 1
    # store row untouched
    assert HydroSession.load(sid).get("_run_log")["legacy_1"]["key_outputs"]["geometry_geojson_path"] == leak
    p = subprocess.run([sys.executable, "replay.py"], cwd=cap, capture_output=True, text=True, timeout=120)
    assert "redacted for privacy" in p.stdout and "FAIL" not in p.stdout, p.stdout[-1500:]


def test_scrub_is_idempotent_and_whole_path_values():
    from ai_hydro.session.refs import scrub_value
    once = scrub_value({"a": "/Users/bob/My Docs/x.csv", "b": f"see '/opt/z/q.json' ok", "c": 3})
    assert once["a"] == "<abs>/x.csv" and once["b"] == "see '<abs>/q.json' ok"
    assert scrub_value(once) == once
