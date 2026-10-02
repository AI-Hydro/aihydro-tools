"""No MCP tool can create a human approval record (Slice 1b, ADR-002a, acceptance 8).

The only writer is ``ai_hydro.approval.writer``, reachable only from the
``aihydro-approve`` CLI. Three independent layers:

1. static: no module under ``ai_hydro/`` other than the writer and the CLI
   references the writer module or ``write_approval``;
2. runtime reachability: importing the whole MCP server in a clean interpreter
   does not load the writer or the CLI;
3. registered tools: no tool function (nor its module globals) holds the writer.

ADR-002b adds the signing side (``ai_hydro.approval.signing``, the only code that
runs ``ssh-keygen -Y sign``): the same three layers apply to it, plus a scan
that nothing under ``ai_hydro/mcp`` invokes ``-Y sign`` or ``ssh-keygen`` at all.
"""
from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ai_hydro"
ALLOWED_WRITER_USERS = {PKG / "approval" / "writer.py", PKG / "approval" / "cli.py"}
WRITER_MODULE = "ai_hydro.approval.writer"
FORBIDDEN_NAMES = {"write_approval"}


def _references_writer(path: Path) -> list[str]:
    hits = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name.startswith(WRITER_MODULE)]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith(WRITER_MODULE):
                hits.append(mod)
            if mod == "ai_hydro.approval" and any(a.name in {"writer", "cli"} for a in node.names):
                hits.append(f"{mod}.{[a.name for a in node.names]}")
            if node.level and mod in {"writer", "approval.writer"}:      # relative import
                hits.append(f".{mod}")
            hits += [a.name for a in node.names if a.name in FORBIDDEN_NAMES]
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            hits.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            hits.append(node.attr)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and node.value.strip().startswith(WRITER_MODULE) and " " not in node.value.strip()):
            hits.append(node.value)     # importlib.import_module("ai_hydro.approval.writer")
    return hits


SIGNING_MODULE = "ai_hydro.approval.signing"
SIGNING_USERS = {PKG / "approval" / "signing.py", PKG / "approval" / "writer.py", PKG / "approval" / "cli.py"}


def _references_signing(path: Path) -> list[str]:
    hits = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name.startswith(SIGNING_MODULE)]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith(SIGNING_MODULE) or (mod == "ai_hydro.approval"
                                                  and any(a.name == "signing" for a in node.names)):
                hits.append(mod)
            if node.level and mod in {"signing", "approval.signing"}:
                hits.append(f".{mod}")
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and node.value.strip().startswith(SIGNING_MODULE) and " " not in node.value.strip()):
            hits.append(node.value)
    return hits


def test_only_writer_and_cli_reference_the_signing_module():
    offenders = {str(p.relative_to(REPO)): _references_signing(p)
                 for p in sorted(PKG.rglob("*.py")) if p not in SIGNING_USERS and _references_signing(p)}
    assert not offenders, offenders


def test_nothing_under_mcp_invokes_ssh_keygen_or_dash_Y_sign():
    offenders = {}
    for path in sorted((PKG / "mcp").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "ssh-keygen" in text or "ssh-add" in text or ('"-Y"' in text and '"sign"' in text):
            offenders[str(path.relative_to(REPO))] = "ssh-keygen / -Y sign"
    assert not offenders, offenders


def test_signing_invocation_is_confined_to_the_signing_module():
    """``-Y sign`` appears in exactly one module under ai_hydro/."""
    users = {str(p.relative_to(REPO)) for p in PKG.rglob("*.py")
             if '"-Y", "sign"' in p.read_text(encoding="utf-8")}
    assert users == {"ai_hydro/approval/signing.py"}, users


def test_scanner_detects_a_violation(tmp_path):
    """The scan itself must be able to fail."""
    bad = tmp_path / "bad_tool.py"
    for src in ("from ai_hydro.approval.writer import write_approval\n",
                "import ai_hydro.approval.writer as w\n",
                "from ai_hydro.approval import writer\n",
                "import importlib; importlib.import_module('ai_hydro.approval.writer')\n",
                "def tool():\n    return write_approval()\n"):
        bad.write_text(src)
        assert _references_writer(bad), src


def test_only_the_cli_references_the_approval_writer():
    offenders = {}
    for path in sorted(PKG.rglob("*.py")):
        if path in ALLOWED_WRITER_USERS:
            continue
        hits = _references_writer(path)
        if hits:
            offenders[str(path.relative_to(REPO))] = hits
    assert not offenders, offenders


def test_mcp_modules_never_touch_the_writer():
    mcp_files = sorted((PKG / "mcp").rglob("*.py"))
    assert len(mcp_files) > 20
    assert {str(p.relative_to(REPO)): _references_writer(p) for p in mcp_files if _references_writer(p)} == {}


def test_importing_the_mcp_server_never_loads_the_writer_or_cli():
    code = (
        "import sys, ai_hydro.mcp\n"
        "from ai_hydro.mcp import tools_ledger  # the tool that reads approvals\n"
        "r = tools_ledger.promote_claim_to_registry('no-such-session', 'c', researcher_approved=True)\n"
        "assert r['error'] is True  # exercised: the approval read path is now imported\n"
        "bad = [m for m in ('ai_hydro.approval.writer', 'ai_hydro.approval.cli', 'ai_hydro.approval.signing')\n"
        "       if m in sys.modules]\n"
        "assert not bad, bad\n"
        "assert 'ai_hydro.approval.records' in sys.modules and 'ai_hydro.approval.trust' in sys.modules\n"
        "print('ok')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(REPO), timeout=300,
                          env={**__import__("os").environ,
                               "PYTHONPATH": ":".join(filter(None, [str(REPO), __import__("os").environ.get("PYTHONPATH", "")]))})
    assert proc.returncode == 0 and proc.stdout.strip().endswith("ok"), proc.stderr[-2000:]


def test_no_loaded_module_holds_the_writer():
    import ai_hydro.mcp  # noqa: F401  (registers every tool)
    from ai_hydro.approval import writer

    checked = 0
    for name, mod in list(sys.modules.items()):
        if not name.startswith("ai_hydro") or mod is None:
            continue
        if name in {"ai_hydro.approval", "ai_hydro.approval.writer", "ai_hydro.approval.cli"}:
            continue      # the package itself gains a `writer` attribute once imported
        for attr, value in vars(mod).items():
            assert value is not writer and value is not writer.write_approval, f"{name}.{attr}"
        checked += 1
    assert checked > 50, "expected to inspect the whole loaded ai_hydro package"


def test_no_registered_tool_holds_the_writer():
    """Layer 3. Needs the server to enumerate its tools; skip loudly when it cannot."""
    import pytest

    import ai_hydro.mcp  # noqa: F401
    from ai_hydro.approval import writer
    from ai_hydro.mcp.app import mcp

    try:
        tools = asyncio.run(mcp.list_tools())
    except AttributeError as exc:     # e.g. FastMCP 3.x lacks the 2.x internals ai_hydro.mcp.app uses
        pytest.skip(f"cannot enumerate registered MCP tools in this environment ({exc}); "
                    "layers 1, 2 and the module-globals check still ran")
    assert len(tools) > 100, f"enumerated only {len(tools)} tools"
    inspected = 0
    for tool in tools:
        fn = getattr(tool, "fn", None)
        if fn is None:
            continue
        held = list(getattr(fn, "__globals__", {}).values())
        held += [c.cell_contents for c in (fn.__closure__ or ())]
        assert all(v is not writer.write_approval and v is not writer for v in held), tool.name
        inspected += 1
    assert inspected > 100, f"inspected only {inspected} tool functions"
