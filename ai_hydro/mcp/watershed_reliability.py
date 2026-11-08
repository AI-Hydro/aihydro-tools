"""Surface the delineation's own reliability verdict (aihydro-watershed: ``reliability``, ``reliability_reason``, ``remedy``).

A watershed whose outlet could not be snapped (``reliability == "unreliable"``) is the basin of the raw coordinate's own
flow-direction cell, not necessarily the river's. Three places carry the verdict so it is not lost:

* ``reliability_note`` - the sentence in ``delineate_watershed_from_point``'s ``_note``;
* ``layer_metadata`` - the map layer's metadata;
* ``WatershedReliabilityMiddleware`` - a warning on the result of every tool that consumes the session watershed
  (a quality flag plus ``watershed_reliability_warning``). It warns and records (the middleware runs inside the run-record
  seal); it never refuses.

Every function is a no-op for a watershed result that lacks the keys (an older aihydro-watershed).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

#: tools that read the session ``watershed`` slot; ``lsh_*`` entries only when the call names a ``session_id``.
DOWNSTREAM_TOOLS = frozenset({
    "compute_twi", "extract_geomorphic_parameters", "extract_hydrological_signatures", "create_cn_grid",
    "fetch_forcing_data", "fetch_soil_attributes_ssurgo", "compute_soil_loss_rusle",
    "lsh_attributes", "lsh_geomorphic_attributes", "lsh_dynamic_attributes", "lsh_forcing", "lsh_events",
})

_UNSET = (None, "", [])


def _remedy_text(remedy: Any) -> str:
    if isinstance(remedy, (list, tuple)):
        return " ".join(str(r) for r in remedy if r)
    return str(remedy or "")


def reliability_of(data: Any) -> Optional[str]:
    """``"unreliable"`` / ``"provisional"`` or ``None`` (absent, ``None`` or any other value)."""
    value = data.get("reliability") if isinstance(data, dict) else None
    return value if value in ("unreliable", "provisional") else None


def reliability_note(data: Any) -> str:
    """The sentence for the human ``_note`` ('' when there is nothing to say)."""
    level = reliability_of(data)
    if level is None:
        return ""
    text = f"RELIABILITY {level.upper()}: {data.get('reliability_reason') or 'see reliability_checks'}. "
    remedy = _remedy_text(data.get("remedy"))
    if remedy:
        text += f"Remedy: {remedy} "
    return text


def layer_metadata(data: Any) -> dict:
    """Extra map-layer metadata (strings, like the rest of it); empty when the watershed carries no reliability."""
    level = reliability_of(data)
    if level is None:
        return {}
    out = {"reliability": level, "reliability_reason": str(data.get("reliability_reason") or "")}
    remedy = _remedy_text(data.get("remedy"))
    if remedy:
        out["remedy"] = remedy
    return out


def downstream_warning(session_id: str) -> Optional[dict]:
    """A quality-flag dict when the session watershed is ``unreliable``, else ``None``. Never raises."""
    try:
        from ai_hydro.session import HydroSession

        slot = HydroSession.load(session_id).get("watershed")
        data = (slot or {}).get("data") if isinstance(slot, dict) else None
        if reliability_of(data) != "unreliable":
            return None
        message = (
            "The session watershed is marked UNRELIABLE by the delineation: "
            f"{data.get('reliability_reason') or 'see its reliability_checks'}. "
            "This result describes that basin and inherits the problem."
        )
        flag = {
            "validator": "watershed_reliability", "status": "warning", "severity": "high",
            "message": message, "affected_slots": ["watershed"],
            "metadata": {"quality_flag": "watershed_unreliable", "reliability": "unreliable"},
        }
        remedy = _remedy_text(data.get("remedy"))
        if remedy:
            flag["recommendation"] = remedy
        return flag
    except Exception as exc:
        log.debug("watershed reliability check skipped: %s", exc)
        return None


def _add_warning(body: dict, flag: dict) -> None:
    flags = body.get("quality_flags")
    if not isinstance(flags, list):
        flags = [] if flags in (None, "") else [flags]
    if not any(isinstance(f, dict) and f.get("validator") == "watershed_reliability" for f in flags):
        flags.append(flag)
    body["quality_flags"] = flags
    body["watershed_reliability_warning"] = flag["message"]


def apply_to_tool_result(tool_result: Any, flag: dict) -> None:
    """Add ``flag`` to a successful dict result (structured content and JSON text block). Best effort."""
    structured = getattr(tool_result, "structured_content", None)
    if isinstance(structured, dict) and set(structured) != {"result"} and not structured.get("error"):
        _add_warning(structured, flag)
    for block in getattr(tool_result, "content", None) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict) and not parsed.get("error"):
            _add_warning(parsed, flag)
            block.text = json.dumps(parsed, default=str)
        return


def session_for_call(tool: str, arguments: dict) -> Optional[str]:
    """The session a downstream call reads: its ``session_id`` argument, else the chat's bound session (native tools only)."""
    sid = arguments.get("session_id") if isinstance(arguments, dict) else None
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    if tool.startswith("lsh_"):
        return None
    try:
        from ai_hydro.mcp.helpers import _resolve_session

        return _resolve_session(None, None, allow_auto_create=False, must_exist=False)
    except Exception:
        return None
