"""Agent-context refusal in ``aihydro-approve`` (defence in depth, exit 6).

A speed bump, not a boundary: same-user code can unset the variables. These
tests pin the behaviour: ``approve`` and ``enroll --user-trust`` refuse, before
the TTY check, when ``AIHYDRO_AGENT_CONTEXT`` is truthy or ``AIHYDRO_ACTIVE`` is
present; ``revoke`` and plain ``enroll`` still work; there is no override.

Scratch ``AIHYDRO_HOME``/``HOME`` only (the suite fixture); throwaway software
key generated in ``tmp_path``. Nothing touches ``~/.aihydro`` or ``~/.ssh``.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_hydro.approval import cli
from ai_hydro.approval.cli import EXIT_AGENT_CONTEXT, EXIT_NOT_TTY, main as approve_cli

CTX_VARIANTS = [
    pytest.param({"AIHYDRO_AGENT_CONTEXT": "1"}, id="agent_context=1"),
    pytest.param({"AIHYDRO_AGENT_CONTEXT": "true"}, id="agent_context=true"),
    pytest.param({"AIHYDRO_AGENT_CONTEXT": "yes"}, id="agent_context=yes"),
    pytest.param({"AIHYDRO_ACTIVE": "1"}, id="active=1"),
    pytest.param({"AIHYDRO_ACTIVE": "true"}, id="active=true"),
    pytest.param({"AIHYDRO_ACTIVE": ""}, id="active=empty-but-present"),
    pytest.param({"AIHYDRO_ACTIVE": "0"}, id="active=0-but-present"),
]


@pytest.fixture(autouse=True)
def scratch_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AIHYDRO_HOME", str(tmp_path / "aihydro_home"))
    monkeypatch.setenv("AIHYDRO_SYSTEM_TRUST_FILE", str(tmp_path / "etc" / "allowed_signers"))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    return tmp_path


@pytest.fixture
def pubkey(tmp_path):
    if shutil.which("ssh-keygen") is None:
        pytest.skip("needs OpenSSH ssh-keygen")
    key = tmp_path / "k"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(key)],
                   check=True, capture_output=True)
    return str(key) + ".pub"


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = approve_cli(argv, stdin=io.StringIO(""), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _set(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)


@pytest.mark.parametrize("env", CTX_VARIANTS)
def test_approve_refused_with_exit_6(monkeypatch, env):
    _set(monkeypatch, env)
    code, out, err = _run(["sess", "claim"])
    assert code == EXIT_AGENT_CONTEXT == 6
    assert "not by an agent" in err and "your own terminal" in err
    assert out == ""


@pytest.mark.parametrize("env", CTX_VARIANTS)
def test_enroll_user_trust_refused_and_writes_nothing(monkeypatch, tmp_path, pubkey, env):
    _set(monkeypatch, env)
    code, out, err = _run(["enroll", pubkey, "--user-trust"])
    assert code == 6
    assert "not by an agent" in err
    assert not (tmp_path / "aihydro_home" / "trust").exists()


@pytest.mark.parametrize("env", CTX_VARIANTS)
def test_plain_enroll_and_revoke_still_allowed(monkeypatch, tmp_path, pubkey, env):
    _set(monkeypatch, env)
    code, out, _ = _run(["enroll", pubkey])
    assert code == 0 and "allowed_signers line" in out
    assert not (tmp_path / "aihydro_home" / "trust").exists()      # printed only
    code, out, _ = _run(["revoke", "SHA256:" + "A" * 43, "--reason", "test"])
    assert code == 0 and "Recorded local revocation" in out


def test_no_marker_does_not_refuse_with_6(monkeypatch):
    for k in ("AIHYDRO_AGENT_CONTEXT", "AIHYDRO_ACTIVE"):
        monkeypatch.delenv(k, raising=False)
    code, _, err = _run(["sess", "claim"])
    assert code == EXIT_NOT_TTY            # falls through to the TTY check
    assert "not by an agent" not in err


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_falsy_agent_context_is_not_a_marker(monkeypatch, value):
    monkeypatch.delenv("AIHYDRO_ACTIVE", raising=False)
    monkeypatch.setenv("AIHYDRO_AGENT_CONTEXT", value)
    assert _run(["sess", "claim"])[0] == EXIT_NOT_TTY


def test_refusal_happens_before_tty_check(monkeypatch):
    """Without the marker a non-TTY run gives 3; with it, 6 (the TTY check never runs)."""
    monkeypatch.delenv("AIHYDRO_ACTIVE", raising=False)
    monkeypatch.delenv("AIHYDRO_AGENT_CONTEXT", raising=False)
    assert _run(["sess", "claim"])[0] == 3
    monkeypatch.setenv("AIHYDRO_AGENT_CONTEXT", "1")
    calls = []
    monkeypatch.setattr(cli, "_is_tty", lambda s: calls.append(s) or True)
    assert _run(["sess", "claim"])[0] == 6
    assert calls == []                       # never consulted, even if a pty made it look interactive


def test_enroll_refusal_happens_before_reading_the_key(monkeypatch):
    monkeypatch.setenv("AIHYDRO_AGENT_CONTEXT", "1")
    code, _, err = _run(["enroll", "/nonexistent/key.pub", "--user-trust"])
    assert code == 6 and "not by an agent" in err


def test_no_override_flag_or_env():
    # No bypass flag on any subcommand.
    for fn_args in (["--help"], ["enroll", "--help"], ["revoke", "--help"]):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert approve_cli(fn_args) == 0         # main() turns argparse's exit into a return code
        assert not re.search(r"--(force|yes|override|allow|agent|ignore|skip|unsafe|bypass)", out.getvalue())
    # The only agent-marker env vars the CLI reads are the two refusal triggers.
    src = inspect.getsource(cli)
    names = set(re.findall(r"""environ(?:\.get)?[\[(]\s*["'](AIHYDRO_\w+)["']""", src))
    assert names <= {"AIHYDRO_ACTIVE", "AIHYDRO_AGENT_CONTEXT"}, names


def test_approve_with_override_looking_args_still_refused(monkeypatch):
    monkeypatch.setenv("AIHYDRO_AGENT_CONTEXT", "1")
    for k in ("AIHYDRO_ALLOW_AGENT_APPROVE", "AIHYDRO_REQUIRE_SIGNED", "AIHYDRO_AGENT_CONTEXT_OVERRIDE"):
        monkeypatch.setenv(k, "1")
    assert _run(["sess", "claim"])[0] == 6


def test_docs_say_speed_bump_not_boundary():
    doc = cli.__doc__
    assert "SPEED BUMP, NOT A BOUNDARY" in doc and "sk-" in doc
    assert "no override" in doc.lower()


def test_no_dunder_main_entry_point():
    # The console script is the entry point; a __main__ was deliberately not added.
    assert not (Path(cli.__file__).parent / "__main__.py").exists()
