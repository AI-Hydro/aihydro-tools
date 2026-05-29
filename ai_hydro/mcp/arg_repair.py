"""
Argument-repair + self-correcting-error middleware (WS-4).

The driving chat model is weak and frequently calls tools with *almost*-right
arguments: a guessed parameter name (``index`` instead of ``index_name``), a
scalar where a list is expected, a number passed as a string. Left alone these
produce a raw validation error and the model retries the same broken call,
burning turns.

This middleware sits in front of every tool call and does two things:

1. **Repair (silent success path).** Before the tool runs, it renames common
   alias mistakes to the real parameter name (via an explicit alias table plus
   fuzzy matching against the tool's declared parameters) and coerces obvious
   type mismatches. If repair makes the call valid, the tool just succeeds and
   the model never sees the mistake.

2. **Teach (on failure).** If the call still cannot be satisfied, instead of a
   bare stack trace the model gets a structured, self-help response containing:
   what was wrong in plain language, the correct input schema, a copy-pasteable
   corrected example call, and the closest valid parameter names to whatever it
   guessed. The fix is now *in the response*, so the next attempt can succeed —
   the error becomes a teaching turn, not a dead end.

A small retry-loop breaker escalates the guidance when the exact same failing
call is seen repeatedly.

The middleware is uniform across all tools (current and future), so every tool
inherits the behaviour for free.
"""
from __future__ import annotations

import difflib
import logging
from typing import Any

from fastmcp.server.middleware import Middleware

log = logging.getLogger("ai_hydro.mcp.arg_repair")

# Common wrong → right parameter names. A rename is applied only when the
# canonical target is a real parameter of the tool AND the wrong key was
# actually supplied AND the target was not already supplied.
_GLOBAL_ALIASES: dict[str, str] = {
    "index": "index_name",
    "index_id": "index_name",
    "spectral_index": "index_name",
    "geometry_geojson": "geometry",
    "geojson": "geometry",
    "geom": "geometry",
    "gauge": "gauge_id",
    "gage_id": "gauge_id",
    "site": "gauge_id",
    "site_id": "gauge_id",
    "session": "session_id",
    "sid": "session_id",
    "lat": "latitude",
    "lon": "longitude",
    "lng": "longitude",
    "start_date": "start",
    "end_date": "end",
    "begin": "start",
    "freq": "frequency",
    "res": "resolution",
}

_FUZZY_CUTOFF = 0.82


def _coerce_type(value: Any, expected: Any) -> Any:
    """Best-effort coercion of a scalar to the schema-declared JSON type."""
    if expected is None:
        return value
    types = expected if isinstance(expected, list) else [expected]
    try:
        if "boolean" in types and isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "yes", "1"):
                return True
            if low in ("false", "no", "0"):
                return False
        if "integer" in types and isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        if "number" in types and isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return value
        if "array" in types and not isinstance(value, (list, tuple)):
            return [value]
    except Exception:
        return value
    return value


