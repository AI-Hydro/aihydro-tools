"""Bundle + RO-Crate at export (2040 slice 5, P5.4).

Real-shape: a fresh capsule is exported in a scratch HOME through the real tools
(``add_claim`` / ``update_claim_status`` / ``export_session``), then validated and
verified by aihydro-core and by the generated ``replay.py`` in a subprocess, with
and without core importable. Tamper cases check that the stdlib mirror in
``replay.py`` agrees with core's ``verify_crate`` on what fails. All content is
synthetic; nothing here is a research result.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aihydro_core.export import validate_crate, verify_crate
from aihydro_core.export.rocrate_validate import errors as validate_errors

from ai_hydro.capsule import claim_chain_verify as ccv
from ai_hydro.capsule import standalone_replay as sr
from ai_hydro.capsule.manifest import MANIFEST_FILE, build_manifest
from ai_hydro.capsule.rocrate_export import CrateExportError, convert_capsule, export_crate
from ai_hydro.mcp.tools_ledger import add_claim, update_claim_status
from ai_hydro.session import claim_revisions as cr
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from approval_helpers import basin_ref_full
from test_approval import _run_record
from test_capsule_replay_real_shape import SID as SID_WORLD, _call, needs_fastmcp2, world  # noqa: F401

SID = SID_WORLD
E2E = Path(__file__).resolve().parents[3] / "docs" / "vision-2040" / "evidence" / "e2e-proof-1" / "capsule"
VALIDATOR_ENV = "AIHYDRO_ROCRATE_VALIDATOR"
_NEW = ("bundle.json", "ro-crate-metadata.json", "manifest-sha256.txt")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)


def _seal_run_row(run_id: str, body: dict):
    """Insert ``body`` as a SEALED run-log row (record + entry_digest), as the middleware writes it."""
    from ai_hydro.session import run_records as rr
    rec = rr.build_run_record(run_id=run_id, tool=body.get("tool_name", "fixture"), session_id=SID, entry=body)
    conn = store._run_log_connect(SID)
    conn.execute("INSERT OR REPLACE INTO runs (run_id, timestamp, entry_json) VALUES (?,?,?)",
                 (run_id, body.get("timestamp", "2026-01-01T00:00:00+00:00"), json.dumps({**body, "record": rec.to_dict()})))
    conn.commit()
    conn.close()


def _session_with_claim(ws: Path | None = None):
    s = HydroSession.load(SID)
    if ws is not None:
        s.workspace_dir = str(ws)
    s.save()
    _seal_run_row("r1", _run_record(SID))               # a sealed row with uncertainty evidence for the claim
    assert add_claim(
        session_id=SID, claim_id="c1", statement="Synthetic NSE is 0.8", claim_type="empirical_result",
        status="proposed", confidence="low", confidence_rationale="Synthetic regression fixture only.",
        basins=["synthetic"], period="2000-2001", metric="nse", basin_refs=[basin_ref_full("synthetic")],
        limitations=["Synthetic regression case, no real research conclusion."],
        evidence_spans=[{"source_type": "run", "source_id": "r1", "metric_ref": "nse"}])["status"] == "recorded"
    update_claim_status(SID, "c1", "supported", "medium", "Synthetic regression fixture only.",
                        uncertainty_verified=True)
    return s


def _export(tmp_path: Path, name: str = "capsule") -> tuple[Path, dict]:
    import ai_hydro.mcp.tools_session as ts

    result = ts.export_session(session_id=SID, capsule_path=str(tmp_path / name))
    assert not result.get("error"), result
    return Path(result["capsule_dir"]), result


@pytest.fixture
def exported(world):
    """A fresh capsule exported through the real middleware chain and the real ledger tools."""
    import fastmcp
    if int(fastmcp.__version__.split(".")[0]) >= 3:
        pytest.skip("uses the real middleware chain on the pinned FastMCP 2.x API")
    server, tmp_path = world
    _call(server, "extract_hydrological_signatures", {"session_id": SID})
    _session_with_claim()
    return _export(tmp_path)


def _replay(capsule: Path, *args: str, isolated_python: bool = False):
    cmd = [sys.executable] + (["-I", "-S"] if isolated_python else []) + [str(capsule / "replay.py"), *args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout + p.stderr


def _copy(capsule: Path, tmp_path: Path, name: str) -> Path:
    dst = tmp_path / name
    shutil.copytree(capsule, dst)
    return dst


def _core_has_session() -> bool:
    import aihydro_core.export.rocrate_verify as rv
    return "VER-SESSION" in inspect.getsource(rv)


_CORE_HAS_SESSION = _core_has_session()


def _force_integrity(monkeypatch):
    """An attacker with the exporter: make the exporter's own assessment pass, so the bundle is written and the
    verifiers (core, mirror) are what must refuse it."""
    import ai_hydro.capsule.rocrate_export as re_
    real = re_.sr.assess

    def liar(*a, **k):
        out = real(*a, **k)
        return {**out, "integrity_ok": True, "head_bad": [], "session_bad": []}

    monkeypatch.setattr(re_.sr, "assess", liar)
    monkeypatch.setattr(re_, "clean_outputs", lambda _d: None)


def _rules(res) -> set:
    return {f.rule for f in res.failures}


def _stdlib_rules(capsule: Path) -> set:
    res = sr.verify_bundle(capsule)
    return {r for r, _e, _m in res["failures"]}


# ---------------------------------------------------------------- real-shape export
def test_export_writes_a_crate_that_core_validates_and_verifies(exported):
    cap, result = exported
    assert result["crate_file"] == str(cap / "ro-crate-metadata.json")
    assert result["bundle_id"].startswith("sha256:") and "crate_error" not in result
    for name in _NEW:
        assert (cap / name).is_file()
    assert validate_errors(validate_crate(cap)) == []
    v = verify_crate(cap)
    assert v.ok, v.failures
    assert v.records_verified == v.records_total >= 4      # sealed runs + the claim's two revisions
    bundle = json.loads((cap / "bundle.json").read_text())
    assert bundle["bundle_id"] == result["bundle_id"]
    kinds = {r["kind"] for r in bundle["records"]}
    assert {"run", "claim_revision"} <= kinds
    assert bundle["coverage"]["unverifiable_ids"] == []


def test_replay_exits_zero_with_and_without_core(exported):
    cap, _ = exported
    code, out = _replay(cap, "--live")
    assert code == 0, out
    assert "FAIL" not in out and "replay_status: cross_check" in out
    assert "Crate regenerated by aihydro_core and byte-compared: ok" in out
    code_plain, out_plain = _replay(cap)
    assert code_plain == 0, out_plain
    code_iso, out_iso = _replay(cap, isolated_python=True)     # stdlib only: core is not importable here
    assert code_iso == 0, out_iso
    assert "stdlib mirror" in out_iso and "Crate regenerated" not in out_iso
    assert "replay_status: archive_integrity" in out_iso
    assert "PASS  claim c1: 2 revision(s)" in out_iso


def test_manifest_and_result_report_claim_revisions_and_skip_new_files(exported):
    cap, result = exported
    manifest = json.loads((cap / MANIFEST_FILE).read_text())
    listed = {f["path"] for f in manifest["files"]}
    assert "records/claim_revisions.json" in listed
    assert not (listed & set(_NEW)) and "replay.py" not in listed
    section = manifest["claim_revisions"]
    assert section["ok"] == 1 and section["corrupt"] == 0 and section["revisions"] == 2
    assert [c["claim_id"] for c in section["claims"]] == ["c1"] and result["claim_revisions"] == section
    # the manifest never claims more than the crate, and the crate no more than the manifest
    bundle = json.loads((cap / "bundle.json").read_text())
    order = ["not_performed", "archive_integrity", "cross_check", "recomputed", "independently_replicated"]
    assert order.index(bundle["replay"]["status"]) <= order.index(manifest["replay_status"])
    assert bundle["replay"]["manifest_status"] == manifest["replay_status"]
    assert bundle["claim_heads"]["c1"] == section["claims"][0]["head_revision_digest"]


def test_bundle_pins_claim_head_and_assessor_is_replay_py(exported):
    cap, _ = exported
    bundle = json.loads((cap / "bundle.json").read_text())
    head = cr.history_readonly(SID)[0]["c1"]["rows"][-1]["revision_digest"]
    assert bundle["claim_heads"] == {"c1": head}
    assert bundle["replay"]["assessor"]["sha256"] == hashlib.sha256((cap / "replay.py").read_bytes()).hexdigest()


def test_crate_is_path_free(exported, tmp_path):
    cap, _ = exported
    for name in _NEW:
        text = (cap / name).read_text(encoding="utf-8")
        assert str(tmp_path) not in text and "/Users/" not in text and "/private/" not in text


def test_export_is_repeatable_into_the_same_directory(exported, tmp_path):
    cap, first = exported
    again_cap, again = _export(tmp_path)
    assert again_cap == cap and not again.get("crate_error")
    assert verify_crate(cap).ok and validate_errors(validate_crate(cap)) == []


# ---------------------------------------------------------------- tamper: mirror agrees with core
def _tamper_run_row(cap: Path):
    log = json.loads((cap / "run_log.json").read_text())
    rid = sorted(log)[0]
    log[rid]["key_outputs"] = {**log[rid].get("key_outputs", {}), "tampered": 1}
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))


def _tamper_claim_row(cap: Path):
    doc = json.loads((cap / "records/claim_revisions.json").read_text())
    doc["claims"]["c1"]["rows"][-1]["content"]["status"] = "contested"
    (cap / "records/claim_revisions.json").write_text(json.dumps(doc, indent=2, sort_keys=True))


def _drop_claim_revision(cap: Path):
    doc = json.loads((cap / "records/claim_revisions.json").read_text())
    rows = doc["claims"]["c1"]["rows"]
    doc["claims"]["c1"]["rows"] = [rows[0], *rows[2:]]
    (cap / "records/claim_revisions.json").write_text(json.dumps(doc, indent=2, sort_keys=True))


def _truncate_claim_tail(cap: Path):
    doc = json.loads((cap / "records/claim_revisions.json").read_text())
    doc["claims"]["c1"]["rows"] = doc["claims"]["c1"]["rows"][:-1]
    (cap / "records/claim_revisions.json").write_text(json.dumps(doc, indent=2, sort_keys=True))


def _edit_bundle(cap: Path):
    b = json.loads((cap / "bundle.json").read_text())
    b["session_id"] = "someone-else"
    (cap / "bundle.json").write_text(json.dumps(b, indent=2, sort_keys=True))


def _reseal(cap: Path, mutate):
    """Edit bundle.json and re-seal it with core, so only the semantic rules are in play."""
    from aihydro_core.records import Bundle
    b = json.loads((cap / "bundle.json").read_text())
    mutate(b)
    nb = Bundle.from_dict({k: v for k, v in b.items() if k not in ("bundle_id", "record_digest")}).seal()
    (cap / "bundle.json").write_text(json.dumps(nb.to_dict(), indent=2, sort_keys=True))
    from aihydro_core.export import write_manifest_sha256
    write_manifest_sha256(cap)


def _edit_file(cap: Path):
    (cap / "README.md").write_text("tampered")


def _edit_crate(cap: Path):
    (cap / "ro-crate-metadata.json").write_text((cap / "ro-crate-metadata.json").read_text().replace(
        "\"name\": \"Capsule export\"", "\"name\": \"Capsule exports\""))


def _refresh(cap: Path):
    """An attacker who edits files can regenerate the file manifest and the BagIt list."""
    (cap / MANIFEST_FILE).write_text(json.dumps(build_manifest(cap), indent=2))
    from aihydro_core.export import write_manifest_sha256
    write_manifest_sha256(cap)


@pytest.mark.parametrize("name,tamper,refresh", [
    ("run_row", _tamper_run_row, True),
    ("claim_row", _tamper_claim_row, True),
    ("claim_dropped", _drop_claim_revision, True),
    ("bundle", _edit_bundle, False),
    ("file", _edit_file, False),
    ("assessor_wrong_file", lambda c: _reseal(c, lambda b: b["replay"]["assessor"].update(
        sha256=hashlib.sha256((c / "README.md").read_bytes()).hexdigest())), False),
    ("assessor_sha_absent", lambda c: _reseal(c, lambda b: b["replay"]["assessor"].pop("sha256")), False),
    ("fifo", lambda c: os.mkfifo(c / "data" / "pipe"), False),
])
def test_stdlib_mirror_fails_where_core_fails(exported, tmp_path, name, tamper, refresh):
    cap = _copy(exported[0], tmp_path, "t_" + name)
    tamper(cap)
    if refresh:
        _refresh(cap)
    core = verify_crate(cap)
    mine = sr.verify_bundle(cap)
    assert not core.ok and not mine["ok"], (name, core.failures, mine["failures"])
    # rule ids agree, except that the mirror does not regenerate the crate, and checks the claim head the
    # sealed bundle pins (core does not know that field)
    mine_rules = _stdlib_rules(cap)
    skipped = {"VER-CRATE-REGEN", "VER-VALIDATE"}          # rules the stdlib mirror deliberately does not run
    if _CORE_HAS_SESSION:
        assert _rules(core) - skipped == mine_rules, name     # exact agreement
    else:
        # TODO(core VER-SESSION): once core compares bundle.session_id with its records, this branch goes
        # and the exact equality above applies (test_exact_rule_equality_needs_core_ver_session flips on).
        extra = {"VER-SESSION", "VER-COVERAGE"} if "VER-SESSION" in mine_rules else set()
        assert _rules(core) - skipped == mine_rules - extra, name
    code, out = _replay(cap, isolated_python=True)
    assert code == 1, out


def test_truncated_claim_tail_is_caught_by_the_pinned_head(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_trunc")
    _truncate_claim_tail(cap)
    _refresh(cap)
    # the chain itself still verifies; the head pinned in the sealed bundle exposes the cut
    doc = json.loads((cap / "records/claim_revisions.json").read_text())
    assert ccv.verify_exported(doc)["c1"]["status"] == "verified"
    res = sr.verify_bundle(cap)
    assert not res["ok"]
    code, out = _replay(cap, isolated_python=True)
    assert code == 1 and "FAIL" in out


def test_edited_crate_is_caught_by_regeneration_and_bagit(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_crate")
    _edit_crate(cap)
    assert "VER-CRATE-REGEN" in _rules(verify_crate(cap)) and "VER-BAGIT" in _rules(verify_crate(cap))
    assert "VER-BAGIT" in _stdlib_rules(cap)
    code, out = _replay(cap, isolated_python=True)
    assert code == 1 and "VER-BAGIT" in out
    # an attacker who also refreshes the BagIt list is stopped only by regeneration (core, not the mirror)
    from aihydro_core.export import write_manifest_sha256
    write_manifest_sha256(cap)
    assert _rules(verify_crate(cap)) == {"VER-CRATE-REGEN"}
    assert _replay(cap, isolated_python=True)[0] == 0           # documented gap of the stdlib mirror
    assert _replay(cap)[0] == 1                                  # with core importable it is caught


def test_stdlib_mirror_matches_core_on_a_clean_capsule(exported):
    cap, _ = exported
    mine = sr.verify_bundle(cap)
    core = verify_crate(cap)
    assert mine["ok"] and core.ok
    assert (mine["records_verified"], mine["records_total"], mine["unverifiable_ids"]) == (
        core.records_verified, core.records_total, core.unverifiable_ids)


# ---------------------------------------------------------------- privacy redaction = partial coverage
def _forge_path_row(sid: str, run_id: str, leak: str):
    from ai_hydro.session import run_records as rr
    body = {"run_id": run_id, "tool_name": "legacy_tool", "session_id": sid,
            "timestamp": "2026-01-01T00:00:00+00:00", "key_outputs": {"leak": leak}}
    rec = rr.build_run_record(run_id=run_id, tool="legacy_tool", session_id=sid, entry=body)
    row = {**body, "record": rec.to_dict()}
    conn = store._run_log_connect(sid)
    conn.execute("INSERT OR REPLACE INTO runs (run_id, timestamp, entry_json) VALUES (?,?,?)",
                 (run_id, body["timestamp"], json.dumps(row)))
    conn.commit()
    conn.close()


def test_redacted_run_is_partial_coverage_and_a_cross_check_is_still_reportable(tmp_path):
    _session_with_claim()
    _forge_path_row(SID, "leg_1", "/Users/zz-someone/secret.csv")
    cap, result = _export(tmp_path)
    assert not result.get("crate_error"), result
    bundle = json.loads((cap / "bundle.json").read_text())
    assert bundle["coverage"]["unverifiable_ids"] == ["leg_1"]
    assert bundle["coverage"]["records_verified"] == bundle["coverage"]["records_total"] - 1
    assert verify_crate(cap).ok
    assert bundle["replay"]["checked_status"] in ("archive_integrity", "cross_check")
    assert "zz-someone" not in (cap / "ro-crate-metadata.json").read_text()
    code, out = _replay(cap, isolated_python=True)
    assert code == 3, out
    assert "partial" in out and "archive_integrity_partial" not in out
    assert "VER-RUN-ROWS" not in out
    assert validate_errors(validate_crate(cap)) == []
    code_core, out_core = _replay(cap)                          # with core importable as well
    assert code_core == 3 and "byte-compared: ok" in out_core, out_core
    if os.environ.get(VALIDATOR_ENV):                          # real redacted export, external validator
        p = subprocess.run([os.environ[VALIDATOR_ENV], "validate", "--profile-identifier", "ro-crate-1.3",
                            "--requirement-severity", "REQUIRED", "--no-paging", str(cap)],
                           capture_output=True, text=True, timeout=600)
        assert p.returncode == 0, p.stdout[-2000:]
    # R2: the legacy partial string is coverage, so it no longer hides a successful cross-check
    assert sr.replay_status(True, True, 3, True, partial=True) == "cross_check"
    assert sr.replay_status(True, False, 0, True, partial=True) == "archive_integrity"
    assert sr.replay_status(False, True, 3, True, partial=True) == "not_performed"


# ---------------------------------------------------------------- fail closed
def test_a_capsule_with_unlisted_files_is_refused_and_leaves_no_crate_files(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_stray")
    for name in _NEW:
        (cap / name).unlink()
    (cap / "stray.txt").write_text("not in the manifest")
    with pytest.raises(CrateExportError, match="not a clean export"):
        export_crate(cap, session_id=SID)
    assert not any((cap / n).exists() for n in _NEW)


def test_a_path_in_the_crate_is_refused(exported, tmp_path, monkeypatch):
    import aihydro_core.export as ex
    cap = _copy(exported[0], tmp_path, "t_leak")
    for name in _NEW:
        (cap / name).unlink()
    real = ex.to_rocrate

    def leaky(*a, **k):
        crate = real(*a, **k)
        crate["@graph"][0]["description"] = "ran in /Users/zz-someone/work/dir"
        return crate

    import ai_hydro.capsule.rocrate_export as re_
    monkeypatch.setattr(re_, "to_rocrate", leaky)
    with pytest.raises(CrateExportError, match="privacy check failed"):
        export_crate(cap, session_id=SID)
    assert not any((cap / n).exists() for n in _NEW)


def test_gates_come_only_from_the_allowlisted_code(tmp_path):
    from ai_hydro.approval.records import APPROVAL_REQUIRED
    from aihydro_core.records import GATE_CODES
    assert GATE_CODES == (APPROVAL_REQUIRED,) == sr._GATE_CODES


def test_a_refused_promotion_becomes_a_derived_gate(exported, tmp_path):
    """A sealed, bound run row whose error_summary is the allowlisted code is declared as a gate
    (exactly the set core derives), and a forged declaration is caught."""
    from ai_hydro.approval.records import APPROVAL_REQUIRED
    from ai_hydro.session import run_records as rr
    import ai_hydro.mcp.tools_session as ts

    body = {"run_id": "gate_1", "tool_name": "promote_claim_to_registry", "session_id": SID,
            "timestamp": "2026-10-03T00:00:00+00:00", "key_outputs": {}, "error": True,
            "error_summary": APPROVAL_REQUIRED}
    rec = rr.build_run_record(run_id="gate_1", tool="promote_claim_to_registry", session_id=SID, entry=body,
                              status="error")
    conn = store._run_log_connect(SID)
    conn.execute("INSERT OR REPLACE INTO runs (run_id, timestamp, entry_json) VALUES (?,?,?)",
                 ("gate_1", body["timestamp"], json.dumps({**body, "record": rec.to_dict()})))
    conn.commit()
    conn.close()
    result = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "gated"))
    assert not result.get("crate_error"), result
    cap = Path(result["capsule_dir"])
    bundle = json.loads((cap / "bundle.json").read_text())
    assert [(g["run_id"], g["code"]) for g in bundle["gates"]] == [("gate_1", APPROVAL_REQUIRED)]
    assert verify_crate(cap).ok and sr.verify_bundle(cap)["ok"]
    # an undeclared gate (declaration dropped from the sealed bundle) is a failure in both verifiers
    b = json.loads((cap / "bundle.json").read_text())
    b["gates"] = []
    from aihydro_core.records import Bundle
    nb = Bundle.from_dict({k: v for k, v in b.items() if k not in ("bundle_id", "record_digest")}).seal()
    (cap / "bundle.json").write_text(json.dumps(nb.to_dict(), indent=2, sort_keys=True))
    assert "VER-GATES" in _rules(verify_crate(cap)) and "VER-GATES" in _stdlib_rules(cap)


# ---------------------------------------------------------------- conversion of existing capsules
def _tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.skipif(not E2E.is_dir(), reason="frozen e2e-proof-1 capsule not on this path")
def test_converting_the_frozen_e2e_capsule_is_out_of_place(tmp_path):
    src = _copy(E2E, tmp_path, "e2e_in")
    before = _tree(src)
    out = convert_capsule(src, tmp_path / "e2e_out")
    assert _tree(src) == before, "input capsule was modified"
    dst = Path(out["output_dir"])
    assert out["claim_revisions"] == "not_carried"
    assert out["coverage"]["records_total"] == 10 and out["coverage"]["unverifiable_ids"] == []
    assert validate_errors(validate_crate(dst)) == []
    assert verify_crate(dst).ok
    for args in ((), ("--live",)):
        code, text = _replay(dst, *args, isolated_python=True)
        assert code == 0, text
    # a regression cross-check against the frozen records.json (same store, not independent corroboration)
    records_json = E2E.parent / "records.json"
    if records_json.is_file():
        frozen = json.loads(records_json.read_text())
        bundle = json.loads((dst / "bundle.json").read_text())
        mine = {r["id"]: r["record_digest"] for r in bundle["records"] if r["kind"] == "run"}
        shared = [rid for rid in mine if rid in frozen]
        for rid in shared:
            rec = frozen[rid]
            assert mine[rid] == (rec["record"]["record_digest"])


def test_conversion_refuses_a_non_empty_output_and_an_output_inside_the_input(exported, tmp_path):
    cap = exported[0]
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    with pytest.raises(CrateExportError, match="not empty"):
        convert_capsule(cap, busy)
    with pytest.raises(CrateExportError, match="inside the input"):
        convert_capsule(cap, cap / "nested_out")


def test_conversion_script_runs_and_reports(exported, tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "capsule_to_rocrate.py"
    out_dir = tmp_path / "script_out"
    p = subprocess.run([sys.executable, str(script), str(exported[0]), str(out_dir), "--no-live"],
                       capture_output=True, text=True, timeout=300,
                       env={**os.environ, "PYTHONPATH": os.pathsep.join(
                           [str(Path(script).parents[1]), os.environ.get("PYTHONPATH", "")])})
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads(p.stdout)["claim_revisions"] == "carried"
    assert verify_crate(out_dir).ok


# ---------------------------------------------------------------- vendored verifier == claim_chain_verify
def test_replay_vendors_the_claim_chain_verifier_verbatim():
    for name in ("_encode", "_es_number", "_es_string", "_serialize", "c14n_digest", "is_digest", "_valid",
                 "verify_claim_revision", "verify_chain", "verify_exported", "_verify_with_stubs"):
        mod_fn = getattr(ccv, name)
        sr_fn = getattr(sr, name)
        assert inspect.getsource(mod_fn) == inspect.getsource(sr_fn), name
    for const in ("CLAIM_REVISION_SCHEMA", "CANONICALIZATION", "ACTOR_KINDS", "REDACTED_KEY", "_KNOWN_FIELDS"):
        assert getattr(ccv, const) == getattr(sr, const), const


def test_session_id_mismatch_in_the_claims_document_fails(exported):
    cap, _ = exported
    doc = json.loads((cap / "records/claim_revisions.json").read_text())
    doc["session_id"] = "another-session"
    assert ccv.verify_exported(doc)["c1"]["status"] == "failed"
    assert sr.verify_exported(doc)["c1"]["status"] == "failed"


def test_entry_digest_matches_core_golden_vectors():
    from aihydro_core.records import entry_digest as core_entry_digest
    vectors = Path(__import__("aihydro_core").__file__).resolve().parent.parent / "tests" / "data" / "entry_vectors.json"
    if not vectors.exists():
        pytest.skip("core entry vectors not on this path")
    for case in json.loads(vectors.read_text())["cases"]:
        assert sr.entry_digest(case["object"]) == case["digest"] == core_entry_digest(case["object"]), case.get("name")


# ---------------------------------------------------------------- optional external validator
@pytest.mark.skipif(not os.environ.get(VALIDATOR_ENV), reason=f"{VALIDATOR_ENV} not set")
def test_external_validator_reports_no_required_failures(exported):
    cap, _ = exported
    p = subprocess.run([os.environ[VALIDATOR_ENV], "validate", "--profile-identifier", "ro-crate-1.3",
                        "--requirement-severity", "REQUIRED", "--no-paging", str(cap)],
                       capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-1000:]


# ---------------------------------------------------------------- follow-ups (S1, S2, S3, F1, minors)
def test_bundle_session_id_must_match_the_records(exported, tmp_path):
    """Core does not compare them yet (to be routed); the mirror does, so a re-sealed bundle
    renamed to another session fails replay on the stdlib path."""
    cap = _copy(exported[0], tmp_path, "t_session")
    _reseal(cap, lambda b: b.update(session_id="another-session"))
    assert "VER-SESSION" in _stdlib_rules(cap)
    code, out = _replay(cap, isolated_python=True)
    assert code == 1 and "VER-SESSION" in out


def test_stdlib_path_says_what_the_mirror_does_not_check(exported):
    code, out = _replay(exported[0], isolated_python=True)
    assert code == 0, out
    assert "stdlib mirror only: not checked here are crate regeneration" in out and "VER-VALIDATE" in out
    code2, out2 = _replay(exported[0])
    assert "stdlib mirror only" not in out2          # with core importable those checks ran


def test_exporter_names_the_projecting_core_version(exported):
    import aihydro_core
    cap, _ = exported
    bundle = json.loads((cap / "bundle.json").read_text())
    assert bundle["exporter"]["projection"] == f"aihydro-core {aihydro_core.__version__}"
    _code, out = _replay(cap)
    assert f"regenerated here with aihydro-core {aihydro_core.__version__}" in out


def _insert_legacy_row(run_id: str, key_outputs=None):
    """An UNSEALED legacy run-log row (no record), as written before sealing existed."""
    body = {"run_id": run_id, "tool_name": "legacy_tool", "session_id": SID, "timestamp": "2026-01-01T00:00:00+00:00",
            "key_outputs": key_outputs or {"v": 1.0}}
    conn = store._run_log_connect(SID)
    conn.execute("INSERT OR REPLACE INTO runs (run_id, timestamp, entry_json) VALUES (?,?,?)",
                 (run_id, body["timestamp"], json.dumps(body)))
    conn.commit()
    conn.close()


def test_bundle_carries_the_unsealed_row_counts(exported, tmp_path):
    _insert_legacy_row("legacy_cnt")
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "counts"))
    cap = Path(res["capsule_dir"])
    rr_ = json.loads((cap / "bundle.json").read_text())["run_rows"]
    assert set(rr_) == {"run_log_rows", "sealed", "legacy_no_record", "unbound", "unsealable",
                        "withheld_for_privacy"}
    assert rr_["legacy_no_record"] >= 1 and rr_["sealed"] >= 1
    assert rr_["run_log_rows"] == rr_["sealed"] + rr_["legacy_no_record"] + rr_["withheld_for_privacy"]
    # core C-S2: 7+ sealed rows and one legacy row are never a plain ok
    assert res["crate_status"] == "ok_partial_unsealed" and res["crate_partial_reasons"] == ["unsealed"]
    assert verify_crate(cap).status == "ok_partial_unsealed"
    for kw in ({"isolated_python": True}, {}):
        code, out = _replay(cap, **kw)
        assert code == 3 and "partial_reasons: unsealed" in out and "replay_outcome: ok_partial_unsealed" in out, out


def test_C_S2_both_reasons_and_a_stripped_stub_flag(exported, tmp_path):
    _insert_legacy_row("legacy_both")
    _forge_path_row(SID, "leg_both", "/Users/zz-someone/secret.csv")
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "both_reasons"))
    cap = Path(res["capsule_dir"])
    assert sorted(res["crate_partial_reasons"]) == ["unsealed", "withheld"] and res["crate_status"] == "ok_partial_withheld"
    core = verify_crate(cap)
    assert sorted(core.partial_reasons) == ["unsealed", "withheld"] and core.status == "ok_partial_withheld"
    mine = sr.verify_bundle(cap)
    assert sorted(mine["partial_reasons"]) == ["unsealed", "withheld"]
    code, out = _replay(cap, isolated_python=True)
    assert code == 3 and "partial_reasons: unsealed,withheld" in out
    # strip the stub flag: the row is now an unsealed legacy-looking row, so the capsule is not plain ok
    log = json.loads((cap / "run_log.json").read_text())
    log["leg_both"].pop("redacted_for_privacy")
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))
    _refresh_manifest(cap)
    assert verify_crate(cap).status != "ok" and _replay(cap, isolated_python=True)[0] != 0


def _mark_claim_corrupt(cap: Path):
    p = cap / "records" / "claim_revisions.json"
    doc = json.loads(p.read_text())
    doc["claims"]["c1"] = {"status": "corrupt", "error": "ClaimRevisionIntegrityError: synthetic"}
    p.write_text(json.dumps(doc, indent=2, sort_keys=True))
    (cap / MANIFEST_FILE).write_text(json.dumps(build_manifest(cap), indent=2))


def test_a_claim_corrupt_at_export_gets_no_crate_and_no_working_view(exported, tmp_path):
    """A failed store is not privacy withholding (F7): the exporter refuses the crate instead of declaring partial
    coverage. (Earlier versions wrote a failed-store stub; core still renders one if a bundle carries it.)"""
    cap = _copy(exported[0], tmp_path, "t_corrupt")
    for name in _NEW:
        (cap / name).unlink()
    _mark_claim_corrupt(cap)
    with pytest.raises(CrateExportError, match="other than privacy withholding"):
        export_crate(cap, session_id=SID)
    assert not any((cap / n).exists() for n in _NEW)


def test_a_sealed_row_downgraded_to_legacy_fails_replay(exported, tmp_path):
    """F1 (fault matrix): delete a row's seal and edit its body. Every per-row check treats a row
    with no record as legacy, so only the export-time counts in the manifest remember it had one."""
    cap = _copy(exported[0], tmp_path, "t_downgrade")
    for name in _NEW:
        (cap / name).unlink()                                      # a capsule without a bundle: manifest counts only
    log = json.loads((cap / "run_log.json").read_text())
    rid = next(r for r, row in sorted(log.items()) if isinstance(row.get("record"), dict))
    log[rid].pop("record")
    log[rid]["key_outputs"] = {"edited": 1}
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))
    (cap / "replay.py").write_text(sr.source_text())
    # the attacker repairs the file hashes but not the run_records counts
    m = json.loads((cap / MANIFEST_FILE).read_text())
    fresh = build_manifest(cap)
    m["files"], m["n_files"] = fresh["files"], fresh["n_files"]
    (cap / MANIFEST_FILE).write_text(json.dumps(m, indent=2))
    code, out = _replay(cap, isolated_python=True)
    assert code == 1, out
    assert "does not match the manifest's run_records counts" in out and "v2_records: manifest" in out
    assert "replay_status: not_performed" in out
    # a pristine capsule is unaffected
    assert _replay(exported[0], isolated_python=True)[0] == 0


def test_converter_refuses_a_capsule_with_symlinks(exported, tmp_path):
    src = _copy(exported[0], tmp_path, "t_link_src")
    (src / "linked.txt").symlink_to(tmp_path)                      # points outside the capsule
    with pytest.raises(CrateExportError, match="symlinks"):
        convert_capsule(src, tmp_path / "t_link_out")
    assert not (tmp_path / "t_link_out").exists()


@pytest.mark.skipif(not _CORE_HAS_SESSION, reason="TODO: core does not compare bundle.session_id yet (VER-SESSION)")
def test_exact_rule_equality_needs_core_ver_session(exported, tmp_path):
    """Marker for T3: runs (and then pins exact agreement incl. the session case) once core has VER-SESSION."""
    cap = _copy(exported[0], tmp_path, "t_exact_session")
    _reseal(cap, lambda b: b.update(session_id="another-session"))
    assert _rules(verify_crate(cap)) - {"VER-CRATE-REGEN", "VER-VALIDATE"} == _stdlib_rules(cap)


def test_replacing_a_sealed_row_with_a_forged_one_is_seen_by_the_counts(exported, tmp_path):
    """T1: delete a sealed row and add a forged unsealed-error stand-in; row/v2/legacy counts can stay equal
    while records_with_record_error or matches_exporting_environment change."""
    cap = _copy(exported[0], tmp_path, "t_forged")
    for name in _NEW:
        (cap / name).unlink()
    log = json.loads((cap / "run_log.json").read_text())
    rid = next(r for r, row in sorted(log.items()) if isinstance(row.get("record"), dict)
               and (row["record"].get("env_digest")))
    forged = json.loads(json.dumps(log[rid]))
    forged["record"]["env_digest"] = "sha256:" + "0" * 64          # a forged replacement of the same shape
    log[rid] = forged
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))
    (cap / "replay.py").write_text(sr.source_text())
    m = json.loads((cap / MANIFEST_FILE).read_text())
    fresh = build_manifest(cap)
    m["files"], m["n_files"] = fresh["files"], fresh["n_files"]
    (cap / MANIFEST_FILE).write_text(json.dumps(m, indent=2))
    code, out = _replay(cap, isolated_python=True)
    assert code == 1, out
    assert "matches_exporting_environment: manifest" in out


def test_redaction_alongside_real_sealed_rows_satisfies_the_run_rows_range(exported, tmp_path):
    """Real sealed rows (middleware) plus one privacy-withheld row: sealed <= entries <= sealed + withheld."""
    _forge_path_row(SID, "leg_2", "/Users/zz-someone/secret.csv")
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "mixed"))
    assert not res.get("crate_error"), res
    cap = Path(res["capsule_dir"])
    rr_ = json.loads((cap / "bundle.json").read_text())["run_rows"]
    assert rr_["sealed"] >= 1 and rr_["withheld_for_privacy"] == 1
    assert verify_crate(cap).ok and sr.verify_bundle(cap)["ok"]
    assert _replay(cap, isolated_python=True)[0] == 3


# ---------------------------------------------------------------- fault-matrix-2 follow-ups (M15, M14b, M02e, M06e)
def _forge_run(sid: str, run_id: str, *, record_session: str, mutate=None):
    from ai_hydro.session import run_records as rr
    body = {"run_id": run_id, "tool_name": "legacy_tool", "session_id": sid,
            "timestamp": "2026-01-01T00:00:00+00:00", "key_outputs": {"v": 1.0}}
    rec = rr.build_run_record(run_id=run_id, tool="legacy_tool", session_id=record_session, entry=body)
    row = {**body, "record": rec.to_dict()}
    if mutate:
        mutate(row)
    conn = store._run_log_connect(sid)
    conn.execute("INSERT OR REPLACE INTO runs (run_id, timestamp, entry_json) VALUES (?,?,?)",
                 (run_id, body["timestamp"], json.dumps(row)))
    conn.commit()
    conn.close()


def test_M15_deleting_the_crate_files_fails_replay(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_m15")
    man = json.loads((cap / MANIFEST_FILE).read_text())
    assert man["crate"]["expected"] is True and man["session_id"] == SID
    for name in _NEW:
        (cap / name).unlink()
    code, out = _replay(cap, isolated_python=True)
    assert code == 1 and "bundle.json is missing but the manifest says a crate was written" in out, out
    # just the bundle, or just the BagIt list
    cap2 = _copy(exported[0], tmp_path, "t_m15b")
    (cap2 / "manifest-sha256.txt").unlink()
    assert _replay(cap2, isolated_python=True)[0] == 1
    # claims carried but the manifest marker also removed: still a failure (claim revisions imply a crate)
    cap3 = _copy(exported[0], tmp_path, "t_m15c")
    for name in _NEW:
        (cap3 / name).unlink()
    m3 = json.loads((cap3 / MANIFEST_FILE).read_text())
    m3.pop("crate")
    (cap3 / MANIFEST_FILE).write_text(json.dumps(m3, indent=2))
    assert _replay(cap3, isolated_python=True)[0] == 1


def test_a_failed_crate_is_recorded_so_its_absence_is_not_a_failure(exported, tmp_path, monkeypatch):
    import ai_hydro.capsule.rocrate_export as re_
    import ai_hydro.mcp.tools_session as ts
    monkeypatch.setattr(re_, "to_rocrate", lambda *a, **k: (_ for _ in ()).throw(CrateExportError("synthetic")))
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "nocrate"))
    cap = Path(res["capsule_dir"])
    assert res["crate_error"] == "synthetic" and not (cap / "bundle.json").exists()
    assert json.loads((cap / MANIFEST_FILE).read_text())["crate"]["expected"] is False
    code, out = _replay(cap, isolated_python=True)
    assert code == 0 and "crate was not written" in out, out


def test_M14b_foreign_session_record_is_refused_by_the_exporter_and_by_replay(world, tmp_path):
    server, _tp = world
    _call(server, "extract_hydrological_signatures", {"session_id": SID})
    _session_with_claim()
    _forge_run(SID, "foreign_1", record_session="some-other-session")
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "m14b"))
    cap = Path(res["capsule_dir"])
    assert "records naming another session" in res["crate_error"] and "foreign_1" in res["crate_error"]
    assert not (cap / "bundle.json").exists()
    code, out = _replay(cap, isolated_python=True)          # F8: caught without any bundle
    assert code == 1 and "run foreign_1 names a session other than this capsule's" in out, out


def test_M02e_edited_row_body_with_the_seal_kept_gets_no_crate(world, tmp_path):
    server, _tp = world
    _call(server, "extract_hydrological_signatures", {"session_id": SID})
    _session_with_claim()
    _forge_run(SID, "edited_1", record_session=SID, mutate=lambda r: r["key_outputs"].update(v=2.0))
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "m02e"))
    assert "other than privacy withholding" in res["crate_error"]
    # (the export itself already turned the unbound-body row into a seal_mismatch stub or refused it)


def test_declaring_a_defect_unverifiable_does_not_make_it_partial_coverage(exported, tmp_path, monkeypatch):
    """An attacker with the exporter: pretend a failing row is privacy-withheld so the bundle declares it.
    Core (0.2.6) refuses to project such a bundle, and core verify and the stdlib mirror fail it with the same rules."""
    import ai_hydro.capsule.rocrate_export as re_
    cap = _copy(exported[0], tmp_path, "t_launder")
    for name in _NEW:
        (cap / name).unlink()
    log = json.loads((cap / "run_log.json").read_text())
    rid = next(r for r, row in sorted(log.items()) if isinstance(row.get("record"), dict) and
               row["record"].get("extra", {}).get("entry_digest"))
    log[rid]["key_outputs"] = {**log[rid].get("key_outputs", {}), "tampered": 1}
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))
    (cap / MANIFEST_FILE).write_text(json.dumps({**json.loads((cap / MANIFEST_FILE).read_text()),
                                                 **{k: v for k, v in build_manifest(cap).items()
                                                    if k in ("files", "n_files", "run_records")}}, indent=2))
    real = re_.build_bundle

    def lying(*a, **k):
        bundle, recs, bods, info = real(*a, **k)
        info["redacted_ids"] = sorted(set(info["redacted_ids"]) | {rid})
        return bundle, recs, bods, info

    monkeypatch.setattr(re_, "build_bundle", lying)
    _force_integrity(monkeypatch)
    with pytest.raises((CrateExportError, ValueError)):       # core's to_rocrate refuses the laundered bundle
        export_crate(cap, session_id=SID, live=False)
    # keep the bundle the lying exporter wrote, and compare the two verifiers on it
    with pytest.raises((CrateExportError, ValueError)):
        export_crate(cap, session_id=SID, live=False)
    assert (cap / "bundle.json").exists()
    skipped = {"VER-CRATE-REGEN", "VER-VALIDATE", "VER-BAGIT"}     # no crate/BagIt were written
    core_rules = _rules(verify_crate(cap)) - skipped
    assert "VER-UNVERIFIABLE" in core_rules
    assert core_rules == _stdlib_rules(cap) - skipped, (core_rules, _stdlib_rules(cap))


def test_M06e_session_claim_edited_after_the_last_revision_is_flagged(exported, tmp_path):
    """Replay flags the edit even on a capsule whose crate was regenerated by something that did not refuse."""
    cap = _copy(exported[0], tmp_path, "t_m06e")
    for name in _NEW:
        (cap / name).unlink()
    sess = json.loads((cap / "session.json").read_text())
    sess["claims"]["c1"]["claim"] = sess["claims"]["c1"]["claim"] + " (and much stronger than shown)"
    (cap / "session.json").write_text(json.dumps(sess, indent=2))
    (cap / MANIFEST_FILE).write_text(json.dumps({**json.loads((cap / MANIFEST_FILE).read_text()),
                                                 **{k: v for k, v in build_manifest(cap).items()
                                                    if k in ("files", "n_files", "run_records")}}, indent=2))
    code, out = _replay(cap, isolated_python=True)
    assert code == 1 and "claim c1: session.json differs from the sealed head revision" in out and "claim" in out, out
    assert _replay(exported[0], isolated_python=True)[0] == 0


def test_a_truthy_non_true_privacy_flag_is_not_withheld(exported, tmp_path):
    """T1: only `redacted_for_privacy is True` is a privacy stub; "yes" or 1 is a defect."""
    cap = _copy(exported[0], tmp_path, "t_truthy")
    for name in _NEW:
        (cap / name).unlink()
    log = json.loads((cap / "run_log.json").read_text())
    rid = next(r for r, row in sorted(log.items()) if isinstance(row.get("record"), dict))
    log[rid] = {"run_id": rid, "redacted_for_privacy": "yes", "record_digest": log[rid]["record"]["record_digest"],
                "record": log[rid]["record"]}
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))
    (cap / MANIFEST_FILE).write_text(json.dumps({**json.loads((cap / MANIFEST_FILE).read_text()),
                                                 **{k: v for k, v in build_manifest(cap).items()
                                                    if k in ("files", "n_files", "run_records")}}, indent=2))
    with pytest.raises(CrateExportError, match="other than privacy withholding"):
        export_crate(cap, session_id=SID, live=False)


def test_core_valueerror_becomes_crate_error(exported, tmp_path, monkeypatch):
    import ai_hydro.capsule.rocrate_export as re_
    cap = _copy(exported[0], tmp_path, "t_valueerror")
    for name in _NEW:
        (cap / name).unlink()
    monkeypatch.setattr(re_, "to_rocrate", lambda *a, **k: (_ for _ in ()).throw(ValueError("core says no")))
    with pytest.raises(CrateExportError, match="core refused the crate: core says no"):
        export_crate(cap, session_id=SID, live=False)
    assert not any((cap / n).exists() for n in _NEW)


def test_M06e_head_mismatch_refuses_the_crate(exported, tmp_path):
    """S1: like M02e, a claim differing from its sealed head gets no crate (replay already fails it)."""
    cap = _copy(exported[0], tmp_path, "t_m06e_refuse")
    for name in _NEW:
        (cap / name).unlink()
    sess = json.loads((cap / "session.json").read_text())
    sess["claims"]["c1"]["claim"] += " (stronger)"
    (cap / "session.json").write_text(json.dumps(sess, indent=2))
    (cap / MANIFEST_FILE).write_text(json.dumps({**json.loads((cap / MANIFEST_FILE).read_text()),
                                                 **{k: v for k, v in build_manifest(cap).items()
                                                    if k in ("files", "n_files", "run_records")}}, indent=2))
    with pytest.raises(CrateExportError, match="claims differing from their sealed head"):
        export_crate(cap, session_id=SID, live=False)
    assert not any((cap / n).exists() for n in _NEW)


# ---------------------------------------------------------------- C1: a privacy flag alone launders nothing (a1-a5)
def _refresh_manifest(cap: Path, counts: bool = True):
    keys = ("files", "n_files", "run_records") if counts else ("files", "n_files")
    (cap / MANIFEST_FILE).write_text(json.dumps({**json.loads((cap / MANIFEST_FILE).read_text()),
                                                 **{k: v for k, v in build_manifest(cap).items() if k in keys}}, indent=2))


def _list_redacted(cap: Path, cid: str, revisions):
    """Record in the manifest that the exporter redacted these revisions (what export_session writes)."""
    m = json.loads((cap / MANIFEST_FILE).read_text())
    for e in m["claim_revisions"]["claims"]:
        if e["claim_id"] == cid:
            e["redacted_revisions"] = list(revisions)
    (cap / MANIFEST_FILE).write_text(json.dumps(m, indent=2))


def _list_redacted_run(cap: Path, run_id: str):
    """Record in the manifest that the exporter redacted this run row (privacy.redacted_run_ids)."""
    m = json.loads((cap / MANIFEST_FILE).read_text())
    m.setdefault("privacy", {}).setdefault("redacted_run_ids", []).append(run_id)
    (cap / MANIFEST_FILE).write_text(json.dumps(m, indent=2))


def _first_bound_row(log):
    # a bound row the fixture's claim does NOT cite (r1 is cited, so a forged stub of it would fail VER-STUB-ANCHOR)
    return next(r for r, row in sorted(log.items()) if r != "r1" and isinstance(row.get("record"), dict) and
                row["record"].get("extra", {}).get("entry_digest"))


def _stub_of(row: dict, *, keep_record=True) -> dict:
    rec = row["record"]
    out = {"redacted_for_privacy": True, "run_id": rec["run_id"], "session_id": rec["session_id"],
           "timestamp": row.get("timestamp"), "tool_name": row.get("tool_name"),
           "record_digest": rec["record_digest"], "entry_digest": rec["extra"]["entry_digest"]}
    if keep_record:
        out["record"] = rec
    return out


def _a1(cap):
    log = json.loads((cap / "run_log.json").read_text()); rid = _first_bound_row(log)
    stub = _stub_of(log[rid]); stub["record"] = {**stub["record"], "tool_version": "9.9.9"}   # sealed record edited
    log[rid] = stub
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))


def _a2(cap):
    log = json.loads((cap / "run_log.json").read_text()); rid = _first_bound_row(log)
    log[rid] = {**log[rid], "redacted_for_privacy": True, "key_outputs": {"edited": 1}}      # body kept and edited, flagged
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))


def _a4(cap):
    log = json.loads((cap / "run_log.json").read_text()); rid = _first_bound_row(log)
    log[rid] = {"redacted_for_privacy": True, "run_id": rid, "session_id": log[rid]["record"]["session_id"],
                "record_digest": "sha256:" + "ab" * 32}                                         # invented digest, no record
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))


def _a5(cap):
    log = json.loads((cap / "run_log.json").read_text()); rid = _first_bound_row(log)
    stub = _stub_of(log[rid]); stub["record"] = {**stub["record"], "status": "error"}          # kept record's seal broken
    log[rid] = stub
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))


def _a3(cap):
    p = cap / "records" / "claim_revisions.json"
    doc = json.loads(p.read_text()); rows = doc["claims"]["c1"]["rows"]
    rows[0] = {**rows[0], "content": {**rows[0]["content"], "status": "contested"}}              # seal broken
    rows.append({"redacted_for_privacy": True, "session_id": rows[1]["session_id"], "claim_id": "c1", "revision": 2,
                 "supersedes": rows[1]["revision_digest"], "revision_digest": "sha256:" + "cd" * 32,
                 "record_digest": "sha256:" + "ef" * 32, "recorded_at": "2026-10-03T00:00:00Z"})  # forged stub revision
    p.write_text(json.dumps(doc, indent=2, sort_keys=True))


@pytest.mark.parametrize("name,tamper", [("a1_edited_record_flagged", _a1), ("a2_edited_body_flagged", _a2),
                                         ("a3_claim_edit_plus_forged_stub", _a3), ("a5_stub_keeps_broken_record", _a5)])
def test_a_privacy_flag_alone_does_not_launder_a_defect(exported, tmp_path, monkeypatch, name, tamper):
    import ai_hydro.capsule.rocrate_export as re_
    cap = _copy(exported[0], tmp_path, "t_" + name)
    for n in _NEW:
        (cap / n).unlink()
    tamper(cap)
    _refresh_manifest(cap)
    _force_integrity(monkeypatch)                                    # reach the verifiers, keep the bundle
    with pytest.raises((CrateExportError, ValueError)):                # no crate may be written for a laundered defect
        export_crate(cap, session_id=SID, live=False)
    assert not (cap / "ro-crate-metadata.json").exists()
    skipped = {"VER-CRATE-REGEN", "VER-VALIDATE", "VER-BAGIT"}
    if (cap / "bundle.json").exists():                               # exact rule parity between core and the mirror
        core_rules = _rules(verify_crate(cap)) - skipped
        mine = _stdlib_rules(cap) - skipped
        assert core_rules and core_rules == mine, (name, core_rules, mine)
    code, out = _replay(cap, isolated_python=True)
    assert code == 1, out


def test_a4_a_bare_stub_with_an_invented_digest_is_caught_only_by_the_export_counts(exported, tmp_path):
    """A stub with the stub shape and an invented record_digest cannot be told from a real one by the bundle
    (nothing is carried to check it against); the manifest's export-time run_records counts (F1) are what fail it."""
    cap = _copy(exported[0], tmp_path, "t_a4")
    for n in _NEW:
        (cap / n).unlink()
    _a4(cap)
    _refresh_manifest(cap, counts=False)              # the attacker repairs the hashes, not the export-time counts
    (cap / "replay.py").write_text(sr.source_text())
    code, out = _replay(cap, isolated_python=True)
    assert code == 1 and "run_records counts" in out, out


