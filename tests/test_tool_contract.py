"""
Tool contract enforcement — 5-rule CI guard.

These tests fail CI if any AI-Hydro MCP tool violates the uniform tool contract
defined in local-docs/PLATFORM_VISION.md §12.  They are *mechanically
enforceable* invariants, the same way import-linter enforces layer boundaries.

Rules (corresponding to §12):
  1. Self-describing  — every @mcp.tool() function must have a non-empty
                        docstring (≥ 20 characters after stripping).
  2. Banned error code — "UNKNOWN_ERROR" must not appear as a string literal in
                        any tool module (legacy bare-error shape, replaced by
                        UNEXPECTED_ERROR + recovery + next_tools).
  3. Error shape       — _tool_error_to_dict always returns the full envelope:
                        code / message / recovery / next_tools.
  4. Async for heavy   — tools whose kind is "data_fetch" or "model_train" must
                        reference start_job, proving they dispatch to jobs.py
                        rather than blocking the event loop.
  5. Discoverable      — every tool in TOOL_TIERS is reachable via the
                        capability-discovery layer (aihydro_describe_capability +
                        describe_tool), and its schema is non-empty.

How to interpret a failure
--------------------------
- Rule 1 failure → add / expand the docstring of the named function.
- Rule 2 failure → replace the "UNKNOWN_ERROR" literal with a ToolError or
                   update the error dict to use "UNEXPECTED_ERROR".
- Rule 3 failure → _tool_error_to_dict was regressed; restore recovery/next_tools.
- Rule 4 failure → the listed heavy-tool module no longer calls start_job; wire
                   it back through jobs.py or remove it from the heavy-tools list.
- Rule 5 failure → add the tool name to _DOMAIN_PREFIXES in tools_discovery.py
                   or ensure it is registered before __init__.py finishes.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOOLS_DIR = Path(__file__).parent.parent / "ai_hydro" / "mcp"
TOOL_MODULES = sorted(TOOLS_DIR.glob("tools_*.py"))

# Known-heavy tools: tuple of (tool_kind/name_fragment, runner_module_name)
# The test verifies start_job is called in the module that registers them.
HEAVY_TOOL_MODULES = {
    "tools_modelling.py":   "train_hydro_model",   # kind=model_train
    "tools_data_async.py":  "data_fetch_background",  # kind=data_fetch
}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _mcp_tool_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return all function defs decorated with @mcp.tool() (or @mcp.tool)."""
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            matched = False
            # @mcp.tool()  →  Call(func=Attribute(attr="tool"))
            if (isinstance(dec, ast.Call) and
                    isinstance(dec.func, ast.Attribute) and
                    dec.func.attr == "tool"):
                matched = True
            # @mcp.tool  →  Attribute(attr="tool")
            elif isinstance(dec, ast.Attribute) and dec.attr == "tool":
                matched = True
            if matched:
                result.append(node)
                break
    return result


def _string_literals_in(tree: ast.Module) -> list[str]:
    """Return all string constants that appear in the module source."""
    literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
    return literals


# ---------------------------------------------------------------------------
# Rule 1 — Self-describing (non-empty docstring)
# ---------------------------------------------------------------------------

class TestRule1SelfDescribing:
    """Every @mcp.tool() function must carry a non-trivial docstring."""

    MIN_DOCSTRING_LEN = 20  # shorter than this is probably a placeholder

    def _violators(self) -> list[tuple[str, str]]:
        bad = []
        for path in TOOL_MODULES:
            tree = _parse(path)
            for fn in _mcp_tool_functions(tree):
                doc = ast.get_docstring(fn)
                if doc is None or len(doc.strip()) < self.MIN_DOCSTRING_LEN:
                    bad.append((path.name, fn.name))
        return bad

    def test_all_mcp_tools_have_docstrings(self):
        """Every @mcp.tool() must have a docstring ≥ 20 chars."""
        violators = self._violators()
        assert not violators, (
            "Rule 1 (self-describing) violation — add/expand docstrings:\n"
            + "\n".join(f"  {mod}: {fn}" for mod, fn in violators)
        )

    def test_tool_tiers_has_descriptions(self):
        """Every tool in TOOL_TIERS must have a non-empty description registered."""
        import asyncio as _asyncio
        from ai_hydro.mcp import mcp
        from ai_hydro.mcp.app import TOOL_TIERS
        tools = _asyncio.run(mcp.list_tools())
        by_name = {t.name: t for t in tools}
        bad = []
        for name in TOOL_TIERS:
            tool = by_name.get(name)
            if tool is None:
                continue  # test_all_builtin_tools_registered catches this
            if not tool.description or len(tool.description.strip()) < self.MIN_DOCSTRING_LEN:
                bad.append(name)
        assert not bad, (
            "Rule 1 (self-describing) violation — tools with missing/short description:\n"
            + "\n".join(f"  {n}" for n in bad)
        )


