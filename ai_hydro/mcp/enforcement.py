"""
Tier 1 post-run enforcement layer.

After any Tier 1 tool completes successfully, post_run() fires all
registered validators for that tool and injects their results into
result["quality_flags"].

Design constraints:
  - Never raises: validator failures are captured in quality_flags, not
    propagated — a malfunctioning validator must not crash a successful tool.
  - No-op on error results: if result["error"] is truthy, skip validation
    (the tool already failed; validators would produce misleading output).
  - All Tier 1 tools receive quality_flags: [] even if no validators fired,
    so downstream code can always rely on the key being present.

Registration happens in ai_hydro/mcp/__init__.py after all tool modules are
imported. This avoids circular imports (enforcement.py imports nothing from
any tool module at module level).

Usage from a Tier 1 tool:
    from ai_hydro.mcp.enforcement import post_run
    ...
    d = _result_to_dict(result)
    d = post_run("my_tool_name", session_id, d)
    return d
"""
from __future__ import annotations

import logging
import secrets
from datetime import date, datetime, timezone
from typing import Callable

log = logging.getLogger("ai_hydro.enforcement")

# Short abbreviations for run_id readability
_TOOL_ABBREVS: dict[str, str] = {
    "extract_hydrological_signatures": "sigs",
    "delineate_watershed":             "wshed",
    "extract_geomorphic_parameters":   "geom",
    "compute_twi":                     "twi",
    "create_cn_grid":                  "cn",
    "separate_baseflow":               "bflow",
    "train_hydro_model":               "model",
    "get_model_results":               "mres",
    "add_claim":                       "claim",
    "add_assumption":                  "assump",
    "promote_claim_to_registry":       "promo",
    "check_water_balance_consistency": "vwb",
    "check_temporal_alignment":        "vta",
    "check_unit_consistency":          "vuc",
    "fetch_streamflow_data":           "q",
}


def _generate_run_id(tool_name: str, session_id: str) -> str:
    """
    Generate a stable, sortable, human-readable run identifier.

    Format: {tool_abbrev}.{yyyymmdd}.{session_frag8}.{hex4}
    Example: sigs.20260508.01031500.a3f2

    The hex suffix provides collision avoidance for multiple calls in the
    same session on the same day.
    """
    abbrev = _TOOL_ABBREVS.get(tool_name, tool_name[:5])
    date_str = date.today().strftime("%Y%m%d")
    session_frag = session_id[:8].replace("-", "").replace(".", "")
    hex4 = secrets.token_hex(2)
    return f"{abbrev}.{date_str}.{session_frag}.{hex4}"


def _write_run_log(
    session_id: str,
    run_id: str,
    tool_name: str,
    result: dict,
) -> None:
    """
    Write a run record to the session's _run_log slot.

    Never raises — a logging failure must not invalidate a successful tool call.
    Captures key_outputs from result['data'] (small scalars only, no arrays).
    """
    try:
        from ai_hydro.session.store import HydroSession
        session = HydroSession.load(session_id)
        run_log: dict = dict(session.get("_run_log") or {})

        # Capture scalar outputs only — skip large arrays and private keys
        raw_data = result.get("data") or {}
        key_outputs = {
            k: v for k, v in raw_data.items()
            if not k.startswith("_") and not isinstance(v, list)
        }
        # Include quality_flags summary if present
        if result.get("quality_flags"):
            key_outputs["_quality_flags"] = [
                {"validator": f.get("validator"), "status": f.get("status")}
                for f in result["quality_flags"]
            ]

        run_log[run_id] = {
            "run_id":    run_id,
            "tool_name": tool_name,
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "key_outputs": key_outputs,
        }
        session.set("_run_log", run_log)
        session.save()
    except Exception as exc:
        log.warning("Failed to write run log for %s: %s", run_id, exc)

# Registry: tool_name → list of (validator_fn, kwargs_builder)
# kwargs_builder(session_id: str) -> dict
_REGISTRY: dict[str, list[tuple[Callable, Callable]]] = {}


def register_post_validator(
    tool_name: str,
    validator_fn: Callable,
    kwargs_builder: Callable,
) -> None:
    """
    Register validator_fn to auto-fire after tool_name completes.

    validator_fn   : the validator callable (e.g. check_water_balance_consistency)
    kwargs_builder : called with session_id → dict of kwargs for validator_fn
                     e.g.  lambda sid: {"session_id": sid}
    """
    _REGISTRY.setdefault(tool_name, []).append((validator_fn, kwargs_builder))


def post_run(tool_name: str, session_id: str, result: dict) -> dict:
    """
    Inject quality_flags and _run_id into result; fire registered validators.

    Called at the end of every Tier 1 tool, regardless of whether any
    validators are registered.  Always returns the (mutated) result dict.

    Injected fields (always present on successful Tier 1 outputs):
        quality_flags : list  — validator results (may be empty)
        _run_id       : str   — stable evidence-binding key for add_claim()
    """
    # Ensure quality_flags key is always present on Tier 1 outputs
    if "quality_flags" not in result:
        result["quality_flags"] = []

    # Skip validation and run-id on failed tool calls
    if result.get("error"):
        return result

    for validator_fn, kwargs_builder in _REGISTRY.get(tool_name, []):
        try:
            kwargs = kwargs_builder(session_id)
            vresult = validator_fn(**kwargs)
            result["quality_flags"].append(vresult)
            _log_flag(tool_name, vresult)
        except Exception as exc:
            log.warning(
                "Post-run validator '%s' for tool '%s' raised: %s",
                getattr(validator_fn, "__name__", "?"),
                tool_name,
                exc,
            )
            result["quality_flags"].append({
                "validator": getattr(validator_fn, "__name__", "unknown"),
                "status": "error",
                "message": str(exc),
                "severity": None,
            })

    # Generate run_id and persist to session run log for evidence binding
    run_id = _generate_run_id(tool_name, session_id)
    result["_run_id"] = run_id
    _write_run_log(session_id, run_id, tool_name, result)

    return result


def get_registry_snapshot() -> dict[str, list[str]]:
    """Return tool → [validator_fn_name, ...] for inspection / testing."""
    return {
        tool: [fn.__name__ for fn, _ in entries]
        for tool, entries in _REGISTRY.items()
    }


def _log_flag(tool_name: str, flag: dict) -> None:
    status = flag.get("status", "?")
    severity = flag.get("severity")
    validator = flag.get("validator", "?")
    if status == "pass":
        log.debug("[enforcement] %s → %s: pass", tool_name, validator)
    elif status in ("warning", "fail"):
        sev = f" ({severity})" if severity else ""
        msg = flag.get("message", "")
        log.warning(
            "[enforcement] %s → %s: %s%s — %s",
            tool_name, validator, status, sev, msg,
        )
