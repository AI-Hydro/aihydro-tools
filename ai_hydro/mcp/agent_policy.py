"""Server-side floor for agent-originated calls (agent orchestration, Stage 1).

A host that runs subagents sends the child's identity on every call it makes,
as ``_meta["aihydro/context"]["agent"]``. This module is the server's half:

* ``parse_agent`` turns that value into a bounded, JSON-safe identity (or ``None``
  when the call has no agent, which is the whole of today's behaviour);
* ``AgentPolicyMiddleware`` refuses, for a call that names an agent,
  - any tool whose annotation row is not ``delegable = "read" | "scoped"`` (a missing
    row or a missing key counts as ``lead``: fail closed), and any ``audience = "host"`` tool;
  - an explicit study id (``session_id`` or any alias ArgRepair maps to it) that is not the
    study the call's context names (re-checked inside ``helpers._resolve_session_impl``, so no
    alias spelling gets past it), plus a few identifier arguments that reach outside the call's
    study (``AGENT_ARG_RULES``). Session resolution also never rebinds a chat for an agent,
    see ``helpers._bind``;
  - every call at all while an evaluation run is active (``in_evaluation_run()``).

What ``agent`` is, and is not. It is asserted by the client. The server cannot tell a
real subagent from a caller that omits the field, so ``agent`` can only NARROW what a call
may do. It never grants anything: a call without it is treated exactly as before, and no
rule here lets any call do more than it could. It is attribution (sealed into the run
record as ``extra.agent`` with ``asserted_by: "client"``), not proof of origin (ADR-002).
The hard stops that need origin (human approval, launcher trust) stay with the human
and the host; this layer is a floor under them, not a replacement.

Limits, stated plainly:

* The study comparison is against ``ACTIVE_STUDY_ID``, which is itself read from the same
  client-asserted ``_meta``. It confines an agent to its study only when the HOST stamps
  ``study_id`` on every child call (the extension does). A caller that omits ``agent`` or lies
  about ``study_id`` is not stopped; this layer is a floor under a cooperating host.
* ``delegable = "read"`` means read-only and study-scoped for tools reached through ``tools/call``.
  ``aihydro://`` resource reads do not pass through tool middleware. A host must not hand a child
  the resource surface (known gap); the policy does not cover it.
* ``audience = "host"`` is a hint to hosts. The server enforces it only for calls that name an
  agent; any caller that omits ``agent`` can call a host-audience tool.

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
    "AGENT_ARG_RULES", "check_agent_arguments", "AgentArgumentPolicyMiddleware",
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


#: Arguments that reach outside the call's own study. Keyed by tool because the argument, not the tool
#: class, is the hazard. Checked on the repaired arguments (AgentArgumentPolicyMiddleware) and on the raw
#: ones. "forbid": refused when set to anything but None / "" / [] / False. "context": must equal the
#: call context's value (``chat`` -> ACTIVE_CHAT_ID, ``workspace`` -> ACTIVE_WORKSPACE). "values": refused values.
AGENT_ARG_RULES: dict[str, dict[str, Any]] = {
    "search_experiments": {"forbid": ("project_name", "compare_sessions")},
    "search_literature": {"forbid": ("project_name",)},
    "audit_interpretation": {"forbid": ("outcome", "unmet")},
    "aihydro_chat_status": {"context": {"chat_id": "chat"}},
    "map_get_state": {"context": {"chat_id": "chat", "workspace": "workspace"},
                      "values": {"scope": ("project",)}},
}


def _set(v: Any) -> bool:
    return v not in (None, "", [], False, {})


def check_agent_arguments(tool: str, args: Any) -> None:
    """Raise a structured refusal if ``args`` reach outside the call's study. Agent calls only."""
    from ai_hydro.mcp.app import ACTIVE_CHAT_ID, ACTIVE_STUDY_ID, ACTIVE_WORKSPACE

    if not isinstance(args, dict):
        return
    requested = args.get("session_id")
    if isinstance(requested, str) and requested.strip():
        bound = ACTIVE_STUDY_ID.get()
        if bound is None or requested.strip() != bound:
            raise _refusal(
                AGENT_CONTEXT_MISMATCH,
                "An agent works in the study of its call context; it may not name another "
                f"(session_id={requested.strip()[:80]!r}). Omit session_id.", tool=tool)
    rule = AGENT_ARG_RULES.get(tool)
    if not rule:
        return
    for name in rule.get("forbid", ()):
        if _set(args.get(name)):
            raise _refusal(AGENT_CONTEXT_MISMATCH,
                           f"{tool}: argument {name!r} reaches outside the agent's study and is not "
                           "available to a subagent.", tool=tool, argument=name)
    ctx = {"chat": ACTIVE_CHAT_ID.get(), "workspace": ACTIVE_WORKSPACE.get()}
    for name, key in (rule.get("context") or {}).items():
        v = args.get(name)
        if _set(v) and v != ctx[key]:
            raise _refusal(AGENT_CONTEXT_MISMATCH,
                           f"{tool}: {name!r} must be the call context's own ({key}); an agent may not "
                           "name another.", tool=tool, argument=name)
    for name, bad in (rule.get("values") or {}).items():
        if args.get(name) in bad:
            raise _refusal(AGENT_CONTEXT_MISMATCH,
                           f"{tool}: {name}={args.get(name)!r} reaches beyond the agent's study.",
                           tool=tool, argument=name)


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

        check_agent_arguments(tool, getattr(message, "arguments", None))
        return await call_next(context)


class AgentArgumentPolicyMiddleware(Middleware):
    """The argument rules again, on the REPAIRED arguments.

    ``AgentPolicyMiddleware`` sees the arguments as the client spelt them; ``ArgRepairMiddleware``
    (installed later) renames aliases (``session``, ``sid``, ``sessionId``, ...) to the real parameter.
    This one is installed after it, so it runs just before the tool and sees what the tool will get.
    A pass-through for a call that names no agent.
    """

    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        from ai_hydro.mcp.app import ACTIVE_AGENT

        if ACTIVE_AGENT.get() is not None:
            message = context.message
            check_agent_arguments(getattr(message, "name", "") or "", getattr(message, "arguments", None))
        return await call_next(context)
