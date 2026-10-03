"""
Honest replay (ADR-001 / ADR-005, slice 1a, acceptance criterion 7).

These tests build a fresh, non-bench session through the public API (tool call
through the recording middleware -> ``export_session`` -> the generated
``replay.py`` in a subprocess). The previous ``--live`` check read
``session.json["slots"]``, a key real exports do not have, so it passed with
zero comparisons on every real capsule. Hand-built ``{"slots": ...}`` fixtures
in test_capsule.py are the legacy shape and are kept working separately.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import subprocess
import sys
from pathlib import Path

import fastmcp
import mcp.types as mcp_types
import pytest
from fastmcp import FastMCP

from aihydro_core.records import RunRecord, digest

from ai_hydro.capsule import standalone_replay as sr
from ai_hydro.capsule.manifest import MANIFEST_FILE, build_manifest
from ai_hydro.session import run_records as rr
from ai_hydro.session import store
from ai_hydro.session.chat_binding import ChatBindingStore
from ai_hydro.session.store import HydroSession

needs_fastmcp2 = pytest.mark.skipif(
    int(fastmcp.__version__.split(".")[0]) >= 3,
    reason="uses the real middleware chain on the pinned FastMCP 2.x API",
)

SID = "replay-real-shape"


def _call(server, name, arguments):
    handler = server._mcp_server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call", params=mcp_types.CallToolRequestParams(name=name, arguments=arguments))
    result = asyncio.run(handler(req))
    root = result.root if hasattr(result, "root") else result
    return json.loads(root.content[0].text)


@pytest.fixture
def world(tmp_path, monkeypatch):
    from ai_hydro.mcp import app
    from ai_hydro.mcp.enforcement import post_run
    from ai_hydro.mcp.helpers import _session_store
    from ai_hydro.session import chat_binding

    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(chat_binding, "_store", ChatBindingStore(tmp_path / "chat_studies.json"))
    (tmp_path / "sessions").mkdir()
    HydroSession(SID).save()

    server = FastMCP(name="replay")
    server.add_middleware(app._ContextInjectionMiddleware())
    server.add_middleware(app.RunRecordMiddleware())

    @server.tool()
    def extract_hydrological_signatures(session_id: str, q_mean: float = 2.5) -> dict:
        data = {"q_mean": q_mean, "baseflow_index": 0.41, "flag": "ok"}
        res = {"data": dict(data), "meta": {"tool": "extract_hydrological_signatures", "params": {}}}
        _session_store(session_id, "signatures", res, tool_name="extract_hydrological_signatures")
        return post_run("extract_hydrological_signatures", session_id,
                        {"data": dict(data), "meta": res["meta"]})

    @server.tool()
    def add_note(session_id: str, note: str = "") -> dict:
        return {"data": {"added": True}}

    return server, tmp_path


def _export(tmp_path: Path) -> Path:
    import ai_hydro.mcp.tools_session as ts

    result = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "capsule"))
    assert "error" not in result, result
    assert result["replay_status"] == "archive_integrity" and result["recomputation"] == "not_performed"
    return Path(result["capsule_dir"])


def _replay(capsule: Path, *args: str):
    proc = subprocess.run([sys.executable, str(capsule / "replay.py"), *args],
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def _rebuild_manifest(capsule: Path):
    """An attacker who edits files can also regenerate the file manifest."""
    (capsule / MANIFEST_FILE).write_text(json.dumps(build_manifest(capsule), indent=2))


# --------------------------------------------------------------- real-shape live
@needs_fastmcp2
def test_live_on_a_real_export_compares_something_and_says_what_it_established(world):
    server, tmp_path = world
    _call(server, "extract_hydrological_signatures", {"session_id": SID})
    capsule = _export(tmp_path)
    assert "slots" not in json.loads((capsule / "session.json").read_text()), "real exports have no 'slots' key"

    code, out = _replay(capsule, "--live")
    assert code == 0, out
    assert "replay_status: cross_check" in out
    assert "recomputation: not_performed" in out
    comparisons = int(out.split("comparisons: ")[1].split()[0])
    assert comparisons >= 1
    assert "v2 records verify" in out and "0 legacy" in out


def test_post_run_then_export_then_live_replay_reports_comparisons(tmp_path, monkeypatch):
    """The acceptance path without the middleware: a result written via post_run
    (public API), exported, verified by the generated replay.py in a subprocess."""
    from ai_hydro.mcp.enforcement import post_run
    from ai_hydro.mcp.helpers import _session_store

    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()
    HydroSession(SID).save()
    res = {"data": {"q_mean": 2.5, "baseflow_index": 0.41}, "meta": {"tool": "extract_hydrological_signatures"}}
    _session_store(SID, "signatures", res, tool_name="extract_hydrological_signatures")
    post_run("extract_hydrological_signatures", SID, {"data": dict(res["data"]), "meta": res["meta"]})
    capsule = _export(tmp_path)
    code, out = _replay(capsule, "--live")
    assert code == 0, out
    assert int(out.split("comparisons: ")[1].split()[0]) >= 1
    assert "replay_status: cross_check" in out


@needs_fastmcp2
def test_live_never_passes_vacuously_exits_2_when_nothing_is_comparable(world):
    server, tmp_path = world
    _call(server, "add_note", {"session_id": SID, "note": "n"})       # a run with no comparable numbers
    capsule = _export(tmp_path)
    code, out = _replay(capsule, "--live")
    assert code == 2, out
    assert "comparisons: 0" in out and "replay_status: archive_integrity" in out
    assert "nothing was cross-checked" in out
    # Without --live the capsule is still a valid archive.
    assert _replay(capsule)[0] == 0


def test_live_on_a_legacy_export_without_records_still_compares_but_flags_legacy_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()
    session = HydroSession(SID)
    session.set("signatures", {"data": {"q_mean": 1.25}, "meta": {"tool": "extract_hydrological_signatures"}})
    session.set("_run_log", {"old.1": {"run_id": "old.1", "tool_name": "extract_hydrological_signatures",
                                       "session_id": SID, "timestamp": "2026-06-10T00:00:00+00:00",
                                       "key_outputs": {"q_mean": 1.25}}})
    session.save()
    capsule = _export(tmp_path)
    code, out = _replay(capsule, "--live")
    assert code == 0, out
    assert "0 of 0 v2 records verify" in out and "2 legacy rows have no record" in out   # run-log row + put_result row


@needs_fastmcp2
def test_a_run_log_value_that_disagrees_with_the_retained_session_fails_live(world):
    server, tmp_path = world
    _call(server, "extract_hydrological_signatures", {"session_id": SID})
    capsule = _export(tmp_path)
    raw = json.loads((capsule / "session.json").read_text())
    raw["signatures"]["__legacy__"][""]["data"]["q_mean"] = 99.0       # session drifted from the run log
    (capsule / "session.json").write_text(json.dumps(raw))
    for name in ("bundle.json", "ro-crate-metadata.json", "manifest-sha256.txt"):
        (capsule / name).unlink()       # models a capsule that predates the crate (the bundle pins file digests)
    _rebuild_manifest(capsule)
    code, out = _replay(capsule, "--live")
    assert code == 1, out
    assert "FAIL" in out and "replay_status: archive_integrity" in out    # integrity ok, cross-check not claimed


# ------------------------------------------------------------ record verification
@needs_fastmcp2
def test_record_tampering_is_caught_even_if_the_file_manifest_is_regenerated(world):
    server, tmp_path = world
    body = _call(server, "extract_hydrological_signatures", {"session_id": SID})
    capsule = _export(tmp_path)
    log = json.loads((capsule / "run_log.json").read_text())
    log[body["_run_id"]]["record"]["status"] = "error"
    (capsule / "run_log.json").write_text(json.dumps(log))
    _rebuild_manifest(capsule)
    code, out = _replay(capsule)
    assert code == 1 and "record_digest mismatch" in out and "replay_status: not_performed" in out


@needs_fastmcp2
def test_editing_run_log_outputs_without_touching_the_record_is_caught(world):
    server, tmp_path = world
    body = _call(server, "extract_hydrological_signatures", {"session_id": SID})
    capsule = _export(tmp_path)
    log = json.loads((capsule / "run_log.json").read_text())
    log[body["_run_id"]]["key_outputs"]["q_mean"] = 7.7
    (capsule / "run_log.json").write_text(json.dumps(log))
    _rebuild_manifest(capsule)
    code, out = _replay(capsule)
    assert code == 1 and "run-log row changed after its record was sealed" in out


def test_file_tampering_is_caught_by_the_manifest(world):
    server, tmp_path = world
    HydroSession.load(SID).set("_run_log", {"x.1": {"run_id": "x.1", "tool_name": "t", "key_outputs": {}}})
    capsule = _export(tmp_path)
    (capsule / "README.md").write_text("tampered")
    code, out = _replay(capsule)
    assert code == 1 and "FAIL  README.md" in out and "replay_status: not_performed" in out


# ------------------------------------------------------------------- manifest
@needs_fastmcp2
def test_manifest_states_the_strongest_level_it_supports_and_counts_records(world):
    server, tmp_path = world
    _call(server, "extract_hydrological_signatures", {"session_id": SID})
    capsule = _export(tmp_path)
    manifest = json.loads((capsule / MANIFEST_FILE).read_text())
    assert manifest["replay_status"] == "archive_integrity"
    assert manifest["recomputation"] == "not_performed"
    records = manifest["run_records"]
    assert records["v2_records"] == records["run_log_rows"] - records["legacy_unrecorded"] >= 2
    assert records["records_with_record_error"] == 0
    assert records["environment"]["env_digest"].startswith("sha256:")
    assert records["matches_exporting_environment"] == records["v2_records"]


def test_replay_source_is_the_tested_module_verbatim(world):
    server, tmp_path = world
    HydroSession.load(SID).set("_run_log", {"x.1": {"run_id": "x.1", "tool_name": "t", "key_outputs": {}}})
    capsule = _export(tmp_path)
    assert (capsule / "replay.py").read_text() == Path(sr.__file__).read_text()


# ------------------------------------- embedded canonicalization == core's, bit for bit
def _random_json(rng: random.Random, depth: int = 0):
    kinds = ["none", "bool", "int", "float", "str", "special"] + (["list", "dict"] if depth < 3 else [])
    kind = rng.choice(kinds)
    if kind == "none":
        return None
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "int":
        return rng.choice([rng.randint(-10**12, 10**12), rng.randint(-10**20, 10**20), 2**53, 2**53 + 1])
    if kind == "float":
        return rng.uniform(-1e6, 1e6)
    if kind == "str":
        return rng.choice(["", "a", "naïve", "日本", "$float", "line\nbreak", 'q"uote'])
    if kind == "special":
        return rng.choice([float("nan"), float("inf"), float("-inf")])
    if kind == "list":
        return [_random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    keys = ["a", "b", "$map", "$float", "z9", "é", "k"]
    return {rng.choice(keys): _random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def test_embedded_canonicalization_matches_core_digest():
    rng = random.Random(20261002)
    for _ in range(400):
        value = _random_json(rng)
        assert sr.c14n_digest(value) == digest(value), value


def test_embedded_seal_check_agrees_with_core_verify():
    base = rr.build_run_record(run_id="t.1", tool="demo", session_id="s", arguments={"a": float("nan")},
                               result={"data": {"x": [1, 2]}}, parents=["p.1"],
                               extra={"unicode": "naïve", "n": 3})
    cases = [base.to_dict()]
    for mutate in (
        lambda d: d.update(status="error"),
        lambda d: d["extra"].update(n=4),
        lambda d: d.update(unknown_field="kept"),            # unknown fields are covered by the seal
        lambda d: d.pop("record_digest"),
    ):
        tampered = json.loads(json.dumps(base.to_dict()))
        mutate(tampered)
        cases.append(tampered)
    # a record from a newer writer carries a field this version does not know
    newer = RunRecord.from_dict({**base.to_dict(), "future_field": {"x": 1}}).seal().to_dict()
    cases.append(newer)
    for case in cases:
        round_tripped = json.loads(json.dumps(case))
        assert sr.record_seal_ok(round_tripped) == RunRecord.from_dict(round_tripped).verify(), case


CORE_VECTORS = Path(__import__("aihydro_core").__file__).resolve().parent.parent / "tests" / "data" / "c14n_vectors.json"


@pytest.mark.skipif(not CORE_VECTORS.exists(), reason="core golden vectors not on this path")
def test_standalone_reproduces_every_core_golden_vector():
    vectors = json.loads(CORE_VECTORS.read_text(encoding="utf-8"))
    assert vectors["canonicalization"] == "aihydro.c14n/1" and vectors["cases"]
    for case in vectors["cases"]:
        assert sr.canonical_bytes(case["input"]).decode("utf-8") == case["canonical"], case["name"]
        assert sr.c14n_digest(case["input"]) == case["digest"], case["name"]


def test_replay_of_an_empty_archive_is_not_performed(tmp_path):
    (tmp_path / MANIFEST_FILE).write_text(json.dumps({"files": []}))
    (tmp_path / "replay.py").write_text(sr.source_text())
    code, out = _replay(tmp_path)
    assert code == 1 and "replay_status: not_performed" in out
