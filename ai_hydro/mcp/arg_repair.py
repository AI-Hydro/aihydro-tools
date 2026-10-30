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

Safety contract (tool-integrity T1-A)
-------------------------------------
A repair must never change what a call *means*.

* A rename is applied only when the two names differ by spelling (case, separators,
  one-character typo in a long word) or by an explicit, reviewed alias. The *semantic*
  tokens of the two names -- units (``m``, ``mm``, ``km2``, ``m3s``, ``hr``), bounds and
  axes (``min``/``max``, ``start``/``end``, ``lat``/``lon``), sides (``a``/``b``,
  ``train``/``val``/``test``), single letters and anything with a digit -- must be identical.
  The check compares parsed tokens, never tool names, so it covers every tool, present and future.
* A near-miss that crosses units (``area_m2`` for ``area_km2``) is refused with a structured
  ``ARG_UNIT_MISMATCH`` error that names the expected parameter and unit. Values are never rescaled.
* A near-miss that differs in any other semantic token (``mode``/``model``, ``min_lat``/``max_lat``,
  ``series_a``/``series``, ``metrics``/``metric``) is not renamed; the normal teach path names the
  closest parameters.
* Every repair that is applied is returned in the tool result as ``_arg_repairs`` and written to
  the sealed run record together with the arguments that actually ran.
"""
from __future__ import annotations

import difflib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from fastmcp.server.middleware import Middleware

from ai_hydro.mcp.errors import StructuredToolError

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
    # ``site`` / ``site_id`` were removed (T1-A): a site is not necessarily a stream gauge
    # (USGS sites include wells and springs), so the id type is not the same.
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
#: Candidates considered when looking for a refusal to explain (not for applying a rename).
_NEAR_CUTOFF = 0.6
#: A morphological typo fix is allowed only inside words at least this long.
_TYPO_MIN_LEN = 6

# --------------------------------------------------------------------------- #
# Semantic tokens: what a parameter name means beyond its words
# --------------------------------------------------------------------------- #

#: Unit tokens (canonical spelling). A name that differs in one of these is a different quantity.
_UNIT_TOKENS: frozenset[str] = frozenset({
    # length
    "m", "mm", "cm", "km", "ft", "mi", "yd",
    # area and volume
    "m2", "km2", "cm2", "ha", "ac", "ft2", "mi2", "m3", "km3", "l", "ml", "af",
    # discharge
    "m3s", "cms", "cfs", "lps", "mmd", "mmday",
    # time
    "s", "ms", "sec", "hr", "h", "day", "d", "yr", "wk", "mo",
    # angle, ratio
    "deg", "rad", "pct", "percent", "frac", "fraction", "ppm",
    # temperature, mass
    "degc", "degf", "kelvin", "kg", "g", "mg", "t",
})

#: Other tokens that fix which member of a pair a parameter is. They must match exactly.
_QUALIFIER_TOKENS: frozenset[str] = frozenset({
    "min", "max", "lower", "upper", "low", "high", "start", "end", "begin", "from", "to",
    "since", "until", "before", "after", "first", "last", "lat", "latitude", "lon", "lng",
    "longitude", "x", "y", "z", "north", "south", "east", "west", "left", "right", "top",
    "bottom", "train", "val", "test", "obs", "sim", "source", "target", "src", "dst", "dest",
    "in", "out", "input", "output", "old", "new", "a", "b", "mean", "std", "median", "sum",
    "dem", "iso",
})

#: Spelling variants of the same unit or qualifier, so ``hours`` and ``hr`` compare equal.
_CANON: dict[str, str] = {
    "lat": "latitude", "lon": "longitude", "lng": "longitude", "begin": "start",
    "hours": "hr", "hour": "hr", "hrs": "hr", "h": "hr",
    "seconds": "s", "secs": "s", "sec": "s", "second": "s",
    "days": "day", "d": "day", "years": "yr", "year": "yr", "yrs": "yr",
    "meters": "m", "metres": "m", "meter": "m", "metre": "m",
    "millimeters": "mm", "millimetres": "mm", "kilometers": "km", "kilometres": "km",
    "mmday": "mmd", "percent": "pct", "fraction": "frac", "src": "source", "dst": "target",
    "dest": "target",
}

_UNIT_DISPLAY: dict[str, str] = {
    "m3s": "m3/s", "mmd": "mm/day", "m2": "m2", "km2": "km2", "pct": "percent",
    "frac": "fraction", "hr": "hours", "s": "seconds", "yr": "years", "day": "days",
}

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _tokens(name: str) -> list[str]:
    """Lower-case word tokens of a parameter name (snake_case, camelCase, kebab-case)."""
    return [_respell(t) for t in re.split(r"[_\-\s]+", _CAMEL.sub("_", str(name)).lower()) if t]


def _canon(tok: str) -> str:
    return _CANON.get(tok, tok)


_LONG_QUALIFIERS = tuple(sorted(q for q in _QUALIFIER_TOKENS if len(q) >= _TYPO_MIN_LEN))


def _respell(tok: str) -> str:
    """Map a one-typo misspelling of a long qualifier (``lattitude``) to the qualifier."""
    if len(tok) >= _TYPO_MIN_LEN and tok not in _QUALIFIER_TOKENS:
        for q in _LONG_QUALIFIERS:
            if _edit_distance_le1(tok, q):
                return q
    return tok


def _is_unit(tok: str) -> bool:
    return _canon(tok) in _UNIT_TOKENS or tok in _UNIT_TOKENS


def _is_semantic(tok: str) -> bool:
    """True for a token that carries meaning beyond its spelling."""
    c = _canon(tok)
    return (
        _is_unit(tok)
        or c in _QUALIFIER_TOKENS
        or tok in _QUALIFIER_TOKENS
        or any(ch.isdigit() for ch in tok)
        or len(tok) <= 1
    )


def _split(name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(semantic tokens, plain tokens)`` of a name, semantic ones in canonical spelling."""
    sem: list[str] = []
    plain: list[str] = []
    for tok in _tokens(name):
        (sem if _is_semantic(tok) else plain).append(_canon(tok) if _is_semantic(tok) else tok)
    return tuple(sem), tuple(plain)


