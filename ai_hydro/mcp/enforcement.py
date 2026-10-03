"""
Tier 1 post-run audit layer (soft gate — see below for the hard gate).

After any Tier 1 tool completes successfully, post_run() fires all
registered validators for that tool and injects their results into
result["quality_flags"]. This is advisory: nothing here blocks a tool call or
a claim from being created. It is the *audit* trail, not *enforcement* in the
sense of "prevents an action" — the module name predates this distinction and
is kept for now to avoid an import-path break; read "audit" wherever this
file says "enforcement".

The one place scientific claims are actually gated is claim promotion
(`promote_claim_to_registry` in ai_hydro/mcp/tools_ledger.py), which requires
evidence spans, limitations, and researcher approval before a claim enters the
global registry. That is the hard gate; quality_flags produced here are input
to it, not a substitute for it.

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

# Same secret-shaped-name blocklist family as tools_execution.py::_scrub_env —
# defence in depth in case a tool's kwargs ever carry an API key/token. One
# definition, shared with the run-record builder.
from ai_hydro.session.run_records import SECRET_INPUT_KEY_PATTERN as _SECRET_INPUT_KEY_PATTERN
_MAX_INPUT_STR_LEN = 500


def _scrub_tool_inputs(inputs: dict | None) -> dict:
    """
    Reduce a tool call's kwargs to the small, JSON-safe scalar subset worth
    recording in the run log for replay/lineage.

    Mirrors _write_run_log's existing key_outputs philosophy (scalars only,
    no arrays) rather than inventing a new rule: session_id/ctx/private keys
    are dropped (session_id is already a top-level run-log field; ctx is an
    MCP framework object, not data), secret-shaped names are dropped, and
    long strings (e.g. an inline geometry_geojson blob) are recorded by
    length only rather than included or silently truncated mid-value.
    """
    if not inputs:
        return {}
    scrubbed: dict = {}
    for key, value in inputs.items():
        if not key or key.startswith("_") or key in ("ctx", "session", "session_id"):
            continue
        if _SECRET_INPUT_KEY_PATTERN.search(key):
            continue
        if value is None or isinstance(value, (bool, int, float)):
            scrubbed[key] = value
        elif isinstance(value, str):
            scrubbed[key] = value if len(value) <= _MAX_INPUT_STR_LEN else f"<{len(value)} chars, omitted>"
        # lists/dicts/other complex types are omitted, same as key_outputs.
    return scrubbed

# Short abbreviations for run_id readability
_TOOL_ABBREVS: dict[str, str] = {
    "extract_hydrological_signatures": "sigs",
    "delineate_watershed":             "wshed",
    "delineate_watershed_from_point": "wshed",
    "extract_geomorphic_parameters":   "geom",
    "compute_twi":                     "twi",
    "create_cn_grid":                  "cn",
    "separate_baseflow":               "bflow",
    "propose_and_train":               "ptrain",
    "run_autoresearch":                "srch",
    "get_leaderboard":                 "lbrd",
    "train_hydro_model":               "model",
    "get_model_results":               "mres",
    "add_claim":                       "claim",
    "add_assumption":                  "assump",
    "promote_claim_to_registry":       "promo",
    "check_water_balance_consistency": "vwb",
    "check_temporal_alignment":        "vta",
    "check_unit_consistency":          "vuc",
    "fetch_streamflow_data":           "q",
    "data_fetch":                      "dfetch",
    "summarize_series":                "ssum",
    "detect_threshold_runs":           "runs",
    "compare_series":                  "cmp",
    "bootstrap_statistic":             "boot",
    "measure_feature":                 "meas",
}


def _generate_run_id(tool_name: str, session_id: str) -> str:
    """
    Generate a sortable, human-readable run identifier.

    Format: {tool_abbrev}.{yyyymmdd}.{session_frag8}.{hex8}
    Example: sigs.20260508.01031500.a3f2c91e

    The hex suffix (32 bits) separates calls in the same session on the same
    day. It used to be 16 bits, which collides after a few hundred calls per
    tool and day. Every consumer treats the id as an opaque token
    (``[A-Za-z0-9._-]+`` in the auditor grammar); none parses the suffix, so
    older 4-hex ids remain valid. Uniqueness within a session is enforced at
    generation by ``_generate_unique_run_id`` and again at write time, where a
    sealed row can never be replaced.
    """
    abbrev = _TOOL_ABBREVS.get(tool_name, tool_name[:5])
    date_str = date.today().strftime("%Y%m%d")
    session_frag = session_id[:8].replace("-", "").replace(".", "")
    hex8 = secrets.token_hex(4)
    return f"{abbrev}.{date_str}.{session_frag}.{hex8}"


def _generate_unique_run_id(tool_name: str, session_id: str) -> str:
    """A run id not already present in the session's run log."""
    from ai_hydro.session.store import _run_log_read_one

    run_id = _generate_run_id(tool_name, session_id)
    for _ in range(8):
        if _run_log_read_one(session_id, run_id) is None:
            return run_id
        run_id = _generate_run_id(tool_name, session_id)
    # Eight collisions in a row at 32 bits means something is wrong; widen.
    return f"{run_id}{secrets.token_hex(4)}"


