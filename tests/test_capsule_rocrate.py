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


def _session_with_claim(ws: Path | None = None):
    s = HydroSession.load(SID)
    if ws is not None:
        s.workspace_dir = str(ws)
    s.set("_run_log", {"r1": _run_record(SID)})        # a legacy row with uncertainty evidence for the claim
    s.save()
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
    extra = {"VER-CHAIN"} | ({"VER-SESSION", "VER-COVERAGE"} if "VER-SESSION" in mine_rules else set())
    assert _rules(core) - {"VER-CRATE-REGEN", "VER-VALIDATE"} <= mine_rules <= _rules(core) | extra, name
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
    assert code == 0, out
    assert "partial" in out and "archive_integrity_partial" not in out
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


def test_bundle_carries_the_unsealed_row_counts(exported):
    cap, _ = exported
    rr_ = json.loads((cap / "bundle.json").read_text())["run_rows"]
    assert set(rr_) == {"run_log_rows", "sealed", "legacy_no_record", "unbound", "unsealable",
                        "withheld_for_privacy"}
    assert rr_["legacy_no_record"] >= 1 and rr_["sealed"] >= 1      # the fixture's r1 row is legacy
    assert rr_["run_log_rows"] == rr_["sealed"] + rr_["legacy_no_record"] + rr_["withheld_for_privacy"]


def _mark_claim_corrupt(cap: Path):
    p = cap / "records" / "claim_revisions.json"
    doc = json.loads(p.read_text())
    doc["claims"]["c1"] = {"status": "corrupt", "error": "ClaimRevisionIntegrityError: synthetic"}
    p.write_text(json.dumps(doc, indent=2, sort_keys=True))
    (cap / MANIFEST_FILE).write_text(json.dumps(build_manifest(cap), indent=2))


def test_a_claim_corrupt_at_export_is_a_failed_store_stub_not_a_working_view(exported, tmp_path):
    cap = _copy(exported[0], tmp_path, "t_corrupt")
    for name in _NEW:
        (cap / name).unlink()
    _mark_claim_corrupt(cap)
    out = export_crate(cap, session_id=SID)
    crate = (cap / "ro-crate-metadata.json").read_text()
    assert "working view, unsealed" not in crate                  # session.json still holds the claim text
    assert "Claim c1 revision failed-store (unverifiable)" in crate
    bundle = json.loads((cap / "bundle.json").read_text())
    assert "c1@failed-store" in bundle["coverage"]["unverifiable_ids"]
    assert not any(r["kind"] == "claim_view" and r["id"] == "c1" for r in bundle["records"])
    assert bundle["replay"]["checked_status"] == "not_performed" and out["claims_not_carried"]
    assert verify_crate(cap).ok


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
