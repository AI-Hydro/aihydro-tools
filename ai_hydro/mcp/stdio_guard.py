"""Keep fd 1 reserved for the MCP stdio protocol.

The MCP stdio transport speaks newline-delimited JSON-RPC on the process's
stdout. Anything else written to fd 1 while a tool runs (a ``print()`` in our
code or in a third-party library, a progress bar, a C extension calling
``write(1, ...)``) is interleaved with the protocol. A short stray line makes
the client report a parse error; a long burst flushes mid-line and gets glued
onto the front of a JSON-RPC response, which then cannot be parsed and the call
hangs until its timeout.

Fixing individual ``print()`` calls cannot cover libraries or C code, so this is
done once, for the whole process, at server start:

1. ``os.dup(1)`` keeps the original stdout pipe as a private *protocol fd*;
2. ``os.dup2(2, 1)`` points fd 1 at stderr, so every write to fd 1 (Python,
   third-party, C-level) lands in the log stream the extension already captures;
3. ``sys.stdout`` is rebound to ``sys.stderr``;
4. the MCP stdio transport is served on the protocol fd only.

Step 4 needs a small replacement for ``FastMCP.run_stdio_async``: FastMCP's
version calls ``mcp.server.stdio.stdio_server()`` with no arguments, which binds
to ``sys.stdout.buffer`` and has no hook to pass another stream. The replacement
is the same body as FastMCP 2.14's, plus an explicit ``stdout=``. It relies on
two private FastMCP names (``_lifespan_manager``, ``_mcp_server``), which is why
``fastmcp`` is bounded ``>=2.14,<3`` in ``pyproject.toml`` and why
``tests/test_stdio_protocol_guard.py`` exercises it end to end. If a future
FastMCP renames them the server fails at start with an explicit message rather
than silently losing the guard.

Windows: ``os.dup``/``os.dup2`` work on CRT file descriptors, pipes included.
``tests/test_stdio_protocol_guard.py`` runs on Windows and macOS in CI (the
``stdio guard`` jobs), including a child process inheriting stdout and raw
``os.write(1, ...)``. One Windows-specific hazard, independent of this guard: a
child that inherits the server's *stdin* pipe blocks in start-up while the
transport has a synchronous read pending on it, so tools that spawn
subprocesses should pass ``stdin=subprocess.DEVNULL``.

Not covered: anything written to fd 1 before the guard is installed, i.e. at
import time of ``ai_hydro.mcp`` (the guard is installed first thing in
``main()``). Such output is buffered or written to the original stdout before
the redirect.
"""

from __future__ import annotations

import os
import sys
from io import TextIOWrapper
from typing import Any

_protocol_fd: int | None = None


def install_stdout_guard() -> int:
    """Reserve the original stdout for the protocol; return its saved fd.

    Idempotent: later calls return the same fd without redirecting again.
    """
    global _protocol_fd
    if _protocol_fd is not None:
        return _protocol_fd

    # Whatever is already buffered on the Python-level stdout was not protocol
    # output. Redirect first, then flush, so those bytes go to stderr.
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    old_stdout = sys.stdout
    try:
        if old_stdout is not None:
            old_stdout.flush()
    except Exception:  # a closed or detached stdout must not stop startup
        pass
    sys.stdout = sys.stderr
    _protocol_fd = proto_fd
    return proto_fd


def run_stdio_guarded(app: Any, show_banner: bool = True) -> None:
    """Serve ``app`` (a FastMCP server) over stdio on the protected protocol fd."""
    import anyio
    from mcp.server.lowlevel.server import NotificationOptions
    from mcp.server.stdio import stdio_server

    if not (hasattr(app, "_lifespan_manager") and hasattr(app, "_mcp_server")):
        raise RuntimeError(
            "aihydro stdio guard needs FastMCP 2.14.x internals "
            "(_lifespan_manager, _mcp_server); install fastmcp>=2.14,<3"
        )

    proto_fd = install_stdout_guard()
    proto_out = anyio.wrap_file(
        TextIOWrapper(os.fdopen(proto_fd, "wb", closefd=False), encoding="utf-8", newline="\n")
    )

    async def _serve() -> None:
        if show_banner:
            from fastmcp.utilities.cli import log_server_banner

            log_server_banner(server=app)
        async with app._lifespan_manager():
            async with stdio_server(stdout=proto_out) as (read_stream, write_stream):
                await app._mcp_server.run(
                    read_stream,
                    write_stream,
                    app._mcp_server.create_initialization_options(
                        notification_options=NotificationOptions(tools_changed=True),
                    ),
                )

    anyio.run(_serve)
