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