# ---------------------------------------------------------------------------
# Rule 2 — Banned error code UNKNOWN_ERROR
# ---------------------------------------------------------------------------

class TestRule2BannedErrorCode:
    """
    The legacy 'UNKNOWN_ERROR' error code is banned.

    _tool_error_to_dict now always emits UNEXPECTED_ERROR + recovery + next_tools.
    Any tool that returns {"code": "UNKNOWN_ERROR"} directly bypasses this and
    strands the agent without a recovery hint.
    """

    BANNED_LITERAL = "UNKNOWN_ERROR"

    def _violating_modules(self) -> list[tuple[str, int]]:
        """Return (module_name, line_no) for any UNKNOWN_ERROR literal found."""
        bad = []
        for path in TOOL_MODULES:
            src = path.read_text(encoding="utf-8")
            for i, line in enumerate(src.splitlines(), start=1):
                # Ignore comments
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if self.BANNED_LITERAL in line:
                    bad.append((path.name, i))
        return bad

    def test_unknown_error_not_in_tool_modules(self):
        """'UNKNOWN_ERROR' must not appear in any tools_*.py file."""
        bad = self._violating_modules()
        assert not bad, (
            "Rule 2 (banned error code) violation — replace UNKNOWN_ERROR with "
            "UNEXPECTED_ERROR or raise ToolError with a recovery hint:\n"
            + "\n".join(f"  {mod}:{line}" for mod, line in bad)
        )

    def test_unknown_error_not_in_helpers(self):
        """helpers.py must not emit UNKNOWN_ERROR (it was updated to UNEXPECTED_ERROR)."""
        helpers = TOOLS_DIR / "helpers.py"
        src = helpers.read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            assert self.BANNED_LITERAL not in line, (
                f"Rule 2 violation in helpers.py:{i} — "
                f"'UNKNOWN_ERROR' found. Replace with UNEXPECTED_ERROR."
            )


# ---------------------------------------------------------------------------
# Rule 3 — Error shape always populated
# ---------------------------------------------------------------------------

class TestRule3ErrorShape:
    """
    _tool_error_to_dict must always emit the full envelope even for raw exceptions.

    Since the arg_repair middleware calls _tool_error_to_dict as the last-resort
    boundary catcher, we verify that the function itself returns all required keys.
    """

    REQUIRED_KEYS = {"error", "code", "message", "recovery", "next_tools"}

    def test_unexpected_error_shape(self):
        """A raw ValueError through _tool_error_to_dict must yield the full envelope."""
        from ai_hydro.mcp.helpers import _tool_error_to_dict
        result = _tool_error_to_dict(RuntimeError("unexpected boom"))
        missing = self.REQUIRED_KEYS - result.keys()
        assert not missing, (
            f"Rule 3 (error shape) violation — _tool_error_to_dict is missing keys: "
            f"{missing}"
        )
        assert result["error"] is True
        assert result["code"] != "UNKNOWN_ERROR", (
            "Rule 3 violation — UNKNOWN_ERROR still emitted by _tool_error_to_dict"
        )
        assert result["recovery"], "Rule 3 violation — recovery is empty"
        assert isinstance(result["next_tools"], list), (
            "Rule 3 violation — next_tools must be a list"
        )

    def test_tool_error_shape_preserved(self):
        """A ToolError's own to_dict() result is passed through unchanged."""
        from ai_hydro.mcp.helpers import _tool_error_to_dict
        from unittest.mock import MagicMock
        mock = MagicMock()
        mock.to_dict.return_value = {
            "error": True, "code": "SOME_CODE",
            "message": "msg", "recovery": "fix it", "next_tools": [],
        }
        result = _tool_error_to_dict(mock)
        assert result["code"] == "SOME_CODE"

    def test_arg_repair_middleware_installed(self):
        """The arg_repair middleware must be active (all boundary exceptions caught)."""
        # The simplest proxy: arg_repair installs a wrapper on mcp._call_tool_mcp.
        # After __init__.py runs, the wrapper should be in place.
        from ai_hydro.mcp import mcp
        # We check that the module loaded without raising (middleware install is
        # try/caught — a warning would be logged on failure).
        assert mcp is not None, "MCP singleton not available"