def _write_run_log(
    session_id: str,
    run_id: str,
    tool_name: str,
    result: dict,
    inputs: dict | None = None,
) -> None:
    """
    Write a run-log row for the session.

    Never raises — a logging failure must not invalidate a successful tool call.
    Captures key_outputs from result['data'] (small scalars only, no arrays)
    and, when the caller supplies them, scrubbed tool inputs (see
    _scrub_tool_inputs) — this is what lets Session Replay/Experiment Table
    show what a run was actually called with, not just what it produced.
    """
    try:
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

        entry = {
            "run_id":    run_id,
            "tool_name": tool_name,
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "key_outputs": key_outputs,
        }
        from ai_hydro.session.evidence import capture_result_evidence
        entry["evidence"] = capture_result_evidence(result)
        scrubbed_inputs = _scrub_tool_inputs(inputs)
        if scrubbed_inputs:
            entry["inputs"] = scrubbed_inputs
        # One row, written directly. The old load -> mutate whole log -> save
        # cycle re-sent every existing row on each call; insert-only rows make
        # that both wasteful and a place for a stale snapshot to do harm.
        from ai_hydro.session.store import _run_log_record
        status = _run_log_record(session_id, run_id, entry, writer="post_run")
        if status not in ("inserted", "replaced", "noop"):
            log.warning("Run log row %s was not stored (%s)", run_id, status)
    except Exception as exc:
        log.warning("Failed to write run log for %s: %s", run_id, exc)

# ---------------------------------------------------------------------------
# Validator registry
# tool_name → list of (validator_fn, kwargs_builder)
# kwargs_builder(session_id: str) -> dict
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, list[tuple[Callable, Callable]]] = {}

# ---------------------------------------------------------------------------
# Next-steps registry
# tool_name → list of {tool, reason, when} hints injected into every result.
#
# Each hint:
#   tool   : MCP tool name the agent should consider calling next
#   reason : one-line natural-language rationale (shown directly to the agent)
#   when   : optional condition string ("if model.nse < 0.5", etc.)
#
# Registered in ai_hydro/mcp/__init__.py after all modules are loaded.
# ---------------------------------------------------------------------------
_NEXT_STEPS_REGISTRY: dict[str, list[dict]] = {}


def register_next_steps(tool_name: str, steps: list[dict]) -> None:
    """
    Register a list of next-step hints for a tool.

    After tool_name completes successfully, post_run() injects these hints
    into result["next_steps"] (using setdefault so a tool's own explicit
    next_steps list is never overwritten).

    Each step dict should have:
        tool   (str)  — MCP tool name
        reason (str)  — one-line rationale for the agent
        when   (str, optional) — condition that makes this step most relevant

    Registration order matters: earlier steps appear first.  Call this
    multiple times to append; later calls extend (not replace) the list.
    """
    _NEXT_STEPS_REGISTRY.setdefault(tool_name, []).extend(steps)


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


def post_run(tool_name: str, session_id: str, result: dict, inputs: dict | None = None) -> dict:
    """
    Inject quality_flags and _run_id into result; fire registered validators.

    Called at the end of every Tier 1 tool, regardless of whether any
    validators are registered.  Always returns the (mutated) result dict.

    Injected fields (always present on successful Tier 1 outputs):
        quality_flags : list  — validator results (may be empty)
        _run_id       : str   — stable evidence-binding key for add_claim()

    inputs : optional dict of the tool's own call kwargs (e.g. {"gauge_id":
        gauge_id, "start_date": start_date}), built by the caller — post_run
        has no access to the caller's arguments otherwise. Scrubbed via
        _scrub_tool_inputs before being written to the run log. Omit for
        tools where recording inputs isn't worth the caller-side plumbing;
        the run log simply won't have an "inputs" key for that entry.
    """
    # Ensure quality_flags key is always present on Tier 1 outputs
    if "quality_flags" not in result:
        result["quality_flags"] = []

    # Inject next_steps hints (tool's own list wins; registry fills the gap)
    if "next_steps" not in result:
        hints = _NEXT_STEPS_REGISTRY.get(tool_name)
        if hints:
            result["next_steps"] = hints

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
    run_id = _generate_unique_run_id(tool_name, session_id)
    result["_run_id"] = run_id
    _write_run_log(session_id, run_id, tool_name, result, inputs)

    return result


def get_registry_snapshot() -> dict[str, list[str]]:
    """Return tool → [validator_fn_name, ...] for inspection / testing."""
    return {
        tool: [fn.__name__ for fn, _ in entries]
        for tool, entries in _REGISTRY.items()
    }


def get_next_steps_snapshot() -> dict[str, list[dict]]:
    """Return a copy of the next-steps registry for inspection / testing."""
    return {tool: list(steps) for tool, steps in _NEXT_STEPS_REGISTRY.items()}


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
