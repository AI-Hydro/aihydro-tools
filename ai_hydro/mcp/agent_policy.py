"""Server-side floor for agent-originated calls (agent orchestration, Stage 1).

A host that runs subagents sends the child's identity on every call it makes,
as ``_meta["aihydro/context"]["agent"]``. This module is the server's half:

* ``parse_agent`` turns that value into a bounded, JSON-safe identity (or ``None``
  when the call has no agent, which is the whole of today's behaviour);
* ``AgentPolicyMiddleware`` refuses, for a call that names an agent,
  - any tool whose annotation row is not ``delegable = "read" | "scoped"`` (a missing
    row or a missing key counts as ``lead``: fail closed), and any ``audience = "host"`` tool;
  - an explicit ``session_id`` that is not the study the call's context names, so an agent
    cannot reach another study (session resolution also never rebinds a chat for an agent,
    see ``helpers._bind``);
  - every call at all while an evaluation run is active (``in_evaluation_run()``).

What ``agent`` is, and is not. It is asserted by the client. The server cannot tell a
real subagent from a caller that omits the field, so ``agent`` can only NARROW what a call
may do. It never grants anything: a call without it is treated exactly as before, and no
rule here lets any call do more than it could. It is attribution (sealed into the run
record as ``extra.agent`` with ``asserted_by: "client"``), not proof of origin (ADR-002).
The hard stops that need origin (human approval, launcher trust) stay with the human
and the host; this layer is a floor under them, not a replacement.

With no ``agent`` the middleware is a pure pass-through and run records are byte-identical
to those written before it existed.
"""
from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.middleware import Middleware

from ai_hydro.mcp.errors import StructuredToolError

log = logging.getLogger("ai_hydro.mcp.agent_policy")

__all__ = [
    "AGENT_KEYS", "DELEGABLE_VALUES", "AGENT_NOT_DELEGABLE", "AGENT_CONTEXT_MISMATCH",
    "AGENT_IN_EVALUATION", "parse_agent", "delegable_of", "AgentPolicyMiddleware",
]

AGENT_NOT_DELEGABLE = "AGENT_NOT_DELEGABLE"
AGENT_CONTEXT_MISMATCH = "AGENT_CONTEXT_MISMATCH"
AGENT_IN_EVALUATION = "AGENT_REFUSED_IN_EVALUATION"

#: Closed vocabulary of the ``delegable`` column of tool_annotations.toml.
#: read   - a child may call it freely (the tool is readOnlyHint).
#: scoped - a child may call it only inside a grant the host holds; the server lets it through
#:          (the host enforces the grant).
#: lead   - never by an agent: the lead author's alone. Absent means lead.
DELEGABLE_VALUES = frozenset({"read", "scoped", "lead"})

#: Identity fields carried into the sealed record, in this order.
AGENT_KEYS = ("agent_id", "parent_agent_id", "role", "definition_sha256", "team_id", "lease")
_MAX_STR = 200
_MAX_LEASE_KEYS = 8
_UNIDENTIFIED = "unidentified"


def _short(value: Any) -> str | None:
    return value.strip()[:_MAX_STR] if isinstance(value, str) and value.strip() else None


def _lease(value: Any) -> Any:
    """A lease is a short string, a number, or a flat dict of scalars; anything else is dropped."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return _short(value)
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:_MAX_LEASE_KEYS]:
            if isinstance(k, str) and isinstance(v, (str, int, float, bool)):
                out[k[:_MAX_STR]] = v[:_MAX_STR] if isinstance(v, str) else v
        return out or None
    return None


def parse_agent(value: Any) -> dict | None:
    """The call's agent identity, or ``None`` when ``agent`` is absent.

    Only an absent key (or an explicit ``null``) means no agent. A present but malformed value
    (not an object, no usable ``agent_id``) still counts as an agent, with ``agent_id``
    ``"unidentified"``: a caller must not be able to shed the floor by garbling the field.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return {"agent_id": _UNIDENTIFIED}
    out: dict[str, Any] = {}
    for key in AGENT_KEYS:
        v = _lease(value.get(key)) if key == "lease" else _short(value.get(key))
        if v is not None:
            out[key] = v
    out.setdefault("agent_id", _UNIDENTIFIED)
    return out


def delegable_of(tool: str, rows: dict[str, dict[str, Any]] | None = None) -> str:
    """The tool's ``delegable`` class; ``lead`` for a tool with no row or no (valid) key."""
    if rows is None:
        from ai_hydro.mcp.tool_surface import load_annotation_rows_cached

        rows = load_annotation_rows_cached()
    value = (rows.get(tool) or {}).get("delegable")
    return value if value in DELEGABLE_VALUES else "lead"


def _refusal(code: str, message: str, **extra: Any) -> StructuredToolError:
    return StructuredToolError({"error": True, "code": code, "message": message, **extra})


class AgentPolicyMiddleware(Middleware):
    """Refuse what an agent may not do. A pure pass-through when the call names no agent.

    Registered with ``mcp.add_middleware`` after the context middleware (so ``ACTIVE_AGENT`` is
    set) and the evaluation layer, before the run-record middleware: a refused call does nothing,
    writes nothing and is not sealed. The host records its own refusals in the agent's record.
    """

    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        from ai_hydro.mcp.app import ACTIVE_AGENT, ACTIVE_STUDY_ID

        agent = ACTIVE_AGENT.get()
        if agent is None:
            return await call_next(context)

        message = context.message
        tool = getattr(message, "name", "") or ""
        who = agent.get("agent_id", _UNIDENTIFIED)

        from ai_hydro.mcp.eval_condition import in_evaluation_run

        if in_evaluation_run():
            raise _refusal(
                AGENT_IN_EVALUATION,
                "Evaluation runs do not accept calls from agents: the arms are measured on one "
                "actor's tool use. Make the call without the `agent` context field.",
                tool=tool)

        from ai_hydro.mcp.tool_surface import load_annotation_rows_cached

        rows = load_annotation_rows_cached()
        row = rows.get(tool) or {}
        if row.get("audience") == "host" or delegable_of(tool, rows) == "lead":
            raise _refusal(
                AGENT_NOT_DELEGABLE,
                f"{tool} is not available to a subagent (agent {who!r}). Claims, the research plan, "
                "interpretation, claim binding, study lifecycle, profile and skills, and host-only "
                "operations are the lead's alone, whatever the user approved for the agent. "
                "Report what you found to the lead, which makes the call itself.",
                tool=tool, delegable="lead")

        args = getattr(message, "arguments", None) or {}
        requested = args.get("session_id") if isinstance(args, dict) else None
        if isinstance(requested, str) and requested.strip():
            bound = ACTIVE_STUDY_ID.get()
            if bound is None or requested.strip() != bound:
                raise _refusal(
                    AGENT_CONTEXT_MISMATCH,
                    f"An agent works in the study of its call context; it may not name another "
                    f"(session_id={requested.strip()[:80]!r}). Omit session_id.",
                    tool=tool)
        return await call_next(context)
