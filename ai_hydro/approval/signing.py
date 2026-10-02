"""Signing side of ADR-002b: SSHSIG creation, enrolment lines, revocation.

Imported only by ``ai_hydro.approval.writer`` and ``ai_hydro.approval.cli``.
Nothing reachable from the MCP server may import it or invoke ``ssh-keygen -Y
sign`` (``tests/test_approval_authority.py`` enforces both). Private keys are
never read, copied or stored here: ``ssh-keygen`` (and ssh-agent) do the
signing, including any passphrase or FIDO touch prompt, on the human's
terminal.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from aihydro_core.records import utc_now

from ai_hydro.approval.records import approvals_dir, seal_record_generic
from ai_hydro.approval.trust import (
    NAMESPACE,
    REVOCATION_SCHEMA,
    key_fingerprint,
    revocations_file,
    user_trust_file,
)
from ai_hydro.registry.locking import file_lock

DEFAULT_KEYS = ("id_ed25519_sk", "id_ecdsa_sk", "id_ed25519", "id_ecdsa", "id_rsa")
_PUB_RE = re.compile(r"^(?P<type>(?:ssh|ecdsa|sk)-\S+)\s+(?P<b64>[A-Za-z0-9+/=]+)(?:\s+(?P<comment>.*))?$")


class SigningError(Exception):
    """No usable key, or ssh-keygen refused to sign."""


def _exe() -> str:
    exe = shutil.which("ssh-keygen")
    if exe is None:
        raise SigningError("ssh-keygen not found on PATH (OpenSSH 8.0+ is required for -Y sign).")
    return exe


def parse_pubkey_line(text: str) -> dict:
    """``{key_type, blob, b64, comment, fingerprint}`` from a public-key line."""
    for raw in text.splitlines():
        m = _PUB_RE.match(raw.strip())
        if m:
            blob = base64.b64decode(m["b64"])
            return {"key_type": m["type"], "blob": blob, "b64": m["b64"],
                    "comment": (m["comment"] or "").strip(), "fingerprint": key_fingerprint(blob)}
    raise SigningError("not an OpenSSH public key line (expected 'ssh-ed25519 AAAA... comment')")


def read_pubkey_file(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    if "PRIVATE KEY" in text:
        raise SigningError(f"{path} is a private key; pass the .pub file.")
    return parse_pubkey_line(text)


def _pub_for(key: Path) -> Path:
    return key if key.suffix == ".pub" else Path(str(key) + ".pub")


def default_key() -> Optional[Path]:
    """First of ``~/.ssh/id_*`` (hardware keys first) that has a public half."""
    ssh_dir = Path(os.path.expanduser("~")) / ".ssh"
    for name in DEFAULT_KEYS:
        if (ssh_dir / name).is_file() and (ssh_dir / (name + ".pub")).is_file():
            return ssh_dir / name
    return None


def agent_public_keys() -> list:
    """Parsed public keys held by ssh-agent (empty when none or no agent)."""
    exe = shutil.which("ssh-add")
    if not exe or not os.environ.get("SSH_AUTH_SOCK"):
        return []
    try:
        res = subprocess.run([exe, "-L"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    keys = []
    for line in res.stdout.splitlines() if res.returncode == 0 else []:
        try:
            keys.append({**parse_pubkey_line(line), "line": line.strip()})
        except (SigningError, ValueError):
            continue
    return keys


def resolve_key(key: Optional[str], enrolled_fingerprints: Optional[set] = None) -> dict:
    """Pick the signing key: ``--key``, else a default ``~/.ssh`` key, else an enrolled agent key.

    Returns ``{pub, path, agent}`` where ``path`` is the file passed to
    ``ssh-keygen -f`` (private key, or a ``.pub`` meaning "ask the agent").
    """
    if key:
        path = Path(key).expanduser()
        pub_path = _pub_for(path)
        if not pub_path.is_file():
            raise SigningError(f"public key {pub_path} not found (needed to name the signer).")
        pub = read_pubkey_file(pub_path)
        return {"pub": pub, "path": path if path.suffix != ".pub" and path.is_file() else pub_path,
                "agent": not (path.suffix != ".pub" and path.is_file())}
    path = default_key()
    if path is not None:
        return {"pub": read_pubkey_file(_pub_for(path)), "path": path, "agent": False}
    for pub in agent_public_keys():
        if enrolled_fingerprints is None or pub["fingerprint"] in enrolled_fingerprints:
            return {"pub": pub, "path": None, "agent": True, "line": pub["line"]}
    raise SigningError("no signing key available: pass --key, or create ~/.ssh/id_ed25519(_sk), "
                       "or load an enrolled key into ssh-agent.")


def sign_digest(record_digest: str, choice: dict, scratch: Path) -> str:
    """Armored SSHSIG over the UTF-8 bytes of ``record_digest`` (namespace ``aihydro-approval@v1``)."""
    args = [_exe(), "-Y", "sign", "-n", NAMESPACE]
    if choice.get("path") is not None and not choice.get("agent"):
        args += ["-f", str(choice["path"])]
    else:
        pub_file = choice.get("path")
        if pub_file is None:                      # agent key known only by its line
            pub_file = scratch / "agent_key.pub"
            pub_file.write_text(choice["line"] + "\n", encoding="utf-8")
        args += ["-U", "-f", str(pub_file)]
    # stderr is inherited so passphrase / "touch your authenticator" prompts reach the human.
    res = subprocess.run(args, input=record_digest.encode("utf-8"), stdout=subprocess.PIPE, timeout=300)
    if res.returncode != 0 or b"BEGIN SSH SIGNATURE" not in res.stdout:
        raise SigningError(f"ssh-keygen -Y sign failed (exit {res.returncode}).")
    return res.stdout.decode("utf-8").strip() + "\n"


# ---------------------------------------------------------------------------
# Enrolment and revocation
# ---------------------------------------------------------------------------

def enrol_line(pub: dict, principal: str, valid_after: Optional[str] = None,
               valid_before: Optional[str] = None) -> str:
    """One ``allowed_signers`` line, restricted to this namespace."""
    opts = [f'namespaces="{NAMESPACE}"']
    if valid_after:
        opts.append(f'valid-after="{valid_after}"')
    if valid_before:
        opts.append(f'valid-before="{valid_before}"')
    return f"{principal} {','.join(opts)} {pub['key_type']} {pub['b64']}"


def append_user_trust(line: str) -> Path:
    """Append to the user-writable fallback trust root (dir 0700, file 0600)."""
    path = user_trust_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return path


def write_revocation(fingerprint: str, reason: str = "") -> dict:
    """Append a sealed revocation line for ``fingerprint`` (``SHA256:...``)."""
    if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", fingerprint):
        raise ValueError("fingerprint must look like SHA256:<43 base64 chars> (as printed by ssh-keygen -lf)")
    rec = seal_record_generic({"schema": REVOCATION_SCHEMA, "fingerprint": fingerprint,
                               "revoked_at": utc_now(), "reason": reason})
    approvals_dir().mkdir(parents=True, exist_ok=True)
    with file_lock(approvals_dir() / "revocations.jsonl.lock"):
        fd = os.open(str(revocations_file()), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(rec, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    return rec
