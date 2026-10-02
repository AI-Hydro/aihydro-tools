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

from fastmcp.exceptions import ToolError


class StructuredToolError(ToolError):
    """A ``ToolError`` carrying a structured error envelope (``.envelope``)."""

    def __init__(self, envelope: dict[str, Any]):
        self.envelope = dict(envelope)
        super().__init__(json.dumps(self.envelope, default=str))