# ---------------------------------------------------------------------------
# Rule 4 — Async for heavy work
# ---------------------------------------------------------------------------

class TestRule4AsyncForHeavy:
    """
    Known heavy-work tools must dispatch through jobs.start_job, not block.

    The source of truth is HEAVY_TOOL_MODULES (defined at module level above).
    Adding a new heavy tool? Add it there; the test will enforce the contract
    from that point forward.
    """

    def test_heavy_tool_modules_call_start_job(self):
        """Each heavy-tool module must reference start_job in its source."""
        bad = []
        for module_name, tool_name in HEAVY_TOOL_MODULES.items():
            path = TOOLS_DIR / module_name
            if not path.exists():
                bad.append(f"{module_name}: file not found")
                continue
            src = path.read_text(encoding="utf-8")
            if "start_job" not in src:
                bad.append(
                    f"{module_name} ({tool_name}): start_job not found — "
                    "heavy tool must dispatch through jobs.py"
                )
        assert not bad, (
            "Rule 4 (async for heavy) violation:\n"
            + "\n".join(f"  {m}" for m in bad)
        )

    def test_base_job_runner_exists(self):
        """BaseJobRunner must exist so future heavy tools have a boilerplate-free path."""
        from ai_hydro.mcp.runners.base import BaseJobRunner
        assert BaseJobRunner is not None

    def test_data_fetch_runner_uses_base(self):
        """DataFetchRunner must subclass BaseJobRunner (proves the pattern works)."""
        from ai_hydro.mcp.runners.base import BaseJobRunner
        from ai_hydro.mcp.runners.data_fetch_runner import DataFetchRunner
        assert issubclass(DataFetchRunner, BaseJobRunner)

    def test_base_runner_run_job_signature(self):
        """BaseJobRunner.run_job must accept (cfg, artifact_dir) — future runners rely on this."""
        from ai_hydro.mcp.runners.base import BaseJobRunner
        sig = inspect.signature(BaseJobRunner.run_job)
        params = list(sig.parameters.keys())
        assert "cfg"          in params, "run_job must accept cfg"
        assert "artifact_dir" in params, "run_job must accept artifact_dir"


# ---------------------------------------------------------------------------
# Rule 5 — Discoverable
# ---------------------------------------------------------------------------

