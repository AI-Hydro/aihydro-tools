"""Read side of approval signatures: trust root, revocations, SSHSIG verification (ADR-002b).

Safe to import from MCP tool modules. It runs ``ssh-keygen -Y verify`` and
``-Y find-principals`` (read-only operations) and can never sign: the signing
side lives in ``ai_hydro.approval.signing``, imported only by the writer and
the CLI.

Trust model
-----------
A v2 approval record carries an SSHSIG over the UTF-8 bytes of its
``record_digest`` (namespace ``aihydro-approval@v1``). The record is accepted
only when the signature verifies against an ``allowed_signers`` file:

* ``/etc/aihydro/allowed_signers`` when present (root-owned in a sane
  deployment; ``trust_root: "system"``). Override the path for tests with
  ``AIHYDRO_SYSTEM_TRUST_FILE``.
* else ``$AIHYDRO_HOME/trust/allowed_signers`` (``trust_root: "user_writable"``:
  a same-user process can edit it, so this only blocks processes that do not
  edit it - labelled, never presented as strong).
* or a file the verifier supplies (``allowed_signers=...``; ``trust_root:
  "supplied"``), the off-machine verification path.

``valid-after``/``valid-before`` in the file are enforced by ``ssh-keygen``
at verification time (now), so a rotated-out key stops verifying; an
unconsumed approval signed by it must be redone.

The channel is derived here, never read from the record:

    ssh_sig_sk     key type sk-ssh-ed25519@openssh.com / sk-ecdsa-sha2-nistp256@openssh.com,
                   signature verifies, and the allowed_signers line does not
                   carry ``no-touch-required`` (touch per signature)
    ssh_sig        any other verifying key (software key, agent key)
    cli_same_user  legacy v1 record; never upgraded; refused when signatures
                   are required
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from aihydro_core.records import digest

from ai_hydro.registry.paths import aihydro_home

NAMESPACE = "aihydro-approval@v1"
SIG_FORMAT = "sshsig"
CHANNEL_SK = "ssh_sig_sk"
CHANNEL_SSH = "ssh_sig"
CHANNEL_LEGACY = "cli_same_user"
SK_KEY_TYPES = ("sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
SYSTEM_TRUST_FILE = "/etc/aihydro/allowed_signers"
REVOCATION_SCHEMA = "aihydro.approval_revocation/1"
_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Paths and policy
# ---------------------------------------------------------------------------

def system_trust_file() -> Path:
    return Path(os.environ.get("AIHYDRO_SYSTEM_TRUST_FILE") or SYSTEM_TRUST_FILE)


def user_trust_file() -> Path:
    return aihydro_home() / "trust" / "allowed_signers"


def revocations_file() -> Path:
    return aihydro_home() / "approvals" / "revocations.jsonl"


def config_file() -> Path:
    return aihydro_home() / "approvals" / "config.json"


def trust_root() -> tuple:
    """``(path, label)``: system file if present, else user file if present, else ``(None, None)``."""
    sys_file = system_trust_file()
    if sys_file.is_file():
        return sys_file, "system"
    usr = user_trust_file()
    if usr.is_file():
        return usr, "user_writable"
    return None, None


def _truthy(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return None


def require_signed() -> bool:
    """Whether unsigned (v1, ``cli_same_user``) approvals are refused.

    A system trust root forces True (a user-writable flag must not be able to
    weaken a root-owned policy). Otherwise ``AIHYDRO_REQUIRE_SIGNED`` or
    ``{"require_signed": bool}`` in ``$AIHYDRO_HOME/approvals/config.json``
    decides; the default is True when any allowed_signers file exists.
    """
    _, label = trust_root()
    if label == "system":
        return True
    env = _truthy(os.environ.get("AIHYDRO_REQUIRE_SIGNED"))
    if env is not None:
        return env
    try:
        cfg = json.loads(config_file().read_text(encoding="utf-8"))
        flag = _truthy(cfg.get("require_signed")) if isinstance(cfg, dict) else None
        if flag is not None:
            return flag
    except (OSError, ValueError):
        pass
    return label is not None


# ---------------------------------------------------------------------------
# SSHSIG / key helpers (pure Python, no subprocess)
# ---------------------------------------------------------------------------

def key_fingerprint(pubkey_blob: bytes) -> str:
    """``SHA256:<b64>`` exactly as ``ssh-keygen -lf`` prints it."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(pubkey_blob).digest()).decode().rstrip("=")


