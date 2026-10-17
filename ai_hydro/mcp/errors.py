"""Structured MCP errors shared by tools and middleware.

A tool whose output schema is an array (``list_claims``) cannot return the usual error
dict, and the evaluation layer refuses calls from outside the tool; both raise an MCP
*error* whose text is the JSON envelope. ``StructuredToolError`` carries that envelope as
data so consumers (``ArgRepairMiddleware``, the eval layer) test ``isinstance`` instead of
re-parsing text. ``str()`` stays the JSON envelope, so clients and the runner that read
``code`` from the error text are unaffected.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp.exceptions import PromptError, ResourceError, ToolError


class StructuredToolError(ToolError):
    """A ``ToolError`` carrying a structured error envelope (``.envelope``)."""

    def __init__(self, envelope: dict[str, Any]):
        self.envelope = dict(envelope)
        super().__init__(json.dumps(self.envelope, default=str))


class StructuredResourceError(ResourceError):
    """``StructuredToolError`` for resource reads (FastMCP expects ``ResourceError``)."""

    def __init__(self, envelope: dict[str, Any]):
        self.envelope = dict(envelope)
        super().__init__(json.dumps(self.envelope, default=str))


class StructuredPromptError(PromptError):
    """``StructuredToolError`` for prompt gets (FastMCP expects ``PromptError``)."""

    def __init__(self, envelope: dict[str, Any]):
        self.envelope = dict(envelope)
        super().__init__(json.dumps(self.envelope, default=str))


def envelope_from_exception(exc: BaseException) -> dict[str, Any] | None:
    """The error envelope an exception carries, else ``None``.

    A ``StructuredToolError`` carries it as ``.envelope``. A plain ``ToolError`` (a community
    tool, or aihydro-data, which cannot import this module) counts when its text is a JSON dict
    with ``error`` true and a ``code``. One rule for every consumer: arg-repair, the run
    record and the evaluation layer.
    """
    envelope = getattr(exc, "envelope", None)
    if isinstance(exc, StructuredToolError) and isinstance(envelope, dict):
        return envelope
    if not isinstance(exc, ToolError):
        return None
    try:
        body = json.loads(str(exc))
    except ValueError:
        return None
    if isinstance(body, dict) and body.get("error") is True and "code" in body:
        return body
    return None