def test_a_stub_excuses_only_itself_and_siblings_stay_verified(exported, tmp_path):
    """Claim revision 0 withheld; revision 1 (the head) must remain verified, and only the stub is declared unverifiable."""
    cap = _copy(exported[0], tmp_path, "t_sibling")
    for n in _NEW:
        (cap / n).unlink()
    p = cap / "records" / "claim_revisions.json"
    doc = json.loads(p.read_text()); rows = doc["claims"]["c1"]["rows"]
    r = rows[0]
    rows[0] = {"redacted_for_privacy": True, "session_id": r["session_id"], "claim_id": "c1", "revision": r["revision"],
               "revision_digest": r["revision_digest"],
               "record_digest": r["record_digest"], "recorded_at": r["recorded_at"]}
    p.write_text(json.dumps(doc, indent=2, sort_keys=True))
    _refresh_manifest(cap)
    _list_redacted(cap, "c1", [0])
    out = export_crate(cap, session_id=SID, live=False)
    assert out["coverage"]["unverifiable_ids"] == ["c1@0"] and out["coverage"]["withheld_ids"] == ["c1@0"]
    assert verify_crate(cap).ok and sr.verify_bundle(cap)["ok"]


def test_real_redacted_export_with_a_run_row_and_a_claim_revision_both_withheld(world, tmp_path):
    """Real path-bearing sealed rows (run row and claim revision) through export_session."""
    server, _tp = world
    _call(server, "extract_hydrological_signatures", {"session_id": SID})
    _session_with_claim()
    _forge_path_row(SID, "leg_9", "/Users/zz-someone/secret.csv")
    rows = cr.history_readonly(SID)[0]["c1"]["rows"]
    prev = rows[-1]
    from aihydro_core.records import ClaimRevision, digest
    content = {**prev["content"], "limitations": ["data in /Users/zz-someone/private/x.csv"]}
    new = ClaimRevision(session_id=SID, claim_id="c1", revision=prev["revision"] + 1, supersedes=prev["revision_digest"],
                        revision_digest=digest(content), content=content,
                        cause={"tool": "update_claim_status", "reason": "redefined"}, actor=prev["actor"]).seal()
    conn = cr._connect(SID, create=False)
    cr._insert_sealed(conn, new)
    conn.close()
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "both"))
    assert not res.get("crate_error"), res
    cap = Path(res["capsule_dir"])
    cov = json.loads((cap / "bundle.json").read_text())["coverage"]
    assert sorted(cov["withheld_ids"]) == sorted(cov["unverifiable_ids"]) == sorted(["leg_9", f"c1@{new.revision}"])
    assert verify_crate(cap).ok and validate_errors(validate_crate(cap)) == []
    assert _replay(cap, isolated_python=True)[0] == 3 and _replay(cap)[0] == 3
    assert "zz-someone" not in "".join(p.read_text(errors="ignore") for p in cap.rglob("*") if p.is_file())
    if os.environ.get(VALIDATOR_ENV):
        pr = subprocess.run([os.environ[VALIDATOR_ENV], "validate", "--profile-identifier", "ro-crate-1.3",
                             "--requirement-severity", "REQUIRED", "--no-paging", str(cap)],
                            capture_output=True, text=True, timeout=600)
        assert pr.returncode == 0, pr.stdout[-1500:]


