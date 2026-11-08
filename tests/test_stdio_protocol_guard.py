"""stdout is reserved for the MCP stdio protocol (ai_hydro/mcp/stdio_guard.py).

A tool that prints (Python ``print``, progress-bar style ``\\r`` / ``end=""``,
``os.write(1, ...)``, a burst larger than the pipe/stdio buffers) must not put
a single non-JSON-RPC byte on the server's stdout. Real subprocess, raw pipes,
no network. Each scenario runs with and without ``PYTHONUNBUFFERED``.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_TOOLS = textwrap.dedent(
    '''
    import os, sys

    def register(mcp):
        @mcp.tool()
        def chatty() -> str:
            for i in range(2000):
                print(f"  - progress line {i}")
            return "done-chatty"

        @mcp.tool()
        def progress_bar() -> str:
            for i in range(0, 101, 10):
                print(f"\\rprogress {i}%", end="")
            return "done-bar"

        @mcp.tool()
        def raw_fd() -> str:
            os.write(1, b"raw c-level line\\n")
            os.write(1, b"no newline at the end of this one")
            return "done-raw"

        @mcp.tool()
        def big_burst() -> str:
            # far beyond the 64 KiB pipe buffer and the 8 KiB stdio buffer
            sys.stdout.write("x" * 400_000)
            os.write(1, b"y" * 400_000)
            return "done-burst"

        @mcp.tool()
        def lib_print() -> str:
            import subprocess
            # a child inheriting our stdout AND stdin (third-party / native helpers). On
            # Windows an inherited stdin pipe blocks the child in start-up (found in CI).
            subprocess.run([sys.executable, "-c", "print('child says hi')"], check=True)
            return "done-lib"

        @mcp.tool()
        def bare_popen() -> str:
            import subprocess
            p = subprocess.Popen([sys.executable, "-c", "import sys; print('bare child'); sys.stdin.read()"])
            try:
                p.wait(timeout=30)   # the child reads stdin: it must see EOF from the null device
            finally:
                if p.poll() is None:
                    p.kill()
            return "done-bare"
    '''
)

_MINIMAL_SERVER = textwrap.dedent(
    '''
    import sys
    sys.path.insert(0, {tools_dir!r})
    from fastmcp import FastMCP
    from stdio_tools import register
    from ai_hydro.mcp.stdio_guard import install_stdout_guard, run_stdio_guarded

    app = FastMCP("guard-test")
    register(app)
    install_stdout_guard()  # what main() does first thing
    print("print after the guard, before serving")
    run_stdio_guarded(app, show_banner=False)
    '''
)

_UNGUARDED_SERVER = textwrap.dedent(
    '''
    import sys
    sys.path.insert(0, {tools_dir!r})
    from fastmcp import FastMCP
    from stdio_tools import register

    app = FastMCP("guard-test")
    register(app)
    app.run(show_banner=False)
    '''
)

_REAL_MAIN_SERVER = textwrap.dedent(
    '''
    import sys
    sys.path.insert(0, {tools_dir!r})
    sys.argv = ["aihydro-mcp"]
    import ai_hydro.mcp as m
    from stdio_tools import register
    register(m.mcp)
    m.main()
    '''
)

_TOOL_NAMES = ["chatty", "progress_bar", "raw_fd", "big_burst", "lib_print", "bare_popen"]


def _drive(tmp_path: Path, template: str, unbuffered: bool, tools: list[str], timeout: float = 60.0):
    (tmp_path / "stdio_tools.py").write_text(_TOOLS)
    script = tmp_path / "server.py"
    script.write_text(template.format(tools_dir=str(tmp_path)))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    # Inherit the environment: Windows needs SYSTEMROOT etc. just to start Python.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "AIHYDRO_HOME": str(home / "aihydro"),
            "PYTHONPATH": os.pathsep.join(
                [str(REPO)] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
            ),
            "TMPDIR": str(tmp_path),
            "TEMP": str(tmp_path),
            "TMP": str(tmp_path),
        }
    )
    if unbuffered:
        env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
    )
    lines: "queue.Queue[bytes]" = queue.Queue()
    err_chunks: list[bytes] = []

    def _pump(stream, sink):
        for chunk in iter(lambda: stream.readline(), b""):
            sink(chunk)

    threading.Thread(target=_pump, args=(proc.stdout, lines.put), daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr, err_chunks.append), daemon=True).start()

    def send(msg):
        try:
            proc.stdin.write((json.dumps(msg) + "\n").encode())
            proc.stdin.flush()
        except OSError as exc:  # child is gone: say why instead of a bare Errno 22
            try:
                rc = proc.wait(5)
            except subprocess.TimeoutExpired:
                rc = None
            time.sleep(0.5)
            raise AssertionError(
                f"server died ({exc!r}); exit code {rc}; stderr:\n"
                + b"".join(err_chunks).decode("utf-8", "replace")[-4000:]
            ) from exc

    all_lines: list[bytes] = []

    def wait_for(msg_id):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = lines.get(timeout=0.2)
            except queue.Empty:
                if proc.poll() is not None and lines.empty():
                    break
                continue
            all_lines.append(raw)
            try:
                if json.loads(raw).get("id") == msg_id:
                    return json.loads(raw)
            except Exception:
                pass
        return None

    responses = {}
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"}}})
        wait_for(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        for n, name in enumerate(tools, start=2):
            send({"jsonrpc": "2.0", "id": n, "method": "tools/call",
                  "params": {"name": name, "arguments": {}}})
            responses[name] = wait_for(n)
        proc.stdin.close()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        # whatever else reached stdout before exit
        while True:
            try:
                all_lines.append(lines.get(timeout=0.5))
            except queue.Empty:
                break
    finally:
        if proc.poll() is None:
            proc.kill()
    return all_lines, responses, b"".join(err_chunks)


def _non_jsonrpc(lines):
    bad = []
    for raw in lines:
        try:
            obj = json.loads(raw)
            assert obj.get("jsonrpc") == "2.0"
        except Exception:
            bad.append(raw[:80])
    return bad


@pytest.mark.parametrize("unbuffered", [False, True], ids=["default-buffering", "PYTHONUNBUFFERED"])
@pytest.mark.parametrize(
    "template", [_MINIMAL_SERVER, _REAL_MAIN_SERVER], ids=["run_stdio_guarded", "ai_hydro.mcp.main"]
)
def test_stdout_carries_only_json_rpc(tmp_path, template, unbuffered):
    lines, responses, stderr = _drive(tmp_path, template, unbuffered, _TOOL_NAMES)

    assert _non_jsonrpc(lines) == [], "non-protocol bytes reached the server's stdout"
    for name in _TOOL_NAMES:
        resp = responses[name]
        assert resp is not None, f"no response for {name}; stderr tail:\n" + stderr.decode("utf-8", "replace")[-3000:]
        assert "error" not in resp, resp
        assert "done-" in json.dumps(resp)

    # the prints were diverted, not dropped
    assert b"progress line 1999" in stderr
    assert b"raw c-level line" in stderr
    assert b"child says hi" in stderr
    assert b"bare child" in stderr
    assert b"x" * 1000 in stderr and b"y" * 1000 in stderr


def test_unguarded_control_is_corrupted(tmp_path):
    """Control: without the guard the same tools do corrupt stdout (test has teeth)."""
    lines, _responses, _ = _drive(
        tmp_path, _UNGUARDED_SERVER, False, ["chatty"], timeout=15.0
    )
    assert _non_jsonrpc(lines), "expected the unguarded server to leak prints onto stdout"
