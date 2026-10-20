"""What an error response may show the model.

A traceback and absolute local paths are diagnostics for the operator, not context for the
agent: they cost tokens, expose the user's directory layout, and invite the model to reason
about internals it cannot change (session study 01031500, M5). The full traceback stays in
the server log; the model-facing envelope keeps ``code``, ``message``, ``recovery`` and
``next_tools``.

One rule, applied at two points so no tool needs its own handling:

* ``_tool_error_to_dict`` (the shared wrapper) logs the traceback and returns a sanitized dict;
* ``ErrorHygieneMiddleware`` sanitizes any error dict a tool built itself, before the run
  record is sealed (it is registered inside ``RunRecordMiddleware``).
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("ai_hydro.mcp.error_hygiene")

#: Keys that carry stack traces. Dropped from error dicts, never from successful results.
TRACEBACK_KEYS = ("_traceback", "traceback")


def _scrub(value: Any, workspace_dir: Any = None) -> Any:
    if isinstance(value, str):
        from ai_hydro.session.refs import scrub_paths
        return scrub_paths(value, workspace_dir)
    if isinstance(value, dict):
        return {k: _scrub(v, workspace_dir) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, workspace_dir) for v in value]
    return value


def is_error_dict(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("error"))


def sanitize_error(envelope: dict, workspace_dir: Any = None) -> dict:
    """A copy of an error envelope without tracebacks and with local paths scrubbed.

    Idempotent. Non-error dicts are returned unchanged.
    """
    if not is_error_dict(envelope):
        return envelope
    out = {k: v for k, v in envelope.items() if k not in TRACEBACK_KEYS}
    try:
        return _scrub(out, workspace_dir)
    except Exception as exc:          # hygiene must never turn an error into a crash
        log.debug("error scrub skipped: %s", exc)
        return out


def sanitize_tool_result(tool_result: Any) -> None:
    """Sanitize a FastMCP ``ToolResult`` in place when it is an error dict. Best effort."""
    try:
        structured = getattr(tool_result, "structured_content", None)
        if isinstance(structured, dict) and is_error_dict(structured):
            clean = sanitize_error(structured)
            structured.clear()
            structured.update(clean)
        for block in getattr(tool_result, "content", None) or []:
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if is_error_dict(parsed):
                block.text = json.dumps(sanitize_error(parsed), default=str)
            return
    except Exception as exc:
        log.debug("could not sanitize error result: %s", exc)
