"""Capsule approvals and external verification (ADR-002b, packet A3).

Throwaway ed25519 keys in a scratch HOME. ``export_session`` carries the
approval record each promoted claim consumed plus the registry stamp;
``replay.py --allowed-signers FILE`` verifies against a key file the verifier
supplies (never the machine's trust root).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from aihydro_core.records import Actor, digest

from ai_hydro.approval import records, trust
from ai_hydro.approval.records import approval_stamp
from ai_hydro.approval.signing import enrol_line, read_pubkey_file
from ai_hydro.approval.writer import write_approval, write_signed_approval
from ai_hydro.capsule import standalone_replay as sr
from ai_hydro.capsule.manifest import MANIFEST_FILE, build_manifest
from ai_hydro.registry import store as registry

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs OpenSSH ssh-keygen")

SID = "cap-approvals"
REV = "sha256:" + "cd" * 32
HUMAN = Actor(kind="human", id="alice")


@pytest.fixture(autouse=True)
def scratch_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AIHYDRO_SYSTEM_TRUST_FILE", str(tmp_path / "etc" / "allowed_signers"))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("AIHYDRO_REQUIRE_SIGNED", raising=False)
    return tmp_path


def make_key(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)],
                   check=True, capture_output=True)
    return path


def signer_file(path: Path, key: Path, principal: str = "alice") -> Path:
    path.write_text(enrol_line(read_pubkey_file(Path(str(key) + ".pub")), principal) + "\n")
    return path


@pytest.fixture
def keys(tmp_path):
    good = make_key(tmp_path / "keys", "good")
    other = make_key(tmp_path / "keys", "other")
    # the machine's own trust root enrols the good key (needed to *write* a signed approval)
    user = trust.user_trust_file()
    user.parent.mkdir(parents=True, exist_ok=True)
    signer_file(user, good)
    return {"good": good, "other": other,
            "supplied_good": signer_file(tmp_path / "supplied_good", good),
            "supplied_other": signer_file(tmp_path / "supplied_other", other)}


class Case:
    """A session whose claims are promoted with the given approval kinds, exported to a capsule."""

    def __init__(self, tmp_path, monkeypatch, claims: dict, key: Path | None = None):
        import ai_hydro.mcp.tools_session as ts
        import ai_hydro.session.store as store
        from ai_hydro.session.store import HydroSession

        sessions = tmp_path / "sessions"
        sessions.mkdir(exist_ok=True)
        monkeypatch.setattr(store, "SESSIONS_DIR", sessions)
        monkeypatch.setattr(store, "_SESSIONS_DIR", sessions)
        session = HydroSession(SID)
        session.set("_run_log", {})
        session.claims = {}
        self.approvals = {}
        for i, (claim_id, kind) in enumerate(claims.items()):
            rid = f"{SID}.{claim_id}"
            session.claims[claim_id] = {"claim": "x", "promoted": True, "registry_id": rid}
            row = {"registry_id": rid, "claim_id": claim_id, "session_id": SID, "status": "promoted",
                   "claim_revision_digest": REV, "claim_revision": 1}
            if kind == "signed":
                rec = write_signed_approval(SID, claim_id, REV, HUMAN, "ok", key=str(key))
            elif kind == "v1":
                rec = write_approval(SID, claim_id, REV, HUMAN, "ok")
            else:
                rec = None
            if rec is not None:
                self.approvals[claim_id] = rec
                row["approval"] = approval_stamp(rec) if kind == "signed" else {
                    "record_digest": rec["record_digest"], "channel": "cli_same_user"}
            registry.append(row)
        session.save()
        res = ts.export_session(session_id=SID, capsule_path=str(tmp_path / "capsule"))
        assert "error" not in res, res
        self.dir = Path(res["capsule_dir"])
        self.result = res

    def replay(self, *args):
        p = subprocess.run([sys.executable, "replay.py", *args], cwd=self.dir,
                           capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout

    def reseal_manifest(self):
        (self.dir / MANIFEST_FILE).write_text(json.dumps(build_manifest(self.dir)))


def test_valid_signature_passes(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 0, out
    assert "PASS  approval c1 (" in out
    assert "approvals: 1 verified against supplied signers, 0 failed, 0 unsigned (cli_same_user/opt-out)" in out
    # carried verbatim, hashed in the manifest
    m = json.loads((c.dir / MANIFEST_FILE).read_text())
    paths = {f["path"] for f in m["files"]}
    assert "approvals/index.json" in paths and f"approvals/{c.approvals['c1']['record_digest'][7:]}.json" in paths
    assert m["approvals"]["n_with_approval_record"] == 1 and m["approvals"]["n_no_approval"] == 0
    assert all(f["sha256"] for f in m["approvals"]["files"])
    # equals-sign spelling works too
    assert c.replay(f"--allowed-signers={keys['supplied_good']}")[0] == 0


def test_standalone_seal_matches_core_digest(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    rec = c.approvals["c1"]
    body = {k: v for k, v in rec.items() if k not in ("record_digest", "signature")}
    assert sr.c14n_digest(body) == digest(body) == rec["record_digest"]


def test_wrong_key_fails(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    code, out = c.replay("--allowed-signers", str(keys["supplied_other"]))
    assert code == 1
    assert "FAIL  approval c1 (" in out and "PASS  approval " not in out
    assert "0 verified against supplied signers, 1 failed" in out


def test_wrong_principal_fails(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    mallory = signer_file(tmp_path / "mallory", keys["good"], principal="mallory")
    code, out = c.replay("--allowed-signers", str(mallory))
    assert code == 1 and "FAIL  approval c1 (" in out


def test_tampered_approval_fails_even_with_regenerated_manifest(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    f = c.dir / c.result["approvals"]["files"][1]["path"]
    rec = json.loads(f.read_text())
    rec["statement"] = "approved something else"
    f.write_text(json.dumps(rec))
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "FAIL  approvals/" in out          # the manifest hash check flags it
    c.reseal_manifest()                                    # attacker hides it from the hash check
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1
    assert "FAIL  approval c1 (cap-approvals.c1): sealed body digest mismatch" in out


def test_resealed_record_fails_signature(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    f = c.dir / c.result["approvals"]["files"][1]["path"]
    rec = json.loads(f.read_text())
    rec["statement"] = "forged"
    rec = records.seal_record(rec)                         # new digest, old signature
    f.write_text(json.dumps(rec))
    idx = c.dir / "approvals" / "index.json"
    ix = json.loads(idx.read_text())
    ix["claims"][0]["record_digest"] = rec["record_digest"]
    ix["claims"][0]["registry_stamp"]["approval"]["record_digest"] = rec["record_digest"]
    idx.write_text(json.dumps(ix))
    c.reseal_manifest()
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "FAIL  approval c1 (cap-approvals.c1): signature not accepted" in out


def test_revision_digest_must_match_registry_stamp(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    idx = c.dir / "approvals" / "index.json"
    ix = json.loads(idx.read_text())
    ix["claims"][0]["registry_stamp"]["claim_revision_digest"] = "sha256:" + "00" * 32
    idx.write_text(json.dumps(ix))
    c.reseal_manifest()
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 1 and "claim_revision_digest differs from the registry stamp's" in out


def test_unsigned_v1_is_unsigned_never_pass(tmp_path, monkeypatch, keys):
    monkeypatch.setenv("AIHYDRO_REQUIRE_SIGNED", "0")
    c = Case(tmp_path, monkeypatch, {"c1": "v1"})
    for args in ([], ["--allowed-signers", str(keys["supplied_good"])]):
        code, out = c.replay(*args)
        assert code == 0, out
        assert "UNSIGNED  approval c1 (" in out and "PASS  approval " not in out
        assert "1 unsigned (cli_same_user/opt-out)" in out


def test_missing_flag_is_not_verified(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    code, out = c.replay()
    assert code == 0, out
    assert "not verified (no signer file supplied)" in out
    assert "PASS  approval " not in out and "verified against supplied signers" not in out


def test_missing_signer_file_fails(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed"}, keys["good"])
    code, out = c.replay("--allowed-signers", str(tmp_path / "nope"))
    assert code == 1 and "signer" in out.lower() and "PASS  approval " not in out


def test_claim_without_approval_is_listed_not_approved(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {"c1": "signed", "c2": "none"}, keys["good"])
    code, out = c.replay("--allowed-signers", str(keys["supplied_good"]))
    assert code == 0, out
    assert "NOTE  approval c2 (" in out and "not approved" in out
    assert "1 verified against supplied signers" in out
    ix = json.loads((c.dir / "approvals" / "index.json").read_text())
    assert {e["claim_id"]: e["status"] for e in ix["claims"]} == {"c1": "approved", "c2": "no_approval"}
    assert c.result["approvals"]["n_no_approval"] == 1


def test_capsule_without_promoted_claims_is_fine(tmp_path, monkeypatch, keys):
    c = Case(tmp_path, monkeypatch, {})
    assert not (c.dir / "approvals").exists()
    assert c.result["approvals"]["n_promoted_claims"] == 0
    for args in ([], ["--allowed-signers", str(keys["supplied_good"])]):
        code, out = c.replay(*args)
        assert code == 0, out
        assert "none in this capsule" in out
