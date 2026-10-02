"""Evaluation condition layer for the P1 study (plan ``b1-p1-eval-harness``, W2, ADR-007).

All evaluation-arm logic lives in THIS module (a test enforces that). The layer is
inert unless an evaluation run was set up explicitly:

* ``$AIHYDRO_HOME/eval_home.json`` exists, and
* its ``nonce`` equals the environment variable ``AIHYDRO_EVAL_NONCE``.

The arm comes from the marker (``condition``: ``C1``/``C2``/``C3``), never from a free
environment variable, so an agent that can set its own environment cannot change the
condition it is measured under. Production, a missing marker and a wrong nonce are
all a no-op (nothing hidden, nothing stripped, nothing refused).

Marker (written by the runner, outside the agent sandbox)::

    {"schema": "aihydro.eval_home/1", "condition": "C1", "nonce": "<random>"}

What each arm gets (the kernel is identical; only the surface differs)::

    all arms  hide write_research_interpretation  (submit_answer is the runner's only output channel)
    C1, C2    hide the registry tools (promote_claim_to_registry, list_registry_claims,
              check_registry_staleness)
    C1        also hide every ``check_*`` validator, run_skeptic, audit_interpretation,
              register_research_plan; and strip from every agent-visible result
              ``quality_flags``, ``promotion_check``, ``next_steps`` and the skeptic
              fields (``skeptic*`` / ``_skeptic*``). ``_run_id`` is kept.
    C3        nothing more: the production gate surface.

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
from dataclasses import dataclass
from typing import Any, Optional

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

log = logging.getLogger("ai_hydro.mcp.eval_condition")

MARKER_FILE = "eval_home.json"
NONCE_ENV = "AIHYDRO_EVAL_NONCE"
CONDITIONS = ("C1", "C2", "C3")
CONTEXT_MISMATCH = "EVAL_CONTEXT_MISMATCH"

HIDDEN_ALL_ARMS = frozenset({"write_research_interpretation"})
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

    Raises ``_InvalidMarker`` only when the nonce matches but the marker names no valid
    arm: an evaluation was clearly intended, so the call fails closed instead of running
    under an unknown surface.
    """
    nonce = os.environ.get(NONCE_ENV)
    if not nonce:
        return None
    try:
        from ai_hydro.registry.paths import aihydro_home
        path = aihydro_home() / MARKER_FILE
        if not path.is_file():
            return None
        marker = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(marker, dict) or marker.get("nonce") != nonce:
        return None
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


def _is_stripped(key: str) -> bool:
    return key in STRIPPED_FIELDS or key.startswith(_SKEPTIC_PREFIXES)


def strip_fields(value: Any) -> Any:
    """Copy of a result dict without the C1-hidden advisory fields (``_run_id`` is kept)."""
    if not isinstance(value, dict):
        return value
    return {k: v for k, v in value.items() if not _is_stripped(k)}


def _strip_tool_result(tool_result: ToolResult) -> ToolResult:
    """A new ``ToolResult`` with the advisory fields removed; the original is untouched."""
    structured = getattr(tool_result, "structured_content", None)
    new_structured = strip_fields(copy.deepcopy(structured)) if isinstance(structured, dict) else structured
    blocks = []
    for block in getattr(tool_result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                blocks.append(TextContent(type="text", text=json.dumps(strip_fields(parsed), default=str)))
                continue
        blocks.append(block)
    return ToolResult(content=blocks, structured_content=new_structured,
                      meta=getattr(tool_result, "meta", None))


def _refusal(message: str) -> ToolResult:
    return ToolResult(structured_content={
        "error": True, "code": CONTEXT_MISMATCH, "message": message,
        "recovery": "This is an evaluation harness fault, not something the agent can fix.",
        "next_tools": []})


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

    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        try:
            state = active_state()
        except _InvalidMarker as exc:
            return _refusal(str(exc))
        if state is None:
            return await call_next(context)

        name = getattr(context.message, "name", "") or ""
        if hidden_tools(state.condition, [name]):
            raise ToolError(f"Unknown tool: '{name}'")

        from ai_hydro.mcp.app import _request_context_meta   # lazy: app registers this module
        client = _request_context_meta(context).get("client")
        if client != state.client_label:
            return _refusal(
                f"request context client {client!r} does not match the evaluation marker "
                f"(expected {state.client_label!r}).")

        result = await call_next(context)
        if state.condition == "C1":
            try:
                return _strip_tool_result(result)
            except Exception as exc:   # never hand back a half-stripped result
                log.warning("eval strip failed for %s: %s", name, exc)
                return _refusal(f"could not strip advisory fields: {exc}")
        return result
