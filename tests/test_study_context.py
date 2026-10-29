"""Active-study context resource (T-R1) and the research.md retirement stub (M8).

Acceptance (ux-program Wave 1, T-R1):
  1. no numeric literal in the context output without a [run:]/[claim:] marker   (gate tests)
  2. only the chat's active study appears                                       (isolation tests)
  3. research.md is never rewritten after the stub                              (stub tests, here and in
     test_provenance_units.py / test_context_diet.py)

Every test runs under the per-test scratch AIHYDRO_HOME of tests/conftest.py; nothing reads ~/.aihydro.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from binding_helpers import CHAT_A, CHAT_B, call, make_study, read_resource

from ai_hydro.session import HydroSession
from ai_hydro.session.chat_binding import get_binding_store
from ai_hydro.session.project_registry import get_project_registry
from ai_hydro.session.study_context import (
    PROFILE_MAX_BYTES, RESEARCH_MD_STUB, STUB_MARKER, WITHHELD, bare_numbers, build_context,
    profile_block, write_research_md_stub,
)

T0 = "2026-10-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AIHYDRO_RECENT_SESSION_FALLBACK", raising=False)
    monkeypatch.delenv("AIHYDRO_EVAL_NONCE", raising=False)


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return d


def _project(ws) -> str:
    res, body = call("ensure_project", {"root_dir": str(ws)})
    assert not res.is_error and not body.get("error"), body
    return body["project_id"]


def _study(sid, ws, *, site_id="01031500", signatures=True, extra=None):
    s = make_study(sid, workspace=str(ws), site_id=site_id, site_name="Piscataquis River",
                   site_type="usgs_gauge", extra=extra)
    s.set("watershed", {"data": {"gauge_name": "Piscataquis River", "area_km2": 769.0},
                        "meta": {"computed_at": T0, "tool": "delineate_watershed"}})
    if signatures:
        s.set("signatures", {"data": {"baseflow_index": 0.4417, "q_mean": 1.83, "flow_quantile_convention": "exceedance"},
                             "meta": {"computed_at": T0, "tool": "extract_hydrological_signatures"}})
    s.save()
    return s


def _read(prj, chat=None) -> dict:
    uri = f"aihydro://project/{prj}/context" + (f"?chat={chat}" if chat else "")
    return read_resource(uri)


def _add(prj, sid):
    get_project_registry().add_study(prj, sid)


# ------------------------------------------------------------------ the resource, end to end

def test_resource_returns_the_chats_study_with_pointer_rendered_values(ws):
    prj = _project(ws)
    _study("st_a", ws)
    _add(prj, "st_a")
    get_binding_store().bind(CHAT_A, "st_a")
    out = _read(prj, CHAT_A)
    assert out["found"] and out["active_study_id"] == "st_a" and out["chat_id"] == CHAT_A
    text = out["text"]
    assert "Piscataquis River" in text and "signatures" in text
    m = re.search(r"baseflow_index = 0\.4417 \[run:([A-Za-z0-9._\-]+)#key_outputs\.baseflow_index\]", text)
    assert m, text
    # the marker resolves: the run id names a real row of this study's log holding that value
    from ai_hydro.session import store
    row = store._run_log_read_one("st_a", m.group(1))
    assert row["key_outputs"]["baseflow_index"] == 0.4417


def test_unknown_project_is_not_found(ws):
    assert _read("prj_01KV1EPM88P9FV5CWNVJ9V60JY", CHAT_A) == {
        "found": False, "project_id": "prj_01KV1EPM88P9FV5CWNVJ9V60JY"}


# ------------------------------------------------------------------ criterion 2: only the active study

def test_only_the_chats_active_study_appears(ws):
    prj = _project(ws)
    _study("st_a", ws, site_id="01031500")
    s2 = _study("st_b", ws, site_id="02231000")
    s2.site_name = "Other River"
    s2.save()
    for sid in ("st_a", "st_b"):
        _add(prj, sid)
    get_binding_store().bind(CHAT_A, "st_a")
    get_binding_store().bind(CHAT_B, "st_b")
    a, b = _read(prj, CHAT_A), _read(prj, CHAT_B)
    assert a["active_study_id"] == "st_a" and b["active_study_id"] == "st_b"
    assert "st_b" not in a["text"] and "Other River" not in a["text"] and "02231000" not in a["text"]
    assert "st_a" not in b["text"] and "01031500" not in b["text"]


def test_no_default_or_recent_study_fallback_without_a_binding(ws):
    prj = _project(ws)
    _study("st_a", ws)
    _add(prj, "st_a")
    reg = get_project_registry()
    reg.mutate(prj, lambda r: r.update(default_study_id="st_a", default_confirmed_by="user"))
    for chat in (None, CHAT_A, "not-a-ulid"):
        out = _read(prj, chat)
        assert out["active_study_id"] is None
        assert "st_a" not in out["text"] and "Piscataquis" not in out["text"]
    assert get_binding_store().lookup_study(CHAT_A) is None        # reading bound nothing


def test_a_study_of_another_project_is_not_shown(ws, tmp_path):
    other_ws = tmp_path / "other"
    other_ws.mkdir()
    prj, other = _project(ws), _project(other_ws)
    _study("st_a", ws)
    _add(prj, "st_a")
    _study("st_x", other_ws, site_id="02231000")
    _add(other, "st_x")
    get_binding_store().bind(CHAT_A, "st_x")                       # the chat's study lives in another project
    out = _read(prj, CHAT_A)
    assert out["active_study_id"] is None
    assert "st_x" not in out["text"] and "02231000" not in out["text"] and "another project" in out["text"]


# ------------------------------------------------------------------ read has no side effects

def _tree(root: Path):
    return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in root.rglob("*") if p.is_file())


def test_reading_the_context_changes_nothing_on_disk(ws):
    prj = _project(ws)
    _study("st_a", ws)
    _add(prj, "st_a")
    get_binding_store().bind(CHAT_A, "st_a")
    home = Path(os.environ["AIHYDRO_HOME"])
    before = (_tree(home), _tree(ws))
    for chat in (CHAT_A, CHAT_B, None):
        _read(prj, chat)
    assert (_tree(home), _tree(ws)) == before
    assert not (ws / ".aihydrorules").exists()


# ------------------------------------------------------------------ criterion 1: the numeric gate

def _strip_markers(text: str) -> str:
    return re.sub(r"\[(?:run|claim|lit):[^\]]*\]", " ", text)


def assert_no_bare_numbers(text: str):
    assert not bare_numbers(text), f"uncited numeric literals: {bare_numbers(text)}"
    # independent of the grammar's classifier: no decimal anywhere outside a marker (a run marker
    # follows each value, so remove "value + marker" pairs first)
    rest = re.sub(r"=\s*[-\d.e+]+\s*\[(?:run|claim):[^\]]*\]", " ", text)
    rest = _strip_markers(rest)
    rest = re.sub(r"`[^`]*`|\bst_[0-9A-Za-z]+|\bprj_[0-9A-Za-z]+", " ", rest)      # code spans, minted ids
    rest = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", rest)                              # ISO dates
    assert not re.search(r"\d+\.\d+", rest), rest


def test_context_has_no_numeric_literal_outside_run_or_claim_markers(ws, monkeypatch):
    from ai_hydro.session.persona import ResearcherProfile

    prj = _project(ws)
    s = _study("st_a", ws)
    s.claims = {"C-001": {"status": "approved", "text": "the BFI is 0.44"}}
    s.interpretation = "The basin has a baseflow index of 0.44 [run:signatures.x#key_outputs.baseflow_index]."
    s.interpretation_at = datetime.now(timezone.utc).isoformat()
    s.notes = ["NSE was 0.82 last time", "check the 3 gauges"]
    s.save()
    _add(prj, "st_a")
    get_binding_store().bind(CHAT_A, "st_a")
    p = ResearcherProfile()
    p.name, p.role = "Ada", "Hydrologist"
    p.research_focus = "baseflow in 12 New England basins, NSE above 0.8"
    p.expertise = ["hydrology", "LSTM"]
    p.add_observation("prefers NSE 0.82")
    p.save()
    text = _read(prj, CHAT_A)["text"]
    assert "[claim:C-001]" in text and "[run:" in text
    assert "0.82" not in text and "NSE was" not in text and "check the" not in text   # notes are not injected
    assert "Ada" in text and "baseflow in 12" not in text                             # the profile line was gated
    assert_no_bare_numbers(text)


def test_a_free_text_number_cannot_reach_the_output(ws):
    prj = _project(ws)
    _study("st_a", ws, extra={"study_label": "Piscataquis, NSE 0.82 study"})
    _add(prj, "st_a")
    get_binding_store().bind(CHAT_A, "st_a")
    text = _read(prj, CHAT_A)["text"]
    assert "0.82" not in text
    assert_no_bare_numbers(text)


def test_coordinate_labels_do_not_leak_a_measurement(ws):
    prj = _project(ws)
    make_study("st_c", workspace=str(ws), outlet=(24.2, -75.75))
    _add(prj, "st_c")
    get_binding_store().bind(CHAT_A, "st_c")
    text = _read(prj, CHAT_A)["text"]
    assert "24.20" not in text and "unnamed study" in text
    assert_no_bare_numbers(text)


def test_the_final_gate_withholds_a_line_with_a_bare_number():
    assert bare_numbers("baseflow index is 0.44")
    assert not bare_numbers("baseflow_index = 0.44 [run:signatures.ab12#key_outputs.baseflow_index]")
    assert not bare_numbers("Study `st_01KV1EPM88P9FV5CWNVJ9V60JY` updated 2026-10-01")
    assert "withheld" in WITHHELD


def test_a_record_that_no_longer_verifies_renders_a_stale_pointer_without_the_value(ws):
    from ai_hydro.session import store

    prj = _project(ws)
    _study("st_a", ws)
    _add(prj, "st_a")
    get_binding_store().bind(CHAT_A, "st_a")
    rows = store._run_log_read_all("st_a")
    rid, row = next((r, e) for r, e in rows.items() if e.get("slot") == "signatures")
    row["record"] = {"run_id": rid, "record_digest": "0" * 64}        # a seal that cannot verify
    import sqlite3
    conn = sqlite3.connect(str(store._run_log_db_path("st_a")))
    conn.execute("UPDATE runs SET entry_json=? WHERE run_id=?", (json.dumps(row), rid))
    conn.commit()
    conn.close()
    text = _read(prj, CHAT_A)["text"]
    assert "stale pointer" in text and "0.4417" not in text
    assert_no_bare_numbers(text)


# ------------------------------------------------------------------ study digest content

def test_degraded_slot_is_not_listed_as_computed(ws):
    prj = _project(ws)
    s = HydroSession("st_d")
    s.set("watershed", {"data": {"area_km2": 5.0}, "meta": {"computed_at": T0}})
    s.set("signatures", {"data": {"q_mean": 1.0, "baseflow_index": 0.5}, "meta": {"computed_at": T0}})  # no convention
    s.site_id, s.workspace_dir = "01013500", str(ws)
    s.save()
    _add(prj, "st_d")
    get_binding_store().bind(CHAT_A, "st_d")
    text = _read(prj, CHAT_A)["text"]
    assert "**Degraded (do not cite; re-run)**" in text and "signatures (legacy_convention)" in text
    assert "baseflow_index" not in text                               # a degraded slot's values are not offered


def test_stale_interpretation_is_marked(ws):
    prj = _project(ws)
    s = _study("st_a", ws)
    s.interpretation = "My interpretation"
    s.interpretation_at = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    s.save()
    _add(prj, "st_a")
    get_binding_store().bind(CHAT_A, "st_a")
    text = _read(prj, CHAT_A)["text"]
    assert "**Interpretation**: written" in text and "(stale)" in text and "My interpretation" not in text


# ------------------------------------------------------------------ profile

def test_profile_is_at_most_one_kb_and_user_fields_only(ws):
    from ai_hydro.session.persona import ResearcherProfile

    p = ResearcherProfile()
    p.name = "Ada"
    p.expertise = [f"topic{chr(97 + i % 26)}{chr(97 + i // 26)}" for i in range(400)]
    p.communication_style = "terse"
    p.add_observation("agent saw something")
    p.save()
    blk = profile_block()
    assert 0 < len(blk.encode("utf-8")) <= PROFILE_MAX_BYTES
    assert "Ada" in blk and "agent saw something" not in blk


def test_blank_profile_adds_no_section(ws):
    assert profile_block() == ""
    assert "Researcher profile" not in build_context(_project(ws), CHAT_A)["text"]


# ------------------------------------------------------------------ criterion 3: the research.md stub

def test_stub_is_written_once_and_replaces_an_old_digest(tmp_path):
    rm = tmp_path / ".aihydrorules" / "research.md"
    rm.parent.mkdir()
    rm.write_text("# Research Session\nBFI=0.44 old digest")
    assert write_research_md_stub(str(tmp_path)) is True
    assert rm.read_text() == RESEARCH_MD_STUB and STUB_MARKER in rm.read_text()
    os.utime(rm, ns=(1_000_000_000, 1_000_000_000))
    for _ in range(3):
        assert write_research_md_stub(str(tmp_path)) is False
    assert rm.stat().st_mtime_ns == 1_000_000_000


def test_stub_text_points_at_the_resource_and_carries_no_number():
    assert "aihydro://project/" in RESEARCH_MD_STUB and "/context" in RESEARCH_MD_STUB
    assert not bare_numbers(RESEARCH_MD_STUB)


def test_no_code_path_writes_a_digest_into_research_md(ws):
    from ai_hydro.session import project as project_mod

    s = _study("st_a", ws)
    s.interpretation = "x"
    s.save(sync_context=True)
    project_mod.ProjectSession("legacy").save()
    s.archive()
    for rm in (ws / ".aihydrorules" / "research.md", Path(os.environ["AIHYDRO_HOME"]) / ".aihydrorules" / "research.md"):
        assert rm.read_text() == RESEARCH_MD_STUB, rm


def test_bare_bfi_nse_writer_is_gone():
    src = (Path(__file__).resolve().parent.parent / "ai_hydro" / "session" / "project.py").read_text()
    assert "BFI=" not in src and "NSE=" not in src