# ---------------------------------------------------------------- B4 / b3 / b6 and VER-STUB-ANCHOR
def _forged_stub_head(cap: Path, *, edit_claim: bool = True):
    """b4: edit the session claim, append a shape-valid stub revision on top of the real head."""
    p = cap / "records" / "claim_revisions.json"
    doc = json.loads(p.read_text()); rows = doc["claims"]["c1"]["rows"]; head = rows[-1]
    rows.append({"redacted_for_privacy": True, "session_id": head["session_id"], "claim_id": "c1",
                 "revision": head["revision"] + 1, "supersedes": head["revision_digest"],
                 "revision_digest": "sha256:" + "cd" * 32, "record_digest": "sha256:" + "ef" * 32,
                 "recorded_at": "2026-10-03T00:00:00Z"})
    p.write_text(json.dumps(doc, indent=2, sort_keys=True))
    if edit_claim:
        sess = json.loads((cap / "session.json").read_text())
        sess["claims"]["c1"]["claim"] += " (much stronger)"
        (cap / "session.json").write_text(json.dumps(sess, indent=2))
    _refresh_manifest(cap)


def test_b4_a_stub_head_does_not_excuse_the_comparison(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_b4")
    for n in _NEW:
        (cap / n).unlink()
    _forged_stub_head(cap)
    lines = []
    bad = sr.check_claims_against_heads(cap, out=lines.append)
    assert bad == [("c1", ["claim"])]
    text = "\n".join(lines)
    assert "head withheld; comparison limited to revision 1" in text and "FAIL  claim c1" in text
    # the exporter refuses a stub the manifest does not list as redacted at export
    with pytest.raises(CrateExportError, match="not list as redacted"):
        export_crate(cap, session_id=SID, live=False)
    # even if the attacker also lists it in the manifest, the claim edit is still refused
    _list_redacted(cap, "c1", [2])
    with pytest.raises(CrateExportError, match="claims differing from their sealed head"):
        export_crate(cap, session_id=SID, live=False)


def test_b4_a_scrubbed_session_value_is_not_a_difference(exported, tmp_path):
    """A legitimately withheld head: the session value was scrubbed (carries a marker), so it may differ."""
    cap = _copy(exported[0], tmp_path, "t_b4_scrub")
    for n in _NEW:
        (cap / n).unlink()
    _forged_stub_head(cap, edit_claim=False)
    sess = json.loads((cap / "session.json").read_text())
    head_text = json.loads((cap / "records" / "claim_revisions.json").read_text())["claims"]["c1"]["rows"][1]["content"]["text"]
    sess["claims"]["c1"]["claim"] = head_text + " <abs>/data.csv"        # only a marker-bearing token differs
    (cap / "session.json").write_text(json.dumps(sess, indent=2))
    assert sr.check_claims_against_heads(cap, out=lambda *_a: None) == []


def test_S9_a_marker_excuses_only_the_marker_bearing_substring(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_s9")
    for n in _NEW:
        (cap / n).unlink()
    _forged_stub_head(cap, edit_claim=False)
    sess = json.loads((cap / "session.json").read_text())
    sess["claims"]["c1"]["claim"] = "Synthetic NSE is 0.9 (notes: <abs>/n.txt)"        # edited AND marker appended
    (cap / "session.json").write_text(json.dumps(sess, indent=2))
    assert sr.check_claims_against_heads(cap, out=lambda *_a: None) == [("c1", ["claim"])]


def test_b3_all_revisions_stubbed_and_unlisted_is_refused(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_b3")
    for n in _NEW:
        (cap / n).unlink()
    p = cap / "records" / "claim_revisions.json"
    doc = json.loads(p.read_text())
    doc["claims"]["c1"]["rows"] = [
        {"redacted_for_privacy": True, "session_id": r["session_id"], "claim_id": "c1", "revision": r["revision"],
         **({"supersedes": r["supersedes"]} if r.get("supersedes") else {}),
         "revision_digest": r["revision_digest"], "record_digest": r["record_digest"],
         "recorded_at": r["recorded_at"]} for r in doc["claims"]["c1"]["rows"]]
    p.write_text(json.dumps(doc, indent=2, sort_keys=True))
    _refresh_manifest(cap)
    with pytest.raises(CrateExportError, match="not list as redacted"):
        export_crate(cap, session_id=SID, live=False)


def test_b6_an_a4_shaped_capsule_gets_no_crate_at_not_performed(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_b6")
    for n in _NEW:
        (cap / n).unlink()
    _a4(cap)
    _refresh_manifest(cap, counts=False)          # hashes repaired, export-time counts not
    log = json.loads((cap / "run_log.json").read_text())
    _list_redacted_run(cap, next(r for r, row in log.items() if row.get("redacted_for_privacy")))   # listed: only the counts fail
    with pytest.raises(CrateExportError, match="fails its own integrity checks"):
        export_crate(cap, session_id=SID, live=False)
    assert not any((cap / n).exists() for n in _NEW)


def _rechain_with_seals(cap: Path, run_id: str, record_digest: str):
    """Replace claim c1's chain by one whose revisions bind ``run_id`` to ``record_digest`` in evidence_seals."""
    from aihydro_core.records import ClaimRevision, digest
    p = cap / "records" / "claim_revisions.json"
    doc = json.loads(p.read_text()); old = doc["claims"]["c1"]["rows"]
    rows, prev = [], None
    for r in old:
        content = {**r["content"], "evidence_seals": {run_id: record_digest}}
        rev = ClaimRevision(session_id=r["session_id"], claim_id="c1", revision=r["revision"],
                            supersedes=prev, revision_digest=digest(content), content=content,
                            cause=r["cause"], actor=r["actor"], recorded_at=r["recorded_at"]).seal()
        rows.append(rev.to_dict()); prev = rev.revision_digest
    doc["claims"]["c1"]["rows"] = rows
    p.write_text(json.dumps(doc, indent=2, sort_keys=True))


def test_a4_on_a_cited_row_fails_stub_anchor_with_core_parity(exported, tmp_path, monkeypatch):
    cap = _copy(exported[0], tmp_path, "t_anchor")
    for n in _NEW:
        (cap / n).unlink()
    log = json.loads((cap / "run_log.json").read_text()); rid = _first_bound_row(log)
    real_digest = log[rid]["record"]["record_digest"]
    _rechain_with_seals(cap, rid, real_digest)
    _a4(cap)                                       # bare stub, invented digest, on a row the claim cites
    _refresh_manifest(cap)
    _list_redacted_run(cap, rid)
    _force_integrity(monkeypatch)
    with pytest.raises((CrateExportError, ValueError)):
        export_crate(cap, session_id=SID, live=False)
    skipped = {"VER-CRATE-REGEN", "VER-VALIDATE", "VER-BAGIT"}
    core_rules = _rules(verify_crate(cap)) - skipped
    assert "VER-STUB-ANCHOR" in core_rules and core_rules == _stdlib_rules(cap) - skipped


def test_the_genuine_stub_of_a_cited_row_verifies_and_an_uncited_a4_is_the_documented_limit(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_anchor_ok")
    for n in _NEW:
        (cap / n).unlink()
    log = json.loads((cap / "run_log.json").read_text()); rid = _first_bound_row(log)
    _rechain_with_seals(cap, rid, log[rid]["record"]["record_digest"])
    log[rid] = _stub_of(log[rid])                  # a genuine stub: the real digest, the real record
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))
    _refresh_manifest(cap)
    _list_redacted_run(cap, rid)
    _list_redacted(cap, "c1", [])
    out = export_crate(cap, session_id=SID, live=False)
    assert rid in out["coverage"]["withheld_ids"] and verify_crate(cap).ok and sr.verify_bundle(cap)["ok"]
    # limit: the same invented-digest stub on a row NO revision cites passes every verifier (documented)
    cap2 = _copy(exported[0], tmp_path, "t_uncited")
    for n in _NEW:
        (cap2 / n).unlink()
    _a4(cap2)
    _refresh_manifest(cap2)                        # the attacker repairs every export-time count too
    _list_redacted_run(cap2, next(r for r, row in json.loads((cap2 / "run_log.json").read_text()).items()
                                  if row.get("redacted_for_privacy")))
    export_crate(cap2, session_id=SID, live=False)
    assert verify_crate(cap2).ok and sr.verify_bundle(cap2)["ok"]


def test_real_export_with_a_cited_run_row_withheld_verifies_with_the_genuine_stub(world, tmp_path):
    """The claim cites a sealed, path-bearing run row; export withholds it as a stub; the claim's evidence_seals
    names the same record digest, so VER-STUB-ANCHOR holds (core verify, validator, stdlib replay all pass)."""
    from ai_hydro.session import run_records as rr
    server, _tp = world
    _call(server, "extract_hydrological_signatures", {"session_id": SID})
    body = {**_run_record(SID), "run_id": "cited_1",
            "timestamp": "2026-01-01T00:00:00+00:00"}
    body["key_outputs"] = {"nse": 0.8, "source_file": "/Users/zz-someone/secret.csv"}
    rec = rr.build_run_record(run_id="cited_1", tool="fixture", session_id=SID, entry=body)
    conn = store._run_log_connect(SID)
    conn.execute("INSERT OR REPLACE INTO runs (run_id, timestamp, entry_json) VALUES (?,?,?)",
                 ("cited_1", body["timestamp"], json.dumps({**body, "record": rec.to_dict()})))
    conn.commit()
    conn.close()
    s = HydroSession.load(SID)
    assert add_claim(
        session_id=SID, claim_id="c2", statement="Synthetic NSE is 0.8", claim_type="empirical_result",
        status="proposed", confidence="low", confidence_rationale="Synthetic regression fixture only.",
        basins=["synthetic"], period="2000-2001", metric="nse", basin_refs=[basin_ref_full("synthetic")],
        limitations=["Synthetic regression case, no real research conclusion."],
        evidence_spans=[{"source_type": "run", "source_id": "cited_1", "metric_ref": "nse"}])["status"] == "recorded"
    rows = cr.history_readonly(SID)[0]["c2"]["rows"]
    assert (rows[-1]["content"].get("evidence_seals") or {}).get("cited_1") == rec.record_digest
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "cited"))
    assert not res.get("crate_error"), res
    cap = Path(res["capsule_dir"])
    cov = json.loads((cap / "bundle.json").read_text())["coverage"]
    assert "cited_1" in cov["withheld_ids"] and verify_crate(cap).ok and sr.verify_bundle(cap)["ok"]
    assert validate_errors(validate_crate(cap)) == []
    assert _replay(cap, isolated_python=True)[0] == 3 and _replay(cap)[0] == 3


# ---------------------------------------------------------------- FP1: concrete-prefix normalisation at write time
def _home() -> str:
    return str(Path.home())


def _add_pathy_claim(cid="c_path", text=None, rationale=None, limitations=None):
    h = _home()
    return add_claim(
        session_id=SID, claim_id=cid,
        statement=text or f"Baseflow index from {h}/work/notes.txt is 0.58 +/- 0.01 mm/day",
        claim_type="empirical_result", status="proposed", confidence="low",
        confidence_rationale=rationale or f"Synthetic fixture; see {h}/work/notes.txt for the CI derivation.",
        basins=["synthetic"], period="2000-2001", metric="nse", basin_refs=[basin_ref_full("synthetic")],
        limitations=limitations or [f"Synthetic regression case; data under {h}/work/ only."],
        evidence_spans=[{"source_type": "run", "source_id": "r1", "metric_ref": "nse"}])


def test_concrete_prefixes_are_replaced_before_sealing_and_the_crate_is_written(exported, tmp_path):
    res = _add_pathy_claim()
    assert res["status"] == "recorded" and sorted(res["paths_scrubbed"]) == ["claim", "confidence_rationale",
                                                                           "limitations"]
    assert "paths_remaining" not in res
    claim = HydroSession.load(SID).claims["c_path"]
    assert _home() not in json.dumps(claim)
    assert claim["claim"] == "Baseflow index from <home>/work/notes.txt is 0.58 +/- 0.01 mm/day"
    rows = cr.history_readonly(SID)[0]["c_path"]["rows"]
    assert len(rows) == 1 and _home() not in json.dumps(rows) and "redacted_for_privacy" not in rows[0]
    assert rows[0]["cause"].get("path_scrub")                      # a truthful cause, never out_of_band_edit
    from aihydro_core.records import verify_claim_revision_dict
    assert verify_claim_revision_dict(rows[0])
    r2 = update_claim_status(SID, "c_path", "proposed", "medium", f"Now also see {_home()}/other/place.csv")
    assert r2["paths_scrubbed"] == ["confidence_rationale"]
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "scrubbed"))
    assert not res.get("crate_error"), res                         # M2: the placeholders pass core's validator
    cap = Path(res["capsule_dir"])
    doc = json.loads((cap / "records" / "claim_revisions.json").read_text())
    assert not any(r.get("redacted_for_privacy") for r in doc["claims"]["c_path"]["rows"])
    assert _home() not in "".join(p.read_text(errors="ignore") for p in cap.rglob("*") if p.is_file())
    assert verify_crate(cap).ok and validate_errors(validate_crate(cap)) == []
    assert _replay(cap, isolated_python=True)[0] == 0 and _replay(cap)[0] == 0
    from ai_hydro.approval.records import session_claim_revision
    s = HydroSession.load(SID)
    assert cr.revision_drift(session_claim_revision(s, "c_path")[2], cr.latest(SID, "c_path"))["state"] == "in_sync"


