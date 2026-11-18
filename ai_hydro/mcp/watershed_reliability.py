"""Surface the delineation's own reliability verdict (aihydro-watershed: ``reliability``, ``reliability_reason``, ``remedy``).

A watershed whose outlet could not be snapped (``reliability == "unreliable"``) is the basin of the raw coordinate's own
flow-direction cell, not necessarily the river's. Three places carry the verdict so it is not lost:

* ``reliability_note`` - the sentence in ``delineate_watershed_from_point``'s ``_note``;
* ``layer_metadata`` - the map layer's metadata;
* ``WatershedReliabilityMiddleware`` - a warning on the result of every tool that consumes the session watershed
  (a quality flag plus ``watershed_reliability_warning``). It warns and records (the middleware runs inside the run-record
  seal); it never refuses.

The same module carries the area check (aihydro-watershed >= 0.4.6: ``area_check``, ``reliability_state``,
``ambiguous_outlet``): ``area_check_note`` (the ``_note`` sentence), ``area_mismatch_flag`` (a warning in the same
shape the downstream middleware uses) and ``area_lookup_provenance`` (what the optional reported-area lookup did).

Every function is a no-op for a watershed result that lacks the keys (an older aihydro-watershed).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

#: tools that read the session ``watershed`` slot.
#: ``lsh_attributes`` is deliberately not listed: since aihydro-lsh 0.4.1 it accepts ``session_id`` (so the run-record
#: seal covers it) but lsh does not read the session watershed, so a watershed warning on its result would be wrong.
DOWNSTREAM_TOOLS = frozenset({
    "compute_twi", "extract_geomorphic_parameters", "extract_hydrological_signatures", "create_cn_grid",
    "fetch_forcing_data", "fetch_soil_attributes_ssurgo", "compute_soil_loss_rusle",
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
    out: dict = {}
    level = reliability_of(data)
    if level is not None:
        out = {"reliability": level, "reliability_reason": str(data.get("reliability_reason") or "")}
        remedy = _remedy_text(data.get("remedy"))
        if remedy:
            out["remedy"] = remedy
    if isinstance(data, dict):
        if data.get("reliability_state"):
            out["reliability_state"] = str(data["reliability_state"])
        status = (data.get("area_check") or {}).get("status") if isinstance(data.get("area_check"), dict) else None
        if status:
            out["area_check_status"] = str(status)
    return out


# ---------------------------------------------------------------------------------------------------------------
# Area check (aihydro-watershed >= 0.4.6)
# ---------------------------------------------------------------------------------------------------------------

#: host of the optional reported-area lookup (``USGS_MONITORING_LOCATIONS_URL`` in aihydro-watershed's
#: ``delineation/area_verification.py``); recorded in provenance, never contacted from this module.
AREA_LOOKUP_HOST = "api.waterdata.usgs.gov"
AREA_LOOKUP_SOURCE = "usgs_waterdata_monitoring_locations"
STATUS_MISMATCH = "area_mismatch"
STATUS_VERIFIED = "verified_by_area"


def _area_check(data: Any) -> Optional[dict]:
    check = data.get("area_check") if isinstance(data, dict) else None
    return check if isinstance(check, dict) else None


def area_mismatch_flag(data: Any) -> Optional[dict]:
    """A warning (same shape as ``downstream_warning``) when the polygon area contradicts the reported area, else ``None``."""
    check = _area_check(data)
    if not check or check.get("status") != STATUS_MISMATCH:
        return None
    flag = {
        "validator": "watershed_reliability", "status": "warning", "severity": "high",
        "message": (
            "AREA MISMATCH: the delineated polygon is NOT verified as the intended basin. "
            f"{check.get('reason') or 'its area differs from the reported drainage area'} "
            f"(reported {check.get('reported_area_km2')} km2 from {check.get('source')}). "
            "Treat the basin, and every result computed from it, as suspect until the outlet is checked."
        ),
        "affected_slots": ["watershed"],
        "metadata": {"quality_flag": "area_mismatch", "reliability_state": data.get("reliability_state"),
                     "rel_err": check.get("rel_err")},
    }
    remedy = _remedy_text(data.get("remedy"))
    if remedy:
        flag["recommendation"] = remedy
    return flag


def attach_warning(body: dict, flag: dict) -> None:
    """Add ``flag`` to a result dict (``quality_flags`` + ``watershed_reliability_warning``), once."""
    _add_warning(body, flag)


def _not_performed(data: Any) -> str:
    """The guard sentence for a requested lookup whose result has no ``area_check`` (the router skipped it)."""
    tier = data.get("method_used") if isinstance(data, dict) else None
    return f"AREA CHECK NOT PERFORMED: tier {tier or 'unknown'} returned no area_check"


def _fmt_rel_err(value: Any) -> str:
    """``+0.3%`` style, or ``n/a`` when the router gave no finite number."""
    try:
        return f"{float(value):+.1%}"
    except (TypeError, ValueError):
        return "n/a"


def area_check_note(data: Any, *, lookup_requested: bool = False) -> str:
    """Sentences for the human ``_note``: the area check, the reliability state and a non-binding outlet ambiguity.

    ``lookup_requested``: the caller asked for the check (a ``gauge_id`` with ``verify_area``). If the router result has
    no ``area_check`` the note says so explicitly instead of staying silent (the tool does not run the check itself).
    """
    if not isinstance(data, dict):
        return ""
    out = ""
    check = _area_check(data)
    if check is None and lookup_requested:
        out += (f"{_not_performed(data)}; the basin is unverified (the installed aihydro-watershed did not apply the "
                "area check on this tier; a newer aihydro-watershed is needed). ")
    if check:
        status = check.get("status")
        if status == STATUS_VERIFIED:
            out += (f"AREA CHECK: polygon area is within tolerance of the reported {check.get('reported_area_km2')} km2 "
                    f"({check.get('source')}, rel_err {_fmt_rel_err(check.get('rel_err'))}); this is area-consistency, not proof "
                    "of the outlet. ")
        elif status == STATUS_MISMATCH:
            out += (f"AREA CHECK MISMATCH: {check.get('reason')}. The basin is NOT verified (reliability_state="
                    f"{data.get('reliability_state')}). ")
        elif check.get("reported_area_km2") is None:
            if check.get("source") in (None, "none") and "lookup_reported_area is off" in str(check.get("reason")):
                pass  # the caller did not ask for a check; nothing to report
            else:
                out += f"AREA CHECK NOT PERFORMED ({check.get('reason')}); the basin is unverified. "
        else:
            out += f"AREA CHECK INCONCLUSIVE: {check.get('reason')}; the basin is unverified. "
    amb = data.get("ambiguous_outlet")
    if isinstance(amb, dict):
        if amb.get("affects_reliability") is False:
            out += (f"AMBIGUOUS_OUTLET (informational inside the conterminous US, location_class="
                    f"{amb.get('location_class')}): an alternative main-stem outlet exists; see ambiguous_outlet. ")
        else:
            out += (f"AMBIGUOUS_OUTLET (affects reliability, location_class={amb.get('location_class')}): "
                    "the point may sit off the main stem; see ambiguous_outlet for both candidates. ")
    return out


def area_lookup_provenance(data: Any, *, gauge_id: Optional[str], requested: bool) -> Optional[dict]:
    """Provenance of the reported-area lookup, or ``None`` when none was requested.

    ``attempted`` is true only when the check's source is the lookup (a caller-supplied area wins and no request is made;
    a failed or refused lookup is still an attempt, its reason is in ``reason``). If the router returned no
    ``area_check`` at all, ``reason`` says ``AREA CHECK NOT PERFORMED`` and ``attempted`` is false.

    ``gauge_id`` is recorded here, not in the result's top-level ``meta.gauge_id``: that field is the result's own
    gauge identity (null for a pour-point delineation); here the id is only the key of the reported-area lookup, so
    copying it up would assert an identity the delineation did not establish.
    """
    if not requested:
        return None
    check = _area_check(data)
    if check is None:
        return {
            "host": AREA_LOOKUP_HOST, "gauge_id": gauge_id, "requested": True, "attempted": False,
            "source": None, "status": None, "reported_area_km2": None, "reason": _not_performed(data),
        }
    attempted = check.get("source") == AREA_LOOKUP_SOURCE
    return {
        "host": AREA_LOOKUP_HOST, "gauge_id": gauge_id, "requested": True, "attempted": attempted,
        "source": check.get("source"), "status": check.get("status"),
        "reported_area_km2": check.get("reported_area_km2"), "reason": check.get("reason"),
    }


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
    """The session a downstream call reads: its ``session_id`` argument, else the chat's bound session."""
    sid = arguments.get("session_id") if isinstance(arguments, dict) else None
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    try:
        from ai_hydro.mcp.helpers import _resolve_session

        return _resolve_session(None, None, allow_auto_create=False, must_exist=False)
    except Exception:
        return None
