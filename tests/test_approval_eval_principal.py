"""ADR-007: the evaluation-only principal ``eval-approver@`` is never a production approver.

The P1 harness signs approvals with an evaluation key under ``eval-approver@p1.aihydro.invalid``
against a user-writable trust file (integrity only). The production verifier refuses that
principal under a ``system`` trust root, and ``aihydro-approve enroll`` will not emit an
enrolment line for it. Nothing else about verification changes (controls below).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from aihydro_core.records import Actor

from ai_hydro.approval import trust
from ai_hydro.approval.cli import main as approve_cli
from ai_hydro.approval.signing import enrol_line, read_pubkey_file
from ai_hydro.approval.writer import write_signed_approval
from ai_hydro.approval import records
from test_approval_signing import BODY, REV, _Sink, make_key, scratch_home, sign_manually  # noqa: F401  (autouse fixture)

EVAL = "eval-approver@p1.aihydro.invalid"
pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs OpenSSH ssh-keygen")
posix_user = pytest.mark.skipif(os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                                reason="needs a non-root POSIX user (root can always write)")


def _line(key, principal):
    return enrol_line(read_pubkey_file(Path(str(key) + ".pub")), principal)


def _sign(key, principal):
    return write_signed_approval("s1", "c1", REV, Actor(kind="human", id=principal), "ok", key=str(key))


def _forged(key, approver, tmp_path):
    """A hand-signed, validly sealed v2 record naming ``approver`` (what a same-user process can mint)."""
    return sign_manually({**BODY, "approver": {"kind": "human", "id": approver}}, key, tmp_path)


def _user_trust(key, principal):
    path = trust.user_trust_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_line(key, principal) + "\n")


def _system_trust(scratch_home, key, principal, monkeypatch):
    sysfile = Path(os.environ["AIHYDRO_SYSTEM_TRUST_FILE"])
    sysfile.parent.mkdir(parents=True)
    sysfile.write_text(_line(key, principal) + "\n")
    os.chmod(sysfile, 0o444)
    os.chmod(sysfile.parent, 0o555)
    real = os.geteuid()
    monkeypatch.setattr(trust.os, "geteuid", lambda: real + 1)   # "owned by someone else"
    return sysfile


@posix_user
@pytest.mark.parametrize("principal", [EVAL, "EVAL-Approver@other.example", f"alice,{EVAL}"])
def test_system_root_refuses_the_evaluation_principal(scratch_home, monkeypatch, principal):
    key = make_key(scratch_home / "keys")
    sysfile = _system_trust(scratch_home, key, principal, monkeypatch)
    try:
        assert trust.trust_root() == (sysfile, "system")
        verdict = trust.check_approval(_forged(key, EVAL, scratch_home))
        assert verdict.ok is False and verdict.trust_root == "system"
        assert "eval-approver@" in verdict.reason and "system trust root" in verdict.reason
        # and the production writer will not store such an approval at all
        with pytest.raises(ValueError, match="eval-approver@"):
            _sign(key, EVAL)
        assert records.find_approval("s1", "c1", REV) is None
    finally:
        os.chmod(sysfile.parent, 0o755)


@posix_user
def test_system_root_refuses_when_only_the_approver_id_is_the_eval_principal(scratch_home, monkeypatch):
    key = make_key(scratch_home / "keys")
    sysfile = _system_trust(scratch_home, key, "alice", monkeypatch)
    try:
        verdict = trust.check_approval(_forged(key, EVAL, scratch_home))
        assert verdict.ok is False and "eval-approver@" in verdict.reason
    finally:
        os.chmod(sysfile.parent, 0o755)


def test_user_writable_evaluation_trust_still_verifies_as_integrity_only(scratch_home):
    key = make_key(scratch_home / "keys")
    _user_trust(key, EVAL)
    verdict = trust.check_approval(_sign(key, EVAL))
    assert verdict.ok is True
    assert verdict.channel == "ssh_sig_user_trust" and verdict.trust_root == "user_writable"
    assert verdict.principal == EVAL


@posix_user
def test_system_root_still_accepts_an_ordinary_principal(scratch_home, monkeypatch):
    key = make_key(scratch_home / "keys")
    sysfile = _system_trust(scratch_home, key, "alice", monkeypatch)
    try:
        verdict = trust.check_approval(_sign(key, "alice"))
        assert verdict.ok is True and verdict.channel == "ssh_sig_system_trust"
    finally:
        os.chmod(sysfile.parent, 0o755)


@pytest.mark.parametrize("principal", [EVAL, "Eval-Approver@x", f"alice,{EVAL}"])
@pytest.mark.parametrize("extra", [[], ["--user-trust"]])
def test_enroll_refuses_to_emit_a_line_for_the_evaluation_principal(scratch_home, principal, extra):
    key = make_key(scratch_home / "keys")
    out, err = _Sink(), _Sink()
    rc = approve_cli(["enroll", f"{key}.pub", "--principal", principal, *extra], stdout=out, stderr=err)
    assert rc == 2
    assert out.getvalue() == "" and "allowed_signers line" not in out.getvalue()
    assert "eval-approver@" in err.getvalue()
    assert not trust.user_trust_file().exists()


def test_enroll_still_enrols_an_ordinary_principal(scratch_home):
    key = make_key(scratch_home / "keys")
    out = _Sink()
    assert approve_cli(["enroll", f"{key}.pub", "--principal", "alice"], stdout=out, stderr=_Sink()) == 0
    assert "alice" in out.getvalue()