def test_M1_a_claim_that_starts_with_a_path_keeps_its_statement(exported):
    t = f"{_home()}/run1/out.nc gives a Lyne-Hollick baseflow index of 0.585 with Q in m3/s and BFI 0.585 +/- 0.014"
    _add_pathy_claim("whole2", text=t, rationale="Synthetic fixture rationale text.", limitations=["plain"])
    stored = HydroSession.load(SID).claims["whole2"]["claim"]
    assert stored == "<home>/run1/out.nc gives a Lyne-Hollick baseflow index of 0.585 with Q in m3/s and BFI 0.585 +/- 0.014"


def test_a_path_that_cannot_be_normalised_is_refused_and_nothing_is_recorded(exported):
    before = cr.history_readonly(SID)[0].get("c_odd")
    res = _add_pathy_claim("c_odd", text="Derived from /Users/zz-someone/elsewhere/x.csv with NSE 0.8",
                           rationale="Synthetic fixture rationale text.", limitations=["plain"])
    assert res["error"] is True and res["code"] == "LOCAL_PATH_IN_TEXT" and res["field"] == "claim"
    assert res["offending_text"] == "/Users/zz-someone/elsewhere/x.csv" and "rephrase" in res["recovery"].lower()
    assert "c_odd" not in HydroSession.load(SID).claims and cr.history_readonly(SID)[0].get("c_odd") == before