def _read_string(buf: bytes, off: int) -> tuple:
    (n,) = struct.unpack_from(">I", buf, off)
    return buf[off + 4: off + 4 + n], off + 4 + n


def parse_sshsig(armored: str) -> dict:
    """``{pubkey_blob, key_type, fingerprint, namespace}`` from an armored SSHSIG.

    Raises ``ValueError`` on anything malformed.
    """
    try:
        lines = [ln.strip() for ln in armored.strip().splitlines()]
        if lines[0] != "-----BEGIN SSH SIGNATURE-----" or lines[-1] != "-----END SSH SIGNATURE-----":
            raise ValueError("not an armored SSH signature")
        blob = base64.b64decode("".join(lines[1:-1]), validate=True)
        if blob[:6] != b"SSHSIG":
            raise ValueError("bad SSHSIG magic")
        off = 10                                   # magic + uint32 version
        pubkey, off = _read_string(blob, off)
        namespace, off = _read_string(blob, off)
        key_type, _ = _read_string(pubkey, 0)
        return {"pubkey_blob": pubkey, "key_type": key_type.decode("ascii"),
                "fingerprint": key_fingerprint(pubkey), "namespace": namespace.decode("utf-8")}
    except (IndexError, struct.error, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError(f"malformed SSH signature: {exc}") from exc


def _tokens(line: str) -> list:
    return [m.group(0) for m in re.finditer(r'(?:[^\s"]|"[^"]*")+', line)]


def _allowed_signer_lines(path: Path) -> list:
    """``[{principals, options, key_type, blob_b64}]`` for each usable line."""
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        toks = _tokens(raw)
        if len(toks) < 3:
            continue
        i = 1
        options = []
        if not re.match(r"^(ssh-|ecdsa-|sk-)", toks[i]):
            options = [o.strip().lower() for o in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", toks[i])]
            i += 1
        if i + 1 >= len(toks):
            continue
        out.append({"principals": toks[0], "options": options,
                    "key_type": toks[i], "blob_b64": toks[i + 1]})
    return out


def _matching_line(path: Path, pubkey_blob: bytes) -> Optional[dict]:
    want = base64.b64encode(pubkey_blob).decode()
    for line in _allowed_signer_lines(path):
        if line["blob_b64"] == want:
            return line
    return None


# ---------------------------------------------------------------------------
# Revocations
# ---------------------------------------------------------------------------

def revoked_fingerprints() -> set:
    """Fingerprints with an intact sealed revocation line."""
    out = set()
    try:
        with open(revocations_file(), encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    body = {k: v for k, v in rec.items() if k != "record_digest"}
                    if (rec.get("schema") == REVOCATION_SCHEMA and rec.get("fingerprint")
                            and digest(body) == rec.get("record_digest")):
                        out.add(rec["fingerprint"])
                except (ValueError, AttributeError, TypeError):
                    continue
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    ok: bool
    channel: Optional[str] = None
    reason: str = ""
    signer: Optional[dict] = None
    trust_root: Optional[str] = None
    principal: Optional[str] = None

    def stamp(self) -> dict:
        return {"channel": self.channel, "signer": self.signer, "trust_root": self.trust_root}


def _ssh_keygen() -> Optional[str]:
    return shutil.which("ssh-keygen")


def _run(args: list, data: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.run(args, input=data, capture_output=True, timeout=_TIMEOUT)


def verify_signature(record: dict, *, allowed_signers: Optional[Any] = None) -> Verdict:
    """Verify a v2 record's signature, signer binding, trust and revocation."""
    sig = record.get("signature")
    signer = record.get("signer")
    if not isinstance(sig, dict) or sig.get("format") != SIG_FORMAT or sig.get("namespace") != NAMESPACE \
            or not isinstance(sig.get("armored"), str):
        return Verdict(False, reason="missing or malformed signature")
    if not isinstance(signer, dict) or not signer.get("fingerprint") or not signer.get("key_type"):
        return Verdict(False, reason="missing signer")
    try:
        parsed = parse_sshsig(sig["armored"])
    except ValueError as exc:
        return Verdict(False, reason=str(exc))
    if parsed["namespace"] != NAMESPACE:
        return Verdict(False, reason="signature namespace mismatch")
    if parsed["fingerprint"] != signer["fingerprint"] or parsed["key_type"] != signer["key_type"]:
        return Verdict(False, reason="signature key does not match the sealed signer")
    info = {"fingerprint": parsed["fingerprint"], "key_type": parsed["key_type"]}

    if allowed_signers is not None:
        trust_path, label = Path(allowed_signers), "supplied"
        if not trust_path.is_file():
            return Verdict(False, reason="allowed_signers file not found", signer=info)
    else:
        trust_path, label = trust_root()
        if trust_path is None:
            return Verdict(False, reason="no allowed_signers trust root is configured", signer=info)
    base = dict(signer=info, trust_root=label)

    if parsed["fingerprint"] in revoked_fingerprints():
        return Verdict(False, reason="signing key has been revoked", **base)
    line = _matching_line(trust_path, parsed["pubkey_blob"])
    if line is None:
        return Verdict(False, reason="signing key is not in allowed_signers", **base)
    exe = _ssh_keygen()
    if exe is None:
        return Verdict(False, reason="ssh-keygen not found", **base)

    data = record["record_digest"].encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="aihydro-sig-") as tmp:
        sig_path = Path(tmp) / "record.sig"
        sig_path.write_text(sig["armored"].strip() + "\n", encoding="utf-8")
        try:
            found = _run([exe, "-Y", "find-principals", "-f", str(trust_path), "-s", str(sig_path)])
            principals = [p for p in found.stdout.decode("utf-8", "replace").split() if p] \
                if found.returncode == 0 else []
            if not principals:
                return Verdict(False, reason="no allowed signer matches this signature", **base)
            for principal in principals:
                res = _run([exe, "-Y", "verify", "-f", str(trust_path), "-I", principal,
                            "-n", NAMESPACE, "-s", str(sig_path)], data)
                if res.returncode == 0:
                    sk = parsed["key_type"] in SK_KEY_TYPES and "no-touch-required" not in line["options"]
                    return Verdict(True, CHANNEL_SK if sk else CHANNEL_SSH, "verified", principal=principal, **base)
            reason = (res.stderr or res.stdout).decode("utf-8", "replace").strip().splitlines()
            return Verdict(False, reason=f"signature not accepted: {reason[0] if reason else 'verify failed'}", **base)
        except (OSError, subprocess.SubprocessError) as exc:
            return Verdict(False, reason=f"ssh-keygen failed: {exc}", **base)


def check_approval(record: dict, *, allowed_signers: Optional[Any] = None) -> Verdict:
    """Authority verdict for a structurally intact record (``verify_record`` passed)."""
    schema = record.get("schema")
    if schema == "aihydro.approval/1":
        if require_signed() and allowed_signers is None:
            return Verdict(False, reason="unsigned (v1) approvals are refused: signatures are required")
        if allowed_signers is not None:
            return Verdict(False, reason="unsigned (v1) approval cannot be verified against a supplied trust root")
        return Verdict(True, CHANNEL_LEGACY, "legacy unsigned record")
    if schema == "aihydro.approval/2":
        return verify_signature(record, allowed_signers=allowed_signers)
    return Verdict(False, reason=f"unknown approval schema {schema!r}")
