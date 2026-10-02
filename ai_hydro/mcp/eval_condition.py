"""Evaluation condition layer for the P1 study (plan ``b1-p1-eval-harness``, W2, ADR-007).

All evaluation-arm logic lives in THIS module (a test enforces that). The layer is
inert unless an evaluation run was set up explicitly:

* ``$AIHYDRO_HOME/eval_home.json`` exists, and
* its ``nonce`` equals the environment variable ``AIHYDRO_EVAL_NONCE``.

The arm comes from the marker (``condition``: ``C1``/``C2``/``C3``), never from a free
environment variable, so an agent that can set its own environment cannot change the
condition it is measured under. With ``AIHYDRO_EVAL_NONCE`` unset (production) the layer
is a complete no-op with no filesystem access. With it SET, the layer fails CLOSED: a
missing, unreadable, unparseable, non-dict, wrong-schema or nonce-mismatched marker
behaves like an invalid marker (empty tool list; every call returns
``EVAL_CONTEXT_MISMATCH``), so deleting or corrupting the marker can never switch an arm
to the production surface.

Marker (written by the runner, outside the agent sandbox)::

    {"schema": "aihydro.eval_home/1", "condition": "C1", "nonce": "<random>"}

What each arm gets (the kernel is identical; only the surface differs)::

    all arms  hide write_research_interpretation  (submit_answer is the runner's only output channel)
              and run_python (arbitrary same-user code execution; see below)
    C1, C2    hide the registry tools (promote_claim_to_registry, list_registry_claims,
              check_registry_staleness)
    all arms  never name a hidden tool in a result: ``scrub_hidden_names`` drops the name from
              lists/keys and replaces it inside text (discovery tools such as
              ``aihydro_describe_capability`` and ``_instruction`` hints would otherwise
              send the agent to tools it cannot call)
    C1        also hide every ``check_*`` validator, run_skeptic, audit_interpretation,
              register_research_plan; and strip at every depth from every agent-visible result
              ``quality_flags``/``_quality_flags``, ``promotion_check``, ``next_steps`` and the skeptic
              fields (``skeptic*`` / ``_skeptic*``). ``_run_id`` is kept.
    C3        nothing more: the production gate surface.

Code execution is hidden in every arm because ``run_python`` runs as the same OS user as
the server and could edit the marker, the session, the registry or the trust files, which
would make this whole layer advisory. Re-enabling it (or any other tool that executes
arbitrary user code) in an evaluation arm requires the owner's OS-user / container
sandbox decision (ADR-007); it is not a per-run toggle.

Order matters and is fixed in ``ai_hydro/mcp/app.py``: this middleware is registered
BEFORE ``RunRecordMiddleware`` so it wraps it. The sealed run record is therefore
built from the unstripped result, and stripping happens only on the way out.

Per-call condition sealing: the runner sends ``_meta["aihydro/context"]["client"] =
"p1eval/<arm>/<sha256(nonce)[:16]>"`` on every call; the existing explicit-context
mechanism seals it as ``extra.context_client``. Any call whose client label differs
from the expected one is refused with ``EVAL_CONTEXT_MISMATCH`` (the runner scores that
as a harness integrity failure), so a sealed record cannot carry a condition the marker
did not name.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

log = logging.getLogger("ai_hydro.mcp.eval_condition")

MARKER_FILE = "eval_home.json"
NONCE_ENV = "AIHYDRO_EVAL_NONCE"
MARKER_SCHEMA = "aihydro.eval_home/1"
CONDITIONS = ("C1", "C2", "C3")
CONTEXT_MISMATCH = "EVAL_CONTEXT_MISMATCH"

HIDDEN_ALL_ARMS = frozenset({"write_research_interpretation", "run_python"})
REGISTRY_TOOLS = frozenset({"promote_claim_to_registry", "list_registry_claims", "check_registry_staleness"})
C1_ONLY_HIDDEN = frozenset({"run_skeptic", "audit_interpretation", "register_research_plan"})
STRIPPED_FIELDS = frozenset({"quality_flags", "promotion_check", "next_steps"})
_SKEPTIC_PREFIXES = ("skeptic", "_skeptic")


@dataclass(frozen=True)
class EvalState:
    condition: str
    nonce_digest: str    # sha256(nonce)[:16]

    @property
    def client_label(self) -> str:
        return f"p1eval/{self.condition}/{self.nonce_digest}"


def nonce_digest(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()[:16]


def expected_client_label(condition: str, nonce: str) -> str:
    """The ``client`` label the runner must send on every call of this arm."""
    return f"p1eval/{condition}/{nonce_digest(nonce)}"


class _InvalidMarker(Exception):
    pass


def active_state() -> Optional[EvalState]:
    """The evaluation state, or ``None`` when the layer is inactive (the production path).

    Once ``AIHYDRO_EVAL_NONCE`` is set an evaluation is clearly intended, so any marker
    problem (missing, unreadable, unparseable, non-dict, wrong schema, nonce mismatch,
    unknown arm) raises ``_InvalidMarker`` and the call fails closed.
    """
    nonce = os.environ.get(NONCE_ENV)
    if not nonce:
        return None
    try:
        from ai_hydro.registry.paths import aihydro_home
        path = aihydro_home() / MARKER_FILE
        marker = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise _InvalidMarker(f"{MARKER_FILE} is missing or unreadable ({type(exc).__name__})") from exc
    if not isinstance(marker, dict):
        raise _InvalidMarker(f"{MARKER_FILE} is not a JSON object")
    if marker.get("schema") != MARKER_SCHEMA:
        raise _InvalidMarker(f"{MARKER_FILE} has schema {marker.get('schema')!r}; expected {MARKER_SCHEMA!r}")
    if marker.get("nonce") != nonce:
        raise _InvalidMarker(f"{MARKER_FILE} nonce does not match {NONCE_ENV}")
    condition = marker.get("condition", marker.get("arm"))
    if condition not in CONDITIONS:
        raise _InvalidMarker(f"eval_home.json names condition {condition!r}; expected one of {CONDITIONS}")
    return EvalState(condition, nonce_digest(nonce))


def hidden_tools(condition: str, tool_names: Any) -> set:
    """Names among ``tool_names`` that arm ``condition`` must not expose."""
    names = set(tool_names)
    hidden = set(HIDDEN_ALL_ARMS)
    if condition in ("C1", "C2"):
        hidden |= REGISTRY_TOOLS
    if condition == "C1":
        hidden |= C1_ONLY_HIDDEN
        hidden |= {n for n in names if n.startswith("check_")}
    return hidden & names


def _is_stripped(key: Any) -> bool:
    """True for a stripped advisory key, with or without any leading underscores."""
    if not isinstance(key, str):
        return False
    bare = key.lstrip("_")
    return bare in STRIPPED_FIELDS or bare.startswith("skeptic")


def strip_fields(value: Any) -> Any:
    """Copy of ``value`` without the C1-hidden advisory fields at every depth.

    Recurses through dicts and lists; ``_run_id`` is kept. A key is stripped when its
    name, ignoring leading underscores, is a stripped field (``quality_flags``,
    ``_quality_flags``, ...) or starts with ``skeptic``.
    """
    if isinstance(value, dict):
        return {k: strip_fields(v) for k, v in value.items() if not _is_stripped(k)}
    if isinstance(value, list):
        return [strip_fields(v) for v in value]
    return value


UNAVAILABLE = "[tool unavailable in this configuration]"


def hidden_name_pattern(condition: str, tool_names: Any = ()) -> "re.Pattern[str]":
    """Regex matching the name of any tool arm ``condition`` hides.

    The alternation is the fixed hidden names plus whatever ``hidden_tools`` selects from
    the live registered ``tool_names`` (this is where the C1 ``check_*`` prefix rule
    lives), so an ordinary token such as ``check_only`` is never rewritten.
    """
    names = set(HIDDEN_ALL_ARMS)
    if condition in ("C1", "C2"):
        names |= REGISTRY_TOOLS
    if condition == "C1":
        names |= C1_ONLY_HIDDEN
    names |= hidden_tools(condition, tool_names)
    alternatives = [re.escape(n) for n in sorted(names, key=lambda n: (-len(n), n))]
    return re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(alternatives) + r")(?![A-Za-z0-9_])")


def scrub_hidden_names(value: Any, pattern: "re.Pattern[str]") -> Any:
    """Copy of ``value`` that never names a hidden tool, at any depth.

    A string that IS a hidden name is dropped from lists and from dict keys; a hidden
    name inside longer text is replaced by ``UNAVAILABLE``. This is a generic discovery
    scrub, not a per-tool rule: every hidden tool is treated the same wherever it is named.
    """
    if isinstance(value, str):
        return pattern.sub(UNAVAILABLE, value)
    if isinstance(value, dict):
        return {k: scrub_hidden_names(v, pattern) for k, v in value.items()
                if not (isinstance(k, str) and pattern.fullmatch(k))}
    if isinstance(value, list):
        return [scrub_hidden_names(v, pattern) for v in value
                if not (isinstance(v, str) and pattern.fullmatch(v))]
    return value


def _sanitize(value: Any, state: EvalState, pattern: "re.Pattern[str]") -> Any:
    if state.condition == "C1":
        value = strip_fields(value)
    return scrub_hidden_names(value, pattern)


def _sanitize_tool_result(tool_result: ToolResult, state: EvalState,
                          pattern: "re.Pattern[str]") -> ToolResult:
    """A new ``ToolResult`` with advisory fields removed (C1) and hidden tool names
    scrubbed (every arm); the original, already sealed, result is untouched."""
    structured = getattr(tool_result, "structured_content", None)
    new_structured = _sanitize(copy.deepcopy(structured), state, pattern) if isinstance(structured, dict) else structured
    blocks = []
    for block in getattr(tool_result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                blocks.append(TextContent(type="text", text=json.dumps(_sanitize(parsed, state, pattern), default=str)))
            else:
                blocks.append(TextContent(type="text", text=pattern.sub(UNAVAILABLE, text)))
            continue
        blocks.append(block)
    return ToolResult(content=blocks, structured_content=new_structured,
                      meta=getattr(tool_result, "meta", None))


def _refusal(message: str) -> ToolError:
    """The refusal for a fault the condition layer itself detects.

    It is an MCP *error*, not a shaped success: error results skip output-schema
    validation, so a ``-> list[...]`` tool cannot turn the envelope into a generic
    "Output validation error". The error text is the JSON envelope (``code`` readable
    by the runner).
    """
    from ai_hydro.mcp.errors import StructuredToolError
    return StructuredToolError({
        "error": True, "code": CONTEXT_MISMATCH, "message": message,
        "recovery": "This is an evaluation harness fault, not something the agent can fix.",
        "next_tools": []})


def _scrub_exception(exc: Exception, pattern: "re.Pattern[str]") -> Exception:
    """``exc`` with hidden tool names removed from its text; ``exc`` itself if nothing changed.

    A ``StructuredToolError`` keeps its type and gets a scrubbed ``.envelope`` (``str()`` is
    the scrubbed JSON); any other exception is rebuilt as the same type from the scrubbed
    message when that type accepts one string, else as a plain ``ToolError``.
    """
    from ai_hydro.mcp.errors import StructuredToolError
    if isinstance(exc, StructuredToolError):
        scrubbed = scrub_hidden_names(copy.deepcopy(exc.envelope), pattern)
        return exc if scrubbed == exc.envelope else StructuredToolError(scrubbed)
    text = str(exc)
    new_text = pattern.sub(UNAVAILABLE, text)
    if new_text == text:
        return exc
    try:
        return type(exc)(new_text)
    except Exception:
        return ToolError(new_text)


class EvalConditionMiddleware(Middleware):
    """Hide tools, verify the sealed condition label, and strip advisory fields per arm."""

    async def on_list_tools(self, context, call_next):  # type: ignore[override]
        tools = await call_next(context)
        try:
            state = active_state()
        except _InvalidMarker:
            return []
        if state is None:
            return tools
        names = [t.name for t in tools]
        hidden = hidden_tools(state.condition, names)
        return [t for t in tools if t.name not in hidden]

    @staticmethod
    async def _scrubbed_exception(context, state, exc: Exception) -> Exception:
        try:
            live = await context.fastmcp_context.fastmcp.get_tools()
            pattern = hidden_name_pattern(state.condition, live.keys())
            return _scrub_exception(exc, pattern)
        except Exception as inner:   # never let an unscrubbed message through
            log.warning("eval exception scrub failed: %s", inner)
            return _refusal(f"could not sanitise the error: {inner}")

    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        try:
            state = active_state()
        except _InvalidMarker as exc:
            raise _refusal(str(exc)) from exc
        if state is None:
            return await call_next(context)

        name = getattr(context.message, "name", "") or ""
        if hidden_tools(state.condition, [name]):
            raise ToolError(f"Unknown tool: '{name}'")

        from ai_hydro.mcp.app import _request_context_meta   # lazy: app registers this module
        client = _request_context_meta(context).get("client")
        if client != state.client_label:
            raise _refusal(
                f"request context client {client!r} does not match the evaluation marker "
                f"(expected {state.client_label!r}).")

        try:
            result = await call_next(context)
        except Exception as exc:
            # Exceptions raised inside the tool skip the result sanitiser below; scrub their
            # text too so an error message cannot name a tool this arm cannot call.
            raise await self._scrubbed_exception(context, state, exc) from None
        try:
            live = await context.fastmcp_context.fastmcp.get_tools()
            pattern = hidden_name_pattern(state.condition, live.keys())
            return _sanitize_tool_result(result, state, pattern)
        except Exception as exc:   # never hand back a half-sanitised result
            log.warning("eval sanitise failed for %s: %s", name, exc)
            raise _refusal(f"could not sanitise the result: {exc}") from exc