def test_redef_a_refused_redefinition_changes_nothing_and_an_honest_one_exports(exported, tmp_path):
    ok = _add_pathy_claim("c_red", text="Synthetic NSE is 0.8 with a plain statement", rationale="Synthetic fixture rationale text.",
                          limitations=["plain"])
    assert ok["status"] == "recorded"
    n = len(cr.history_readonly(SID)[0]["c_red"]["rows"])
    session_before = json.dumps(HydroSession.load(SID).claims["c_red"], sort_keys=True)
    bad = _add_pathy_claim("c_red", text="Synthetic NSE is 0.8, see /Users/zz-someone/q/notes.txt",
                           rationale="Synthetic fixture rationale text.", limitations=["plain"])
    assert bad["code"] == "LOCAL_PATH_IN_TEXT"
    assert len(cr.history_readonly(SID)[0]["c_red"]["rows"]) == n
    assert json.dumps(HydroSession.load(SID).claims["c_red"], sort_keys=True) == session_before
    r2 = update_claim_status(SID, "c_red", "proposed", "medium", "Rationale with /Users/zz-someone/p/q.txt inside")
    assert r2["code"] == "LOCAL_PATH_IN_TEXT" and r2["field"] == "confidence_rationale"
    assert len(cr.history_readonly(SID)[0]["c_red"]["rows"]) == n
    good = _add_pathy_claim("c_red", text="Synthetic NSE is 0.9 with a plain redefinition",
                            rationale="Synthetic fixture rationale text.", limitations=["plain"])
    assert good["status"] == "recorded"
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "redef"))
    assert not res.get("crate_error"), res
    assert verify_crate(Path(res["capsule_dir"])).ok