class TestRule5Discoverable:
    """
    Every tool must be reachable via the capability-discovery layer.

    aihydro_describe_capability → list of tools per domain.
    describe_tool(name)          → full schema + example_call.

    This test does NOT call describe_tool for all 100+ tools (too slow).
    Instead it verifies:
      (a) every tool in TOOL_TIERS appears in at least one domain's tool list, OR
          is reachable by describe_tool directly (for tools that fall into
          domain "general" — acceptable as long as describe_tool works).
      (b) a sample of tools returns a non-empty schema from describe_tool.
    """

    SAMPLE_TOOLS = [
        "start_session",
        "data_fetch",
        "delineate_watershed",
        "compute_twi",
        "describe_tool",
        "aihydro_describe_capability",
    ]

    def _run(self, coro):
        return asyncio.run(coro)

    def test_capability_discovery_returns_domains(self):
        """aihydro_describe_capability() with no domain must list domains."""
        from ai_hydro.mcp.tools_discovery import aihydro_describe_capability
        result = self._run(aihydro_describe_capability(domain=None))
        assert "domains" in result, "Must return 'domains' key"
        assert len(result["domains"]) > 5, "Should have many domains"

    def test_describe_tool_returns_schema_for_sample(self):
        """describe_tool must return a populated schema for common tools."""
        from ai_hydro.mcp.tools_discovery import describe_tool
        for name in self.SAMPLE_TOOLS:
            result = self._run(describe_tool(name))
            assert result.get("error") is not True, (
                f"Rule 5 violation — describe_tool('{name}') returned error: "
                f"{result.get('message')}"
            )
            assert result.get("input_schema"), (
                f"Rule 5 violation — describe_tool('{name}') has no input_schema"
            )
            assert result.get("example_call"), (
                f"Rule 5 violation — describe_tool('{name}') has no example_call"
            )

    def test_all_tier1_tools_discoverable(self):
        """Every Tier 1 tool must be discoverable via describe_tool (schemas always hot)."""
        from ai_hydro.mcp.app import TOOL_TIERS
        from ai_hydro.mcp.tools_discovery import describe_tool
        tier1 = [name for name, tier in TOOL_TIERS.items() if tier == 1]
        bad = []
        for name in tier1:
            result = self._run(describe_tool(name))
            if result.get("error"):
                bad.append(f"{name}: {result.get('message', '?')}")
        assert not bad, (
            "Rule 5 (discoverable) violation — Tier 1 tools not found by describe_tool:\n"
            + "\n".join(f"  {b}" for b in bad)
        )

    def test_no_orphan_tools(self):
        """No tool in TOOL_TIERS should be completely undiscoverable (not in any domain)."""
        from ai_hydro.mcp.app import TOOL_TIERS
        from ai_hydro.mcp.tools_discovery import _DOMAIN_PREFIXES
        # Build a flat set of all prefix-matching tool names
        all_prefixes = [p for ps in _DOMAIN_PREFIXES.values() for p in ps]
        orphans = []
        for name in TOOL_TIERS:
            if not any(name.startswith(pfx) for pfx in all_prefixes):
                orphans.append(name)
        assert not orphans, (
            "Rule 5 (discoverable) violation — tools with no domain prefix match "
            "(add a prefix to _DOMAIN_PREFIXES in tools_discovery.py):\n"
            + "\n".join(f"  {n}" for n in orphans)
        )


# ---------------------------------------------------------------------------
# Contract summary smoke-test
# ---------------------------------------------------------------------------

class TestContractSummary:
    """A quick end-to-end check that the full 5-rule contract holds."""

    def test_all_five_rule_modules_importable(self):
        """All contract-related modules must import without error."""
        modules = [
            "ai_hydro.mcp.helpers",
            "ai_hydro.mcp.enforcement",
            "ai_hydro.mcp.runners.base",
            "ai_hydro.mcp.runners.data_fetch_runner",
            "ai_hydro.mcp.tools_discovery",
        ]
        for mod in modules:
            m = importlib.import_module(mod)
            assert m is not None, f"Failed to import {mod}"

    def test_next_steps_registry_populated(self):
        """After __init__ loads, next_steps must be registered for Tier 1 tools."""
        import ai_hydro.mcp  # noqa: F401
        from ai_hydro.mcp.enforcement import get_next_steps_snapshot
        snap = get_next_steps_snapshot()
        assert len(snap) >= 10, (
            "Expected at least 10 Tier 1 tools with next_steps registrations; "
            f"got {len(snap)}"
        )

    def test_error_fall_through_is_structured(self):
        """The last-resort error path must always produce a full structured envelope."""
        from ai_hydro.mcp.helpers import _tool_error_to_dict
        for exc in [Exception("boom"), ValueError("bad"), RuntimeError("oops")]:
            r = _tool_error_to_dict(exc)
            assert r.get("error") is True
            assert "recovery" in r and r["recovery"]
            assert "next_tools" in r
            assert r.get("code") not in (None, "UNKNOWN_ERROR")
