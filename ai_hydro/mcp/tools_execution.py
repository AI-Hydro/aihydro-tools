"""
Python execution and CLI discovery tools.

run_python: workspace-scoped subprocess for researcher Python scripts.
list_relevant_clis: enumerate installed AI-Hydro-aware CLI tools.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp")

_BLOCKED_PATTERNS = ("pip install", "pip3 install", "__import__('ai_hydro")

# Env-var name patterns that look secret-shaped. Matched case-insensitively
# against the *name*, not the value. This is a scrub, not a security boundary:
# a script can still read credential *files* under $HOME (e.g. GEE's
# ~/.config/earthengine/credentials) — see the run_python docstring.
_SECRET_ENV_PATTERNS = re.compile(
    r"(_KEY$|_TOKEN$|_SECRET$|CREDENTIAL|_PASSWORD$|^AWS_|^HF_|^HUGGING_FACE|"
    r"^EARTHENGINE|^GOOGLE_APPLICATION|^GEE_|^ANTHROPIC_|^OPENAI_)",
    re.IGNORECASE,
)


def _scrub_env(env: dict) -> dict:
    """
    Strip secret-shaped variables from a subprocess environment.

    Blocklist (not allowlist): preserves PATH/HOME/PYTHONPATH/CONDA_PREFIX/etc
    so legitimate scripts (including allow_network=True GEE/HF workflows) keep
    working, while dropping anything that looks like an API key, token, or
    credential. Applied unconditionally — stdout/stderr are returned to the
    calling agent regardless of allow_network, so a leaked secret doesn't need
    network access to exfiltrate, only a print().
    """
    return {k: v for k, v in env.items() if not _SECRET_ENV_PATTERNS.search(k)}


def _rlimit_preexec():
    """
    Best-effort POSIX resource caps for the run_python subprocess.

    CPU time, output file size, and open-file-count are bounded to contain a
    runaway script. RLIMIT_AS (address space) is deliberately NOT set: on
    arm64 macOS it produces spurious ENOMEM/SIGSEGV during numpy/pandas import
    well below any real memory abuse, making it a false-positive machine
    rather than a useful control.
    """
    import resource

    def _set():
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
        except (ValueError, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 1024 * 1024, 200 * 1024 * 1024))
        except (ValueError, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        except (ValueError, OSError):
            pass

    return _set


@mcp.tool()
def run_python(
    script: str,
    workspace_dir: str,
    timeout_seconds: int = 120,
    allow_network: bool = False,
) -> dict:
    """
    Execute a Python snippet in a workspace-scoped subprocess.

    NOT a security boundary — do not run untrusted code. This is process
    isolation with best-effort guardrails, not a sandbox or OS jail:
      - Network: off by default via a socket-class override the child process
        can defeat with enough effort (allow_network=True opts in cleanly).
      - Environment: secret-shaped variable NAMES (API keys/tokens/credentials)
        are stripped before the child starts, but credential *files* readable
        under $HOME (e.g. GEE's ~/.config/earthengine/credentials) remain
        readable — this does not stop a script from reading them from disk.
      - Filesystem: cwd is set to workspace_dir but this is not enforced as a
        jail; the child can read/write elsewhere the host user can.
      - Resources: hard wall-clock timeout, plus best-effort POSIX CPU/file-size/
        open-file caps (not memory — see _rlimit_preexec).

    PREFER the bash tool for: shell commands, CLI invocations, file ops,
    package checks, or any task where a subprocess is cleaner. Use
    run_python when you need: (a) best-effort network-off, (b) to run
    multi-line Python logic that imports ai_hydro internals directly, or
    (c) a hard timeout guard on long-running compute you trust.

    Constraints: network off by default (allow_network=True to opt in),
    pip install rejected, hard timeout 120 s (raise to max ~600 s).
    Returns stdout, stderr, returncode, duration_seconds.
    """
    try:
        # Validate workspace path
        ws = Path(workspace_dir).resolve()
        if not ws.exists():
            return {
                "error": True,
                "code": "WORKSPACE_NOT_FOUND",
                "message": f"workspace_dir does not exist: {workspace_dir}",
                "recovery": "Pass the absolute path returned by start_session.",
            }

        # Reject pip installs
        for pat in _BLOCKED_PATTERNS:
            if pat in script:
                return {
                    "error": True,
                    "code": "BLOCKED_OPERATION",
                    "message": f"Scripts must not contain '{pat}'. Report missing packages to the researcher.",
                    "recovery": "Remove the pip install call. Missing packages must be installed by the researcher.",
                }

        # Build preamble
        preamble_parts = [
            "import os as _os, sys as _sys",
            f"_ws = _os.path.realpath({str(ws)!r})",
        ]
        if not allow_network:
            preamble_parts += [
                "import socket as _socket",
                "class _NoNetSocket(_socket.socket):",
                "    def __init__(self, *a, **kw):",
                "        raise RuntimeError('Network access is disabled (allow_network=False). Set allow_network=True to enable.')",
                "_socket.socket = _NoNetSocket",
                "del _socket",
            ]
        preamble = "\n".join(preamble_parts) + "\n"
        full_script = preamble + script

        # Prepare environment: scrub secret-shaped vars unconditionally (stdout
        # is returned to the caller regardless of allow_network — see docstring)
        env = _scrub_env(os.environ.copy())
        if not allow_network:
            env["NO_PROXY"] = "*"
            env["no_proxy"] = "*"

        run_kwargs = {}
        if os.name == "posix":
            run_kwargs["preexec_fn"] = _rlimit_preexec()

        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-"],
            input=full_script,
            capture_output=True,
            text=True,
            cwd=str(ws),
            timeout=timeout_seconds,
            env=env,
            **run_kwargs,
        )
        duration = time.monotonic() - start

        return {
            "returncode": result.returncode,
            "stdout": result.stdout[-10000:] if len(result.stdout) > 10000 else result.stdout,
            "stderr": result.stderr[-5000:] if len(result.stderr) > 5000 else result.stderr,
            "duration_seconds": round(duration, 3),
            "workspace_dir": str(ws),
            "allow_network": allow_network,
            "timeout_seconds": timeout_seconds,
        }
    except subprocess.TimeoutExpired:
        return {
            "error": True,
            "code": "TIMEOUT",
            "message": f"Script exceeded {timeout_seconds}s timeout.",
            "recovery": "Break the script into smaller steps, or use train_hydro_model for long-running work.",
        }
    except Exception as e:
        log.error("run_python failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def list_relevant_clis() -> dict:
    """
    List installed AI-Hydro-aware CLIs (registered via aihydro.clis entry-point,
    plus aihydro-mcp). Use before driving any domain CLI from a shell.
    """
    try:
        clis = []

        # Built-in: aihydro-mcp
        aihydro_mcp = shutil.which("aihydro-mcp")
        if aihydro_mcp:
            clis.append({
                "name": "aihydro-mcp",
                "binary": aihydro_mcp,
                "description": "AI-Hydro MCP server (this server)",
                "help_subcommand": "--help",
            })

        # Plugin-registered CLIs via aihydro.clis entry-point
        try:
            from importlib.metadata import entry_points
            eps = entry_points(group="aihydro.clis")
            for ep in eps:
                try:
                    descriptor_fn = ep.load()
                    desc = descriptor_fn()
                    if isinstance(desc, dict):
                        clis.append(desc)
                except Exception as exc:
                    log.warning("Failed to load CLI descriptor %s: %s", ep.name, exc)
        except Exception as exc:
            log.debug("aihydro.clis entry-point discovery failed: %s", exc)

        # Best-effort: detect known community CLIs even without entry-points
        known = [
            ("swat", "End-to-end SWAT+ watershed setup, run, and calibration (swatplus-builder)"),
            ("camels-extract", "CAMELS-style catchment attribute extraction (camels-attrs)"),
        ]
        registered_binaries = {c.get("binary") or c.get("name") for c in clis}
        for binary, description in known:
            if binary not in registered_binaries and shutil.which(binary):
                clis.append({
                    "name": binary,
                    "binary": shutil.which(binary),
                    "description": description,
                    "help_subcommand": "--help",
                    "note": "Detected without entry-point registration; install the full plugin for full integration.",
                })

        return {
            "clis": clis,
            "n_clis": len(clis),
            "_note": (
                "Community packages register CLIs via [project.entry-points.'aihydro.clis'] "
                "in their pyproject.toml. Restart the MCP server to pick up newly installed plugins."
            ),
        }
    except Exception as e:
        log.error("list_relevant_clis failed: %s", e)
        return _tool_error_to_dict(e)