def test_N1_nested_placeholders_are_a_boundary(monkeypatch, tmp_path):
    from ai_hydro.session.claim_paths import scrub_claim_text
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "ah"))
    ws = str(tmp_path / "ws")
    once = scrub_claim_text(f"{ws}/tmp/out.nc and {tmp_path}/home/x", ws)
    assert once.startswith("<workspace>/tmp/out.nc") and scrub_claim_text(once, ws) == once


def test_N2_a_relative_workspace_is_not_a_word_prefix(monkeypatch, tmp_path):
    from ai_hydro.session.claim_paths import scrub_claim_text
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIHYDRO_HOME", "relative-ah")
    for rel in ("results", "data", ".", "/", "/a"):
        assert scrub_claim_text("The results show data are fine in /a/b", rel) == "The results show data are fine in /a/b"


def test_S13_the_shared_predicate_is_case_insensitive_and_names_what_is_outside_it():
    from ai_hydro.session.claim_paths import find_local_path
    assert find_local_path("read /users/alice/x.csv today") == "/users/alice/x.csv"
    assert find_local_path("see ~/notes/ci.txt") and find_local_path("under $HOME/work")
    for ok in ("HDF-like /data/x.csv is institutional", "ratio A:B", "mm/day", "0.58 +/- 0.01", "/group/var"):
        assert find_local_path(ok) is None, ok       # /data/... style institutional mounts are outside the heuristic
    assert find_local_path("HDF /var/streamflow") is not None    # accepted consequence: rephrase it
    for tilde in ("~mm/day", "~A/L", "~PET/P", "~m3/s", "~0.95/day"):
        assert find_local_path(tilde) is None, tilde            # core 0.2.8: only ~/ and $HOME are home


def _assert_exporter_does_not_stub_the_head(tmp_path, name="probe"):
    """For an accepted write: the exporter's own collection must not turn any revision into a privacy stub."""
    from ai_hydro.capsule.claim_records import collect_claim_revisions
    entries, counts = collect_claim_revisions(SID, tmp_path / name, HydroSession.load(SID).workspace_dir)
    assert counts["redacted"] == 0 and all(not e["redacted_revisions"] for e in entries), entries


def test_N4_a_path_like_evidence_id_is_refused_and_ids_are_never_rewritten(exported, tmp_path):
    ok = _add_pathy_claim("ev", text="Synthetic NSE is 0.8 plain statement", rationale="Synthetic fixture rationale text.",
                          limitations=["plain"])
    assert ok["status"] == "recorded"
    n = len(cr.history_readonly(SID)[0]["ev"]["rows"])
    before = json.dumps(HydroSession.load(SID).claims["ev"], sort_keys=True)
    bad = add_claim(session_id=SID, claim_id="ev", statement="Synthetic NSE is 0.8 reworded statement",
                    claim_type="empirical_result", status="proposed", confidence="low",
                    confidence_rationale="Synthetic fixture rationale text.", basins=["synthetic"], period="2000-2001",
                    metric="nse", basin_refs=[basin_ref_full("synthetic")], limitations=["plain"],
                    evidence_spans=[{"source_type": "run", "source_id": "r1", "metric_ref": "nse"},
                                    {"source_type": "dataset", "source_id": "/Volumes/Shared/alice/t.csv"}])
    assert bad["code"] == "LOCAL_PATH_IN_TEXT" and "source_id" in bad["field"] and bad["field"].startswith("evidence_spans[")
    assert bad["offending_text"] == "/Volumes/Shared/alice/t.csv"
    assert len(cr.history_readonly(SID)[0]["ev"]["rows"]) == n
    assert json.dumps(HydroSession.load(SID).claims["ev"], sort_keys=True) == before
    _assert_exporter_does_not_stub_the_head(tmp_path)


ACCEPT_OR_REFUSE = [
    "Plain statement about NSE 0.8", "NSE is 0.8 +/- 0.1 mm/day", "about ~mm/day and ~PET/P and ~m3/s",
    "ratio Q5/Q95 and dQ/dt", "see https://doi.org/10.1000/xyz", "approximately ~0.95/day",
    "HDF /var/streamflow", "A:/B ratio", "//NLDAS/FORA group", "read from ~/notes.txt", "uses $HOME/work",
    "Windows C:\\data\\x.csv", "/etc/hosts", "under /Volumes/Shared/x",
]


@pytest.mark.parametrize("text", ACCEPT_OR_REFUSE)
def test_every_accepted_write_is_never_stubbed_by_the_exporter(exported, tmp_path, text):
    res = _add_pathy_claim("prop", text=text, rationale="Synthetic fixture rationale text.", limitations=["plain"])
    if res.get("error"):
        assert res["code"] == "LOCAL_PATH_IN_TEXT" and "prop" not in HydroSession.load(SID).claims
        assert not res["offending_text"].endswith((".", ",", ";", ":"))
    else:
        _assert_exporter_does_not_stub_the_head(tmp_path)


def test_a_legacy_claim_with_an_unnormalisable_path_points_to_add_claim(exported):
    _store_legacy_claim("legx")
    s = HydroSession.load(SID)
    s.claims["legx"]["claim"] = "Derived from /Volumes/Shared/alice/x.csv with NSE 0.8"
    s.save()
    r = update_claim_status(SID, "legx", "retracted", "low", "Synthetic regression fixture only.")
    assert r["code"] == "LOCAL_PATH_IN_TEXT" and r["field"] == "claim" and "add_claim" in r["recovery"]
    # the sound route: re-issue the claim with rephrased text (retracted is a valid status there), then it is writable
    ok = _add_pathy_claim("legx", text="Derived from the shared dataset with NSE 0.8", rationale="Synthetic fixture rationale text.",
                          limitations=["plain"])
    assert ok["status"] == "recorded"
    assert update_claim_status(SID, "legx", "retracted", "low", "Synthetic regression fixture only.")["status"] == "updated"


def test_S9_a_marker_must_start_its_token_and_placeholders_do_not_excuse(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_s9b")
    for n in _NEW:
        (cap / n).unlink()
    _forged_stub_head(cap, edit_claim=False)
    head_text = json.loads((cap / "records" / "claim_revisions.json").read_text())["claims"]["c1"]["rows"][1]["content"]["text"]
    for edited in (head_text.replace(" is ", " is not<tmp> "),          # marker glued to a word: not a token start
                   head_text + " <tmp>/x",                              # write-time placeholder: no honest stub carries it
                   head_text + " <workspace>/x"):
        sess = json.loads((cap / "session.json").read_text())
        sess["claims"]["c1"]["claim"] = edited
        (cap / "session.json").write_text(json.dumps(sess, indent=2))
        assert sr.check_claims_against_heads(cap, out=lambda *_a: None) == [("c1", ["claim"])], edited
    sess["claims"]["c1"]["claim"] = head_text + " <abs>/x"               # a legacy marker token is still excused
    (cap / "session.json").write_text(json.dumps(sess, indent=2))
    assert sr.check_claims_against_heads(cap, out=lambda *_a: None) == []


SCIENTIFIC = ["HDF /var/streamflow", "zarr /root/precip", "/home/range/q", "/opt/mean", "group /private/obs",
              "A:/B ratio", "Q in m3/s and mm/day", "kg/m2/s", "a/b", "BFI 0.585 +/- 0.014", "/group/var",
              "/signatures/baseflow_index", "https://doi.org/10.1000/xyz", "s3://bucket/key", "USGS 01013500",
              "C:N ratio", "2000-10-01/2009-09-30", "at:/Users/alice/x.csv", "/users/alice/x.csv", "/data/x.csv"]


@pytest.mark.parametrize("text", SCIENTIFIC)
def test_scientific_strings_are_unchanged(text, monkeypatch, tmp_path):
    from ai_hydro.session.claim_paths import scrub_claim_text
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "ah"))
    assert scrub_claim_text(text, str(tmp_path / "ws")) == text


