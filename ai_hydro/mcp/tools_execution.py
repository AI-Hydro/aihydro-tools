"""
Python execution and CLI discovery tools.

run_python: workspace-scoped subprocess for researcher Python scripts.
list_relevant_clis: enumerate installed AI-Hydro-aware CLI tools.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp")

_BLOCKED_PATTERNS = ("pip install", "pip3 install", "__import__('ai_hydro")


@mcp.tool()
def run_python(
    script: str,
    workspace_dir: str,
    timeout_seconds: int = 120,
    allow_network: bool = False,
) -> dict:
    """
    Execute a Python script in the researcher's workspace directory.

    Scripts run as a subprocess using the same interpreter that serves the MCP
    server. Output is captured and returned. Designed for data processing,
    analysis, and ad-hoc computation — not for long-running training (use
    train_hydro_model for that).

    Security surface
    ----------------
    - Workspace-scoped: cwd is set to workspace_dir; absolute paths outside
      the workspace are refused.
    - No network by default: allow_network=False injects a socket-blocking
      preamble so scripts cannot reach the internet accidentally.
    - Timeout: hard-killed at timeout_seconds (default 120 s).
    - No shell interpolation: script is passed via stdin to python -, never
      via shell=True.
    - No pip: scripts that call pip install are rejected before execution.

    Parameters
    ----------
    script : str
        Python source to execute. Must not call pip install.
    workspace_dir : str
        Absolute path to the researcher's workspace. Script cwd is set here.
        Paths are resolved and checked to stay within workspace_dir.
    timeout_seconds : int, optional
        Hard timeout (default 120). Use train_hydro_model for longer work.
    allow_network : bool, optional
        If False (default), socket.socket is shimmed to raise RuntimeError
        so accidental network calls fail clearly.

    Returns
    -------
    dict with stdout, stderr, returncode, duration_seconds, workspace_dir.
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
                "_orig_socket = _socket.socket",
                "class _NoNetSocket(_socket.socket):",
                "    def __init__(self, *a, **kw):",
                "        raise RuntimeError('Network access is disabled (allow_network=False). Set allow_network=True to enable.')",
                "_socket.socket = _NoNetSocket",
            ]
        preamble = "\n".join(preamble_parts) + "\n"
        full_script = preamble + script

        # Prepare environment
        env = os.environ.copy()
        if not allow_network:
            env["NO_PROXY"] = "*"
            env["no_proxy"] = "*"

        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-"],
            input=full_script,
            capture_output=True,
            text=True,
            cwd=str(ws),
            timeout=timeout_seconds,
            env=env,
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
    List AI-Hydro-aware command-line tools that are currently installed.

    Returns tools registered via the aihydro.clis entry-point group, plus
    the built-in aihydro-mcp binary. Use this to discover what domain CLIs
    (e.g. swat, camels-extract) are available before driving them via shell.

    Returns
    -------
    dict with clis (list of descriptors) and n_clis.
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