def unit_tokens(name: str) -> tuple[str, ...]:
    """The unit tokens in a parameter name, canonical spelling (``area_km2`` -> ``("km2",)``)."""
    return tuple(_canon(t) for t in _tokens(name) if _is_unit(t))


def _unit_label(name: str) -> str | None:
    units = unit_tokens(name)
    return " ".join(_UNIT_DISPLAY.get(u, u) for u in units) if units else None


def _edit_distance_le1(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` differ by at most one insertion, deletion, substitution or swap."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long_[i]:
        i += 1
    return short[i:] == long_[i + 1:]


@dataclass(frozen=True)
class RenameVerdict:
    """Whether renaming ``key`` to ``target`` keeps the call's meaning."""

    ok: bool
    #: ``unit`` | ``semantic`` | ``shape`` | ``spelling`` | ``type`` when refused
    kind: str | None = None
    reason: str | None = None


def judge_rename(key: str, target: str, *, alias: bool = False) -> RenameVerdict:
    """Decide on tokens alone whether ``key`` -> ``target`` can change what a value means.

    The semantic tokens (units, bounds, axes, sides, single letters, digits) of both names
    must be identical. For a fuzzy (non-alias) rename the plain words must also match, up to
    one typo inside a word of at least ``_TYPO_MIN_LEN`` letters, and may not differ by a
    plural ``s`` (a list is not its element).
    """
    sem_k, plain_k = _split(key)
    sem_t, plain_t = _split(target)
    if sem_k != sem_t:
        diff = set(sem_k) ^ set(sem_t)
        if any(_is_unit(t) for t in diff):
            return RenameVerdict(
                False, "unit",
                f"'{key}' ({_unit_label(key) or 'no unit'}) and '{target}' ({_unit_label(target) or 'no unit'}) "
                "are different units")
        return RenameVerdict(False, "semantic", f"'{key}' and '{target}' differ in {sorted(diff)}")
    if alias:
        return RenameVerdict(True)
    if len(plain_k) != len(plain_t):
        return RenameVerdict(False, "shape", f"'{key}' and '{target}' have different words")
    for a, b in zip(plain_k, plain_t):
        if a == b:
            continue
        if a + "s" == b or b + "s" == a:
            return RenameVerdict(False, "semantic", f"'{a}' vs '{b}' is singular vs plural")
        if min(len(a), len(b)) >= _TYPO_MIN_LEN and _edit_distance_le1(a, b):
            continue
        return RenameVerdict(False, "spelling", f"'{a}' and '{b}' are different words")
    return RenameVerdict(True)


_SCALAR_TYPES = {"string", "number", "integer", "boolean"}


def _shape_ok(value: Any, expected: Any) -> bool:
    """A list or object may not be pushed into a parameter declared as a scalar."""
    if expected is None:
        return True
    types = expected if isinstance(expected, list) else [expected]
    if isinstance(value, (list, tuple, dict)):
        return not (set(types) <= _SCALAR_TYPES)
    return True


def _coerce_type(value: Any, expected: Any) -> Any:
    """Best-effort coercion of a scalar to the schema-declared JSON type.

    A string that parses to a non-finite number (``"nan"``, ``"inf"``) is left alone, so
    schema validation rejects it instead of a NaN reaching a computation.
    """
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
                num = float(value)
            except ValueError:
                return value
            return num if math.isfinite(num) else value
        if "array" in types and not isinstance(value, (list, tuple)):
            return [value]
    except Exception:
        return value
    return value


@dataclass
class RepairOutcome:
    """Result of ``repair_arguments_detailed``."""

    arguments: dict[str, Any]
    #: Applied repairs, structured; surfaced to the agent as ``_arg_repairs``.
    repairs: list[dict[str, Any]] = field(default_factory=list)
    #: Near-misses that were NOT renamed (kept for the teach path and tests).
    refusals: list[dict[str, Any]] = field(default_factory=list)
    #: Near-misses that cross units; the middleware turns the first into ``ARG_UNIT_MISMATCH``.
    unit_conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def notes(self) -> list[str]:
        return [r["note"] for r in self.repairs]


def _short(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def repair_arguments_detailed(
    args: dict[str, Any],
    props: set[str],
    prop_types: dict[str, Any],
) -> RepairOutcome:
    """Repair ``args`` without changing meaning. Pure function -- unit-testable in isolation."""
    if not isinstance(args, dict) or not props:
        return RepairOutcome(args)

    out = RepairOutcome(dict(args))
    repaired = out.arguments

    # 1. Rename unknown keys to valid parameter names, only when meaning is preserved.
    for key in list(repaired.keys()):
        if key in props:
            continue
        value = repaired[key]
        target = None
        rule = None

        alias = _GLOBAL_ALIASES.get(key)
        if alias and alias in props:
            verdict = judge_rename(key, alias, alias=True)
            if verdict.ok and alias not in repaired and _shape_ok(value, prop_types.get(alias)):
                target, rule = alias, "alias"
            elif not verdict.ok:
                out.refusals.append({"from": key, "to": alias, "kind": verdict.kind, "reason": verdict.reason})
                if verdict.kind == "unit":
                    out.unit_conflicts.append({"from": key, "to": alias, "reason": verdict.reason})

        if target is None:
            nkey = "_".join(_tokens(key))
            scored = sorted(
                ((difflib.SequenceMatcher(None, nkey, "_".join(_tokens(p))).ratio(), p) for p in props),
                key=lambda rp: (-rp[0], rp[1]))
            near = [p for r, p in scored if r >= _NEAR_CUTOFF][:5]
            # Same plain words but different semantic tokens (a unit suffix, or none) is a
            # unit near-miss whatever the string similarity says.
            plain_key = _split(key)[1]
            for p in sorted(props):
                if p not in near and plain_key and _split(p)[1] == plain_key:
                    near.append(p)
            for cand in near:
                verdict = judge_rename(key, cand)
                if verdict.ok:
                    ratio = difflib.SequenceMatcher(None, nkey, "_".join(_tokens(cand))).ratio()
                    if (ratio >= _FUZZY_CUTOFF and cand not in repaired
                            and _shape_ok(value, prop_types.get(cand))):
                        target, rule = cand, "spelling"
                        break
                    continue
                if verdict.kind == "unit":
                    out.unit_conflicts.append({"from": key, "to": cand, "reason": verdict.reason})
                out.refusals.append({"from": key, "to": cand, "kind": verdict.kind, "reason": verdict.reason})

        if target:
            repaired[target] = repaired.pop(key)
            out.repairs.append({
                "kind": "rename", "from": key, "to": target, "rule": rule,
                "note": f"renamed '{key}' → '{target}'",
            })

    # 2. Coerce obvious type mismatches on known parameters.
    for key, val in list(repaired.items()):
        if key in prop_types:
            new = _coerce_type(val, prop_types[key])
            if new is not val and new != val:
                repaired[key] = new
                out.repairs.append({
                    "kind": "coerce", "param": key, "to_type": prop_types[key],
                    "from": _short(val), "to": _short(new),
                    "note": f"coerced '{key}' to {prop_types[key]}",
                })

    return out


def repair_arguments(
    args: dict[str, Any],
    props: set[str],
    prop_types: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Return (repaired_args, notes). Pure function -- unit-testable in isolation."""
    outcome = repair_arguments_detailed(args, props, prop_types)
    return outcome.arguments, outcome.notes


def _unit_mismatch_error(tool: str, conflict: dict[str, Any], info: dict) -> dict[str, Any]:
    supplied, expected = conflict["from"], conflict["to"]
    exp_unit = _unit_label(expected)
    sup_unit = _unit_label(supplied)
    desc = (info.get("descs") or {}).get(expected)
    exp_txt = f"'{expected}'" + (f" (unit: {exp_unit})" if exp_unit else "")
    return {
        "error": True,
        "code": "ARG_UNIT_MISMATCH",
        "tool": tool,
        "message": (
            f"'{supplied}' is not a parameter of '{tool}'. The nearest parameter is {exp_txt}, "
            f"but '{supplied}' is in {sup_unit or 'an unstated unit'}. The call was not changed and the value "
            "was not rescaled: a rename that crosses units would change the physical meaning."
        ),
        "supplied_parameter": supplied,
        "supplied_unit": sup_unit,
        "expected_parameter": expected,
        "expected_unit": exp_unit,
        "expected_parameter_description": desc,
        "rescaled": False,
        "recovery": (
            f"Convert the value yourself and call '{tool}' again with '{expected}'"
            + (f" in {exp_unit}" if exp_unit else "") + "."
        ),
    }


def _inject_arg_repairs(tool_result: Any, repairs: list[dict[str, Any]]) -> None:
    """Return the applied repairs to the agent as ``_arg_repairs`` in the tool result.

    Added to a dict result (structured content and the first JSON text block), error
    dicts included; never overwrites a value the tool set. A result that is not a dict
    (a list-returning tool) gets one extra text block, so the note still reaches the agent.
    Best effort: the result must still go out.
    """
    if not repairs or tool_result is None:
        return
    note = [{k: v for k, v in r.items() if k != "note"} for r in repairs]
    try:
        done = False
        structured = getattr(tool_result, "structured_content", None)
        if isinstance(structured, dict) and set(structured) != {"result"}:
            structured.setdefault("_arg_repairs", note)
            done = True
        for block in getattr(tool_result, "content", None) or []:
            raw = getattr(block, "text", None)
            if not isinstance(raw, str):
                continue
            try:
                parsed = json.loads(raw)
            except ValueError:
                break
            if isinstance(parsed, dict):
                if "_arg_repairs" not in parsed:
                    parsed["_arg_repairs"] = note
                    block.text = json.dumps(parsed, default=str)
                done = True
            break
        if not done:
            from mcp.types import TextContent
            tool_result.content = list(getattr(tool_result, "content", None) or []) + [
                TextContent(type="text", text=json.dumps({"_arg_repairs": note}))]
    except Exception as exc:   # pragma: no cover - never block a result on disclosure
        log.debug("could not attach _arg_repairs: %s", exc)


def _seal_effective_arguments(supplied: Any, effective: Any, repairs: list[dict[str, Any]]) -> None:
    """Tell the run record what actually ran. No-op outside a recorded call."""
    try:
        from ai_hydro.session import run_records
        run_records.note_effective_arguments(supplied, effective, repairs)
    except Exception as exc:   # pragma: no cover - recording problems never block a call
        log.warning("arg_repair: could not seal effective arguments: %s", exc)


def _is_structured_refusal(exc: Exception) -> bool:
    """True for a ``StructuredToolError`` a tool raised on purpose with an error envelope.

    Tools whose output schema is an array (``list_claims``, ``list_assumptions``) cannot
    return the usual error dict, so they raise ``StructuredToolError``. That is a
    deliberate, already-structured refusal, not a malformed call: teaching the caller the
    input schema would hide it.

    The JSON-text sniff is kept as a fallback only for a third-party tool that raises a
    plain ``ToolError(json.dumps(envelope))`` (the pre-``StructuredToolError`` contract,
    still possible in community tools); the typed check is the primary mechanism.
    """
    from ai_hydro.mcp.errors import envelope_from_exception
    return envelope_from_exception(exc) is not None


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
                        "descs": {k: ((v or {}).get("description") or "")[:240] for k, v in props.items()},
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

        # 1. Repair before execution -- never across units, always disclosed and sealed.
        supplied = args
        repairs: list[dict[str, Any]] = []
        if info and isinstance(args, dict):
            outcome = repair_arguments_detailed(args, info["props"], info["types"])
            if outcome.unit_conflicts:
                raise StructuredToolError(_unit_mismatch_error(name, outcome.unit_conflicts[0], info))
            if outcome.repairs:
                repairs = outcome.repairs
                log.info("arg_repair: %s: %s", name, "; ".join(outcome.notes))
                msg.arguments = outcome.arguments
                args = outcome.arguments
                _seal_effective_arguments(supplied, args, repairs)

        sig = repr(sorted((args or {}).items()))
        key = (name, sig)

        # 2. Execute; teach on failure
        try:
            result = await call_next(context)
            self._failures.pop(key, None)  # success clears the retry counter
            _inject_arg_repairs(result, repairs)
            return result
        except Exception as e:
            if not info:
                raise  # unknown tool / third-party server — don't interfere
            if _is_structured_refusal(e):
                raise  # the tool already produced a structured error envelope
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            help_payload = self._build_help(name, info, args, str(e), repeated=count >= 2)
            if repairs:
                help_payload["_arg_repairs"] = [{k: v for k, v in r.items() if k != "note"} for r in repairs]
            log.info("arg_repair: returning self-help for failed call to %s", name)
            # An MCP error (isError true) whose text is the help JSON; no ToolResult import.
            raise StructuredToolError(help_payload) from e


def install_arg_repair(mcp) -> None:
    """Attach the ArgRepairMiddleware to a FastMCP instance (idempotent)."""
    if getattr(mcp, "_aihydro_arg_repair_installed", False):
        return
    mcp.add_middleware(ArgRepairMiddleware(mcp))
    mcp._aihydro_arg_repair_installed = True