def test_scrub_is_a_fixpoint_and_concrete_prefixes_leave_no_local_path(monkeypatch, tmp_path):
    import random, tempfile
    from aihydro_core.export.rocrate import PATH_PATTERN
    from ai_hydro.session.claim_paths import concrete_prefixes, scrub_claim_text
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "ah"))
    ws = str(tmp_path / "ws")
    prefixes = [p for p, _t in concrete_prefixes(ws)] + [tempfile.gettempdir(), ws + "/tmp", ws + "/sub/home"]
    rng = random.Random(7)
    words = ["see", "NSE", "0.58", "+/-", "m3/s", "in", "(", ")", "and", "notes.txt", ",", ";",
             "<workspace>/tmp/out.nc", "<home>/tmp/x", "<tmp>/home/y"]
    for _ in range(400):
        parts = []
        for _i in range(rng.randint(1, 8)):
            parts.append(rng.choice(prefixes) + "/" + rng.choice(["a.csv", "x/y.txt", "run1"]) if rng.random() < 0.5
                         else rng.choice(words))
        x = " ".join(parts)
        y = scrub_claim_text(x, ws)
        assert scrub_claim_text(y, ws) == y, x                     # fixpoint
        assert PATH_PATTERN.search(y) is None, (x, y)              # only concrete prefixes: nothing left
def test_approval_and_promotion_round_trip_on_a_normalised_claim(exported):
    from approval_helpers import approve
    from ai_hydro.mcp.tools_ledger import promote_claim_to_registry
    _add_pathy_claim("c_promo")
    update_claim_status(SID, "c_promo", "supported", "medium", f"Synthetic fixture only; notes at {_home()}/n.txt",
                        uncertainty_verified=True)
    approve(SID, "c_promo")
    out = promote_claim_to_registry(SID, "c_promo", researcher_approved=True)
    assert not out.get("error"), out


def _store_legacy_claim(cid="leg"):
    """A claim written BEFORE the write-time normalisation: raw path in the stored claim and its sealed revision."""
    from ai_hydro.approval.records import session_claim_revision
    s = HydroSession.load(SID)
    claim = _legacy_claim_dict(cid, f"{_home()}/old/notes.txt holds the CI, NSE 0.8")
    s.claims[cid] = claim
    s.save()
    s = HydroSession.load(SID)
    fields = session_claim_revision(s, cid)[2]
    cr.record_change(SID, cid, fields, tool="add_claim", reason="created")
    return s


def _legacy_claim_dict(cid, text):
    return {"id": cid, "claim": text, "claim_type": "empirical_result", "status": "proposed", "confidence": "low",
            "confidence_rationale": "Synthetic regression fixture only.",
            "scope": {"basins": ["synthetic"], "period": "2000-2001", "forcing": None, "metric": "nse",
                      "model_versions": {}, "basin_refs": [{"id": basin_ref_full("synthetic")["id"], "label": "synthetic"}]},
            "evidence_spans": [{"source_type": "run", "source_id": "r1", "metric_ref": "nse", "page": None,
                                "passage_hash": None}],
            "limitations": ["Synthetic regression case, no real research conclusion."], "contradictions": [],
            "basin_ref_records": {basin_ref_full("synthetic")["id"]: basin_ref_full("synthetic")}}


def test_M3_legacy_claims_stay_in_sync_and_keep_their_approval(exported):
    """The projection is pure: a claim stored with a raw path before this change does not read as drifted."""
    from approval_helpers import approve
    from ai_hydro.approval.records import session_claim_revision
    from ai_hydro.mcp.tools_ledger import promote_claim_to_registry
    s = _store_legacy_claim()
    assert cr.revision_drift(session_claim_revision(s, "leg")[2], cr.latest(SID, "leg"))["state"] == "in_sync"
    approve(SID, "leg")
    out = promote_claim_to_registry(SID, "leg", researcher_approved=True)
    reasons = [r["cause"]["reason"] for r in cr.history_readonly(SID)[0]["leg"]["rows"]]
    assert "out_of_band_edit" not in reasons, reasons
    assert out.get("status") == "promoted" or out.get("code") != "APPROVAL_REQUIRED", out


def test_M3_a_legacy_claim_is_normalised_at_its_next_write_and_exports(exported, tmp_path):
    _store_legacy_claim()
    r = update_claim_status(SID, "leg", "proposed", "medium", "Synthetic regression fixture only.")
    assert r["paths_scrubbed"] == ["claim"]
    rows = cr.history_readonly(SID)[0]["leg"]["rows"]
    assert rows[-1]["cause"].get("path_scrub") == ["claim"] and rows[-1]["cause"]["reason"] == "status_update"
    assert _home() not in json.dumps(rows[-1]) and "<home>/old/notes.txt" in rows[-1]["content"]["text"]
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "legacy"))
    assert not res.get("crate_error"), res                          # head is now readable; the old revision is a stub
    cap = Path(res["capsule_dir"])
    assert _replay(cap, isolated_python=True)[0] == 3 and verify_crate(cap).ok


def _legacy_stub_head(world_tmp, status="contested"):
    """A LEGACY withheld head: a sealed revision whose content held a path (written before the write-time scrub)."""
    from aihydro_core.records import ClaimRevision, digest
    prev = cr.history_readonly(SID)[0]["c1"]["rows"][-1]
    content = {**prev["content"], "status": status, "confidence_rationale": "legacy note at /Users/zz-someone/x.txt"}
    new = ClaimRevision(session_id=SID, claim_id="c1", revision=prev["revision"] + 1, supersedes=prev["revision_digest"],
                        revision_digest=digest(content), content=content,
                        cause={"tool": "update_claim_status", "reason": "status_update"}, actor=prev["actor"]).seal()
    conn = cr._connect(SID, create=False)
    cr._insert_sealed(conn, new)
    conn.close()
    s = HydroSession.load(SID)
    s.claims["c1"]["status"] = status
    s.claims["c1"]["confidence_rationale"] = "legacy note at <abs>/x.txt"
    s.save()
    return new


def test_FP2_honest_legacy_withheld_head_that_also_changed_status_is_exported_and_replays(exported, tmp_path):
    _legacy_stub_head(tmp_path)
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "fp2"))
    assert not res.get("crate_error"), res
    cap = Path(res["capsule_dir"])
    code, out = _replay(cap, isolated_python=True)
    assert code == 3, out
    assert "head withheld; comparison limited to revision 1" in out and "unverifiable: head withheld" in out
    assert verify_crate(cap).ok
    crate = (cap / "ro-crate-metadata.json").read_text()
    assert "contested" not in crate                       # the session's status is not projected for a withheld head


def test_FP2b_a_text_edit_behind_a_legacy_stub_head_still_fails(exported, tmp_path):
    new = _legacy_stub_head(tmp_path)
    s = HydroSession.load(SID)
    s.claims["c1"]["claim"] += " (stronger)"
    s.save()
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "fp2b"))
    assert "claims differing from their sealed head" in str(res.get("crate_error"))


def test_S4_a_run_stub_the_manifest_does_not_list_is_refused(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_s4")
    for n in _NEW:
        (cap / n).unlink()
    _a4(cap)
    _refresh_manifest(cap)                                # every count repaired; privacy.redacted_run_ids still empty
    with pytest.raises(CrateExportError, match="privacy stub the manifest does not list"):
        export_crate(cap, session_id=SID, live=False)


def test_the_refusal_recovery_is_field_aware(exported):
    prose = _add_pathy_claim("m_prose", text="Derived from /Volumes/Shared/a/x.csv with NSE 0.8",
                             rationale="Synthetic fixture rationale text.", limitations=["plain"])
    assert prose["field"] == "claim" and "Rephrase" in prose["recovery"] and "named dataset" in prose["recovery"]
    ev = add_claim(session_id=SID, claim_id="m_ev", statement="Synthetic NSE is 0.8 plain statement",
                   claim_type="empirical_result", status="proposed", confidence="low",
                   confidence_rationale="Synthetic fixture rationale text.", basins=["synthetic"], period="2000-2001",
                   metric="nse", basin_refs=[basin_ref_full("synthetic")], limitations=["plain"],
                   evidence_spans=[{"source_type": "dataset", "source_id": "/Volumes/Shared/alice/t.csv"}])
    assert ev["field"] == "evidence_spans[0].source_id"
    assert "identifier, not a local file path" in ev["recovery"] and "run id, a DOI or a named dataset" in ev["recovery"]
    assert "Rephrase" not in ev["recovery"] and "cite the file" not in ev["recovery"].lower()
    sc = add_claim(session_id=SID, claim_id="m_sc", statement="Synthetic NSE is 0.8 plain statement",
                   claim_type="empirical_result", status="proposed", confidence="low",
                   confidence_rationale="Synthetic fixture rationale text.", basins=["/Volumes/Shared/basin"],
                   period="2000-2001", metric="nse", limitations=["plain"],
                   evidence_spans=[{"source_type": "run", "source_id": "r1", "metric_ref": "nse"}])
    assert sc["code"] == "LOCAL_PATH_IN_TEXT" and sc["field"].startswith("scope") and "not file paths" in sc["recovery"]
    assert "m_prose" not in HydroSession.load(SID).claims and "m_ev" not in HydroSession.load(SID).claims


# ---------------------------------------------------------------- F13: three-way outcome
def test_F13_outcome_ok_partial_failed_and_the_machine_readable_line(exported, tmp_path):
    code, out = _replay(exported[0], isolated_python=True)
    assert code == 0 and "replay_outcome: ok\n" in out
    cap = _copy(exported[0], tmp_path, "t_f13")
    (cap / "README.md").write_text("tampered")
    code, out = _replay(cap, isolated_python=True)
    assert code == 1 and "replay_outcome: failed" in out


def test_F13_a_partial_export_is_reported_as_partial_by_export_session_and_replay(world, tmp_path):
    server, _tp = world
    _call(server, "extract_hydrological_signatures", {"session_id": SID})
    _session_with_claim()
    _forge_path_row(SID, "leg_f13", "/Users/zz-someone/secret.csv")
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "f13"))
    assert res["crate_status"] == "ok_partial_withheld" and res["crate_partial"] is True
    cap = Path(res["capsule_dir"])
    core = verify_crate(cap)
    assert core.status == "ok_partial_withheld" and core.exit_code == 3 and core.ok and core.partial
    for kw in ({"isolated_python": True}, {}):
        code, out = _replay(cap, **kw)
        assert code == 3 and "replay_outcome: ok_partial_withheld" in out, out
    # the same agreement for a complete export
    clean = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "f13b"))
    assert clean["crate_status"] in ("ok", "ok_partial_withheld")


def test_F11_a_missing_bagit_manifest_fails_in_core_and_the_mirror(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_f11")
    (cap / "manifest-sha256.txt").unlink()
    assert "VER-BAGIT" in _rules(verify_crate(cap)) and "VER-BAGIT" in _stdlib_rules(cap)


def test_F12_session_view_vs_sealed_head_has_core_parity(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_f12")
    sess = json.loads((cap / "session.json").read_text())
    sess["claims"]["c1"]["claim"] += " (stronger)"
    (cap / "session.json").write_text(json.dumps(sess, indent=2))
    _refresh(cap)
    # the bundle still pins the old session.json digest: re-export so only the head comparison is in play
    for n in _NEW:
        (cap / n).unlink()
    _refresh_manifest(cap)
    import ai_hydro.capsule.rocrate_export as re_
    mp = pytest.MonkeyPatch()
    try:
        _force_integrity(mp)
        with pytest.raises((CrateExportError, ValueError)):
            export_crate(cap, session_id=SID, live=False)
    finally:
        mp.undo()
    skipped = {"VER-CRATE-REGEN", "VER-VALIDATE", "VER-BAGIT"}
    assert "VER-HEAD" in _rules(verify_crate(cap)) and _rules(verify_crate(cap)) - skipped == _stdlib_rules(cap) - skipped


# ---------------------------------------------------------------- T-S1: approve, promote, then revise the claim
def _approve_promote_then_revise():
    from approval_helpers import approve
    from ai_hydro.mcp.tools_ledger import promote_claim_to_registry
    approve(SID, "c1")
    out = promote_claim_to_registry(SID, "c1", researcher_approved=True)
    assert out.get("status") == "promoted", out
    r = update_claim_status(SID, "c1", "supported", "high", "Revised after promotion; synthetic fixture only.",
                            uncertainty_verified=True)
    assert r["status"] == "updated", r


def test_TS1_an_approval_of_an_earlier_revision_is_superseded_not_failed(exported, tmp_path):
    _approve_promote_then_revise()
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "ts1"))
    assert not res.get("crate_error") and res["crate_status"] == "ok", res        # export says ok ...
    cap = Path(res["capsule_dir"])
    for kw in ({"isolated_python": True}, {}):
        code, out = _replay(cap, **kw)                                              # ... and replay agrees
        assert code == 0, out
        assert "superseded: it signed revision" in out and "FAIL" not in out
    # the note names the real later revision (the status update), not promotion's bookkeeping revision
    rows = cr.history_readonly(SID)[0]["c1"]["rows"]
    real_later = next(r["revision"] for r in rows if r["cause"]["tool"] == "update_claim_status" and r["revision"] > 1)
    assert f"revision {real_later} changed the claim since" in out
    assert verify_crate(cap).ok