def repair_arguments(
    args: dict[str, Any],
    props: set[str],
    prop_types: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Return (repaired_args, notes). Pure function — unit-testable in isolation."""
    if not isinstance(args, dict) or not props:
        return args, []

    repaired = dict(args)
    notes: list[str] = []

    # 1. Rename unknown keys to valid parameter names.
    for key in list(repaired.keys()):
        if key in props:
            continue
        target = None
        alias = _GLOBAL_ALIASES.get(key)
        if alias and alias in props and alias not in repaired:
            target = alias
        else:
            match = difflib.get_close_matches(key, list(props), n=1, cutoff=_FUZZY_CUTOFF)
            if match and match[0] not in repaired:
                target = match[0]
        if target:
            repaired[target] = repaired.pop(key)
            notes.append(f"renamed '{key}' → '{target}'")

    # 2. Coerce obvious type mismatches on known parameters.
    for key, val in list(repaired.items()):
        if key in prop_types:
            new = _coerce_type(val, prop_types[key])
            if new is not val and new != val:
                repaired[key] = new
                notes.append(f"coerced '{key}' to {prop_types[key]}")

    return repaired, notes


class ArgRepairMiddleware(Middleware):
    """FastMCP middleware: repair tool arguments, teach on failure."""

    def __init__(self, mcp) -> None:
        self._mcp = mcp
        self._cache: dict[str, dict] | None = None
        # Retry-loop breaker: (tool, args-signature) → consecutive failure count
        self._failures: dict[tuple[str, str], int] = {}

    async def _schemas(self) -> dict[str, dict]:
        if self._cache is None:
            cache: dict[str, dict] = {}
            try:
                for t in await self._mcp.list_tools():
                    mt = t.to_mcp_tool()
                    schema = mt.inputSchema or {}
                    props = schema.get("properties", {}) or {}
                    cache[mt.name] = {
                        "schema": schema,
                        "props": set(props.keys()),
                        "required": list(schema.get("required", []) or []),
                        "types": {k: (v or {}).get("type") for k, v in props.items()},
                    }
            except Exception as e:  # pragma: no cover — never block calls on cache build
                log.warning("arg_repair: schema cache build failed: %s", e)
                cache = {}
            self._cache = cache
        return self._cache

    @staticmethod
    def _example(name: str, info: dict) -> dict:
        props = info["schema"].get("properties", {}) or {}
        required = set(info["required"])
        # Lazy import to avoid a hard module dependency at import time
        from ai_hydro.mcp.tools_discovery import _example_value
        args = {}
        for pname, spec in props.items():
            spec = spec or {}
            if pname in required or spec.get("default") is not None:
                args[pname] = _example_value(pname, spec)
        return {"tool": name, "arguments": args}

    def _build_help(self, name: str, info: dict, supplied: dict, error: str, repeated: bool) -> dict:
        props = info["props"]
        bad = [k for k in (supplied or {}) if k not in props]
        suggestions = {}
        for k in bad:
            close = difflib.get_close_matches(k, list(props), n=3, cutoff=0.5)
            if close:
                suggestions[k] = close
        msg = (
            f"The call to '{name}' could not be completed: {error}. "
            "Fix the arguments using the schema and example below, then retry."
        )
        if repeated:
            msg = (
                f"You have already tried this exact call to '{name}' and it failed. "
                "Do NOT repeat it unchanged — use the corrected example below."
            )
        out = {
            "error": True,
            "tool": name,
            "message": msg,
            "unexpected_params": bad or None,
            "did_you_mean": suggestions or None,
            "required": info["required"],
            "input_schema": info["schema"],
            "example_call": self._example(name, info),
        }
        return {k: v for k, v in out.items() if v is not None}

    async def on_call_tool(self, context, call_next):
        msg = context.message
        name = getattr(msg, "name", None)
        args = getattr(msg, "arguments", None)
        schemas = await self._schemas()
        info = schemas.get(name)

        # 1. Repair before execution
        if info and isinstance(args, dict):
            repaired, notes = repair_arguments(args, info["props"], info["types"])
            if notes:
                log.info("arg_repair: %s: %s", name, "; ".join(notes))
                msg.arguments = repaired
                args = repaired

        sig = repr(sorted((args or {}).items()))
        key = (name, sig)

        # 2. Execute; teach on failure
        try:
            result = await call_next(context)
            self._failures.pop(key, None)  # success clears the retry counter
            return result
        except Exception as e:
            if not info:
                raise  # unknown tool / third-party server — don't interfere
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            from fastmcp.tools.tool import ToolResult
            help_payload = self._build_help(name, info, args, str(e), repeated=count >= 2)
            log.info("arg_repair: returning self-help for failed call to %s", name)
            return ToolResult(structured_content=help_payload)


def install_arg_repair(mcp) -> None:
    """Attach the ArgRepairMiddleware to a FastMCP instance (idempotent)."""
    if getattr(mcp, "_aihydro_arg_repair_installed", False):
        return
    mcp.add_middleware(ArgRepairMiddleware(mcp))
    mcp._aihydro_arg_repair_installed = True
