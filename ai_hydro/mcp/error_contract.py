"""One error contract for every tool: a failed call is an MCP error carrying its envelope.

A tool that fails by *returning* ``{"error": True, ...}`` produced a successful MCP result
(``isError`` false), so clients and the run-record layer could not tell it from success
without reading the payload. ``ErrorContractMiddleware`` is the general rule that fixes
that for every tool at once: a result dict whose ``error`` is ``True`` is re-raised as
``StructuredToolError(envelope)``. The error text is the same JSON dict the tool returned,
so clients that parse text (the extension, the evaluation runner) see the same envelope,
and ``RunRecordMiddleware`` seals ``envelope`` so the output digest is unchanged.

It is registered after ``ArgRepairMiddleware`` (innermost, next to the tool). ArgRepair
passes a ``StructuredToolError`` through untouched, so a tool's own error is never
replaced by schema help. There are no per-tool exceptions.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp.server.middleware import Middleware

from ai_hydro.mcp.errors import StructuredToolError


def error_envelope(tool_result: Any) -> dict | None:
    """The result dict when it is a tool failure (``error is True``), else ``None``.

    Reads the same source as the run-record layer: ``structured_content`` first (unless it
    is the ``{"result": ...}`` wrapper of a non-object result), then the first JSON text block.
    """
    structured = getattr(tool_result, "structured_content", None)
    if isinstance(structured, dict) and set(structured) != {"result"}:
        return structured if structured.get("error") is True else None
    if isinstance(structured, dict):
        return None
    for block in getattr(tool_result, "content", None) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) and parsed.get("error") is True else None
    return None


class ErrorContractMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        result = await call_next(context)
        envelope = error_envelope(result)
        if envelope is not None:
            raise StructuredToolError(envelope)
        return result


def install_error_contract(mcp) -> None:
    """Attach the ErrorContractMiddleware to a FastMCP instance (idempotent)."""
    if getattr(mcp, "_aihydro_error_contract_installed", False):
        return
    mcp.add_middleware(ErrorContractMiddleware())
    mcp._aihydro_error_contract_installed = True