def test_TS1_a_tampered_or_missing_approved_revision_still_fails(exported, tmp_path):
    _approve_promote_then_revise()
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "ts1b"))
    cap = Path(res["capsule_dir"])
    p = cap / "records" / "claim_revisions.json"
    doc = json.loads(p.read_text())
    approved = json.loads((cap / "approvals" / "index.json").read_text())["claims"][0]["registry_stamp"]["claim_revision_digest"]
    rows = doc["claims"]["c1"]["rows"]
    i = next(k for k, r in enumerate(rows) if r["revision_digest"] == approved)
    # the approved revision is edited in place: its seal no longer verifies
    rows[i] = {**rows[i], "content": {**rows[i]["content"], "confidence_rationale": "edited later after approval"}}
    p.write_text(json.dumps(doc, indent=2, sort_keys=True))
    (cap / MANIFEST_FILE).write_text(json.dumps({**json.loads((cap / MANIFEST_FILE).read_text()),
                                                 **{k: v for k, v in build_manifest(cap).items() if k in ("files", "n_files")}}, indent=2))
    for n in _NEW:
        (cap / n).unlink()
    code, out = _replay(cap, isolated_python=True)
    assert code == 1 and "FAIL" in out


def test_deleted_projected_keys_are_a_head_difference(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_delkey")
    sess = json.loads((cap / "session.json").read_text())
    del sess["claims"]["c1"]["limitations"]
    del sess["claims"]["c1"]["status"]
    (cap / "session.json").write_text(json.dumps(sess, indent=2))
    lines = []
    bad = sr.check_claims_against_heads(cap, out=lines.append)
    assert bad and set(bad[0][1]) == {"limitations", "status"}
    # mirror-only until core compares missing keys too (to route to core): then parity is asserted
    for n in _NEW:
        (cap / n).unlink()
    _refresh_manifest(cap)
    import aihydro_core.export.rocrate_verify as rv
    import inspect
    if "sk not in claim" in inspect.getsource(rv):
        mp = pytest.MonkeyPatch()
        try:
            _force_integrity(mp)
            with pytest.raises((CrateExportError, ValueError)):
                export_crate(cap, session_id=SID, live=False)
        finally:
            mp.undo()
        skipped = {"VER-CRATE-REGEN", "VER-VALIDATE", "VER-BAGIT"}
        assert "VER-HEAD" in _rules(verify_crate(cap)) and _rules(verify_crate(cap)) - skipped == _stdlib_rules(cap) - skipped


# ---------------------------------------------------------------- core 0.2.10: partial from content, unsealed, deleted keys
def test_C_S1_a_relabelled_stub_is_partial_by_content_with_core_parity(exported, tmp_path):
    """A stub relabelled as a legacy unsealed row (no declaration) still makes the result partial; run_rows that
    undercount the stub rows fail VER-RUN-ROWS. Core and the mirror agree."""
    cap = _copy(exported[0], tmp_path, "t_cs1")
    for n in _NEW:
        (cap / n).unlink()
    log = json.loads((cap / "run_log.json").read_text())
    rid = _first_bound_row(log)
    log[rid] = _stub_of(log[rid])
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))
    _refresh_manifest(cap)
    _list_redacted_run(cap, rid)
    out = export_crate(cap, session_id=SID, live=False)
    assert out["partial"] and "withheld" in out["partial_reasons"]
    core = verify_crate(cap)
    assert core.status == "ok_partial_withheld" and "withheld" in core.partial_reasons
    mine = sr.verify_bundle(cap)
    assert mine["ok"] and "withheld" in mine["partial_reasons"]
    # run_rows undercounts the stub rows: both fail VER-RUN-ROWS
    b = json.loads((cap / "bundle.json").read_text())
    b["run_rows"] = {**b["run_rows"], "withheld_for_privacy": 0, "sealed": b["run_rows"]["sealed"] + b["run_rows"]["withheld_for_privacy"]}
    from aihydro_core.records import Bundle
    nb = Bundle.from_dict({k: v for k, v in b.items() if k not in ("bundle_id", "record_digest")}).seal()
    (cap / "bundle.json").write_text(json.dumps(nb.to_dict(), indent=2, sort_keys=True))
    skipped = {"VER-CRATE-REGEN", "VER-VALIDATE", "VER-BAGIT"}
    assert "VER-RUN-ROWS" in _rules(verify_crate(cap)) and _rules(verify_crate(cap)) - skipped == _stdlib_rules(cap) - skipped


def test_unsealed_only_capsule_is_partial_unsealed_in_core_and_replay(world, tmp_path):
    s = HydroSession.load(SID)
    s.set("_run_log", {"old.1": {"run_id": "old.1", "tool_name": "extract_hydrological_signatures", "session_id": SID,
                                 "timestamp": "2026-06-10T00:00:00+00:00", "key_outputs": {"q_mean": 1.25}}})
    s.save()
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "unsealed"))
    cap = Path(res["capsule_dir"])
    assert res["crate_status"] == "ok_partial_unsealed" and res["crate_partial_reasons"] == ["unsealed"]
    core = verify_crate(cap)
    assert core.status == "ok_partial_unsealed" and core.exit_code == 3
    for kw in ({"isolated_python": True}, {}):
        code, out = _replay(cap, **kw)
        assert code == 3 and "replay_outcome: ok_partial_unsealed" in out and "partial_reasons: unsealed" in out, out


def test_VER_HEAD_absent_chained_claim_and_deleted_withheld_head_keys(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_absent")
    sess = json.loads((cap / "session.json").read_text())
    del sess["claims"]["c1"]
    (cap / "session.json").write_text(json.dumps(sess, indent=2))
    assert sr.check_claims_against_heads(cap, out=lambda *_a: None) == [("c1", ["absent"])]
    for n in _NEW:
        (cap / n).unlink()
    _refresh_manifest(cap)
    mp = pytest.MonkeyPatch()
    try:
        _force_integrity(mp)
        with pytest.raises((CrateExportError, ValueError)):
            export_crate(cap, session_id=SID, live=False)
    finally:
        mp.undo()
    skipped = {"VER-CRATE-REGEN", "VER-VALIDATE", "VER-BAGIT"}
    assert "VER-HEAD" in _rules(verify_crate(cap)) and _rules(verify_crate(cap)) - skipped == _stdlib_rules(cap) - skipped
    # withheld head: a missing enumerated key is unverifiable (partial), a missing string key fails
    cap2 = _copy(exported[0], tmp_path, "t_absent2")
    for n in _NEW:
        (cap2 / n).unlink()
    _forged_stub_head(cap2, edit_claim=False)
    sess = json.loads((cap2 / "session.json").read_text())
    del sess["claims"]["c1"]["status"]
    (cap2 / "session.json").write_text(json.dumps(sess, indent=2))
    part = []
    assert sr.check_claims_against_heads(cap2, out=lambda *_a: None, partial=part) == [] and part == [True]
    sess["claims"]["c1"].pop("claim")
    (cap2 / "session.json").write_text(json.dumps(sess, indent=2))
    assert sr.check_claims_against_heads(cap2, out=lambda *_a: None)[0][1] == ["claim"]


# ---------------------------------------------------------------- T-M1 and follow-ups
def _revise_approve_promote():
    from approval_helpers import approve
    from ai_hydro.mcp.tools_ledger import promote_claim_to_registry
    update_claim_status(SID, "c1", "supported", "high", "Revised before approval; synthetic fixture only.",
                        uncertainty_verified=True)
    approve(SID, "c1")
    assert promote_claim_to_registry(SID, "c1", researcher_approved=True).get("status") == "promoted"


def test_TM1_revise_approve_promote_is_current_not_superseded(exported, tmp_path):
    _revise_approve_promote()
    rows = cr.history_readonly(SID)[0]["c1"]["rows"]
    assert rows[-1]["cause"]["reason"] == "promotion" and rows[-1]["revision_digest"] == rows[-2]["revision_digest"]
    import ai_hydro.mcp.tools_session as ts
    res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "tm1"))
    assert not res.get("crate_error"), res
    cap = Path(res["capsule_dir"])
    for kw in ({"isolated_python": True}, {}):
        code, out = _replay(cap, **kw)
        assert code == 0 and "superseded" not in out, out


def test_TM1_the_stronger_head_check_still_runs_for_a_current_approval(exported, tmp_path):
    """With the approval current, editing the claim in the capsule is caught by the recomputation, not skipped."""
    _revise_approve_promote()
    import ai_hydro.mcp.tools_session as ts
    cap = Path(ts.export_session(session_id=SID, capsule_path=str(tmp_path / "tm1b"))["capsule_dir"])
    sess = json.loads((cap / "session.json").read_text())
    sess["claims"]["c1"]["claim"] += " (edited)"
    (cap / "session.json").write_text(json.dumps(sess, indent=2))
    for n in _NEW:
        (cap / n).unlink()
    _refresh_manifest(cap)
    out = sr.verify_approvals(cap, out=lambda *_a: None)
    assert out["failed"] == 1


def _cited_flow(claim_id, run_id, rows_sealed: bool):
    """claim_id cites run_id; approve, promote, then revise (so the approval is superseded)."""
    from approval_helpers import approve
    from ai_hydro.mcp.tools_ledger import promote_claim_to_registry
    body = {**_run_record(SID), "run_id": run_id}
    if rows_sealed:
        _seal_run_row(run_id, body)
    else:
        conn = store._run_log_connect(SID)
        conn.execute("INSERT OR REPLACE INTO runs (run_id, timestamp, entry_json) VALUES (?,?,?)",
                     (run_id, "2026-01-01T00:00:00+00:00", json.dumps({**body, "timestamp": "2026-01-01T00:00:00+00:00"})))
        conn.commit()
        conn.close()
    assert add_claim(session_id=SID, claim_id=claim_id, statement="Synthetic NSE is 0.8 plain statement",
                     claim_type="empirical_result", status="proposed", confidence="low",
                     confidence_rationale="Synthetic fixture rationale text.", basins=["synthetic"], period="2000-2001",
                     metric="nse", basin_refs=[basin_ref_full("synthetic")], limitations=["plain"],
                     evidence_spans=[{"source_type": "run", "source_id": run_id, "metric_ref": "nse"}])["status"] == "recorded"
    update_claim_status(SID, claim_id, "supported", "medium", "Synthetic regression fixture only.", uncertainty_verified=True)
    approve(SID, claim_id)
    assert promote_claim_to_registry(SID, claim_id, researcher_approved=True).get("status") == "promoted"
    update_claim_status(SID, claim_id, "supported", "high", "Revised after promotion; synthetic fixture only.",
                        uncertainty_verified=True)


def test_sealed_bound_row_whose_record_digest_differs_is_described_as_present_but_different(exported, tmp_path):
    _cited_flow("c_seal", "rseal", rows_sealed=True)
    import ai_hydro.mcp.tools_session as ts
    cap = Path(ts.export_session(session_id=SID, capsule_path=str(tmp_path / "bound"))["capsule_dir"])
    for n in _NEW:
        (cap / n).unlink()
    log = json.loads((cap / "run_log.json").read_text())
    log["rseal"]["record"]["record_digest"] = "sha256:" + "ab" * 32
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))
    _refresh_manifest(cap)
    lines = []
    out = sr.verify_approvals(cap, out=lines.append)
    text = "\n".join(lines)
    assert out["failed"] >= 1 and "present but their record digest (or fingerprint) differs" in text
    del log["rseal"]
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))
    lines = []
    sr.verify_approvals(cap, out=lines.append)
    assert "are not in this capsule" in "\n".join(lines)


def test_pre_seal_approval_is_rederived_from_the_run_log_when_the_claim_has_no_evidence_seals(exported, tmp_path):
    """A claim bound to an UNSEALED row has evidence fingerprints but no evidence_seals: re-derive them."""
    _cited_flow("c_leg", "rleg", rows_sealed=False)
    import ai_hydro.mcp.tools_session as ts
    cap = Path(ts.export_session(session_id=SID, capsule_path=str(tmp_path / "preseal"))["capsule_dir"])
    rows = cr.history_readonly(SID)[0]["c_leg"]["rows"]
    assert not any((r["content"].get("evidence_seals") or {}) for r in rows)
    lines = []
    assert sr.verify_approvals(cap, out=lines.append)["failed"] == 0 and "superseded" in "\n".join(lines)
    for n in _NEW:
        (cap / n).unlink()
    log = json.loads((cap / "run_log.json").read_text())
    log["rleg"]["key_outputs"] = {**log["rleg"].get("key_outputs", {}), "nse": 0.99}
    (cap / "run_log.json").write_text(json.dumps(log, indent=2))
    _refresh_manifest(cap)
    lines = []
    assert sr.verify_approvals(cap, out=lines.append)["failed"] >= 1
    assert "present but their record digest (or fingerprint) differs" in "\n".join(lines)


def test_staleness_flags_a_claim_revised_since_promotion_but_not_before(exported):
    from approval_helpers import approve
    from ai_hydro.mcp.tools_ledger import check_registry_staleness, promote_claim_to_registry
    approve(SID, "c1")
    assert promote_claim_to_registry(SID, "c1", researcher_approved=True).get("status") == "promoted"
    assert check_registry_staleness(SID)["claim_revised_since_promotion"] == []
    update_claim_status(SID, "c1", "supported", "high", "Revised after promotion; synthetic fixture only.",
                        uncertainty_verified=True)
    res = check_registry_staleness(SID)
    (flag,) = res["claim_revised_since_promotion"]
    assert flag["claim_id"] == "c1" and flag["flag"] == "claim_revised_since_promotion"
    assert flag["latest_revision"] > flag["promoted_revision"] and res["n_stale"] == 0 and res["revision_chain_mismatches"] == []
