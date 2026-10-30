"""The gauge a modelling job trains on: the session's own ``site_id``, or an error.

A session id is a label the researcher chose ("kings-study"), not a gauge. Using it as
one made CAMELS/USGS lookups miss and the job silently fall back to whatever series the
session held. The gauge is therefore resolved from ``session.site_id`` only; an empty
value is a structured error and nothing is started.
"""
from __future__ import annotations


def gauge_error(session, session_id: str) -> dict | None:
    """Structured error dict when the session names no gauge, else ``None``."""
    gauge = str(getattr(session, "site_id", "") or "").strip()
    if gauge:
        return None
    return {
        "error": True,
        "code": "GAUGE_NOT_SET",
        "message": (
            f"Session '{session_id}' has no site_id, so there is no gauge to train on. "
            "The session id is a label, not a gauge, and is never used as one."
        ),
        "recovery": (
            "Delineate from a gauge (delineate_watershed) or fetch its streamflow "
            "(fetch_streamflow_data / data_fetch) in this session so site_id is set."
        ),
    }


def require_gauge(session, session_id: str) -> str:
    """The session's gauge id; raises ``ToolError(GAUGE_NOT_SET)`` when there is none."""
    err = gauge_error(session, session_id)
    if err:
        from aihydro_core.primitives.errors import ToolError
        raise ToolError(code=err["code"], message=err["message"], recovery=err["recovery"])
    return str(session.site_id).strip()


# ── Slot consistency (tool-audit F9) ─────────────────────────────────────────

_CMS_UNITS = {"m3/s", "m^3/s", "m³/s", "cms", "m3 s-1", "m3s-1"}


def _norm_gauge(value) -> str:
    g = str(value or "").strip().lower()
    return g[len("usgs:"):] if g.startswith("usgs:") else g


def _declared_gauge(slot) -> str:
    """The gauge a cached slot says it belongs to ('' when it does not say)."""
    if not isinstance(slot, dict):
        return ""
    for holder in (slot.get("data") or {}, slot.get("meta") or {}):
        if isinstance(holder, dict) and holder.get("gauge_id"):
            return _norm_gauge(holder["gauge_id"])
    return ""


def basin_consistency_error(session, gauge_id: str) -> dict | None:
    """Structured error when the cached slots belong to different gauges, or the
    streamflow slot declares units other than m3/s; ``None`` when consistent.

    Training converts discharge to mm/day with the *watershed* area and pairs it with the
    *forcing* slot. A slot cached for another gauge therefore gives runoff off by an
    order of magnitude, or forcing from another basin, with no error. A slot that does
    not declare a gauge is not checked (older sessions); one that does must agree.
    """
    want = _norm_gauge(gauge_id)
    mismatched = {}
    for slot in ("watershed", "streamflow", "forcing"):
        have = _declared_gauge(getattr(session, slot, None))
        if have and have != want:
            mismatched[slot] = have
    if mismatched:
        return {
            "error": True,
            "code": "BASIN_MISMATCH",
            "message": (
                f"Cannot train on gauge '{gauge_id}': cached slots belong to other gauges "
                f"({mismatched}). Runoff would be scaled by another basin's area or driven "
                "by another basin's weather."
            ),
            "recovery": (
                "Re-run the fetch for the slots named above with this session's gauge "
                "(delineate_watershed / fetch_streamflow_data / fetch_forcing_data), or "
                "use a session per basin."
            ),
        }
    sf = getattr(session, "streamflow", None)
    units = ""
    if isinstance(sf, dict):
        units = str((sf.get("data") or {}).get("units") or "").strip()
    if units and units.lower() not in _CMS_UNITS:
        return {
            "error": True,
            "code": "UNIT_MISMATCH",
            "message": (
                f"The cached streamflow declares units '{units}'; training reads it as m3/s. "
                "Training would be wrong by a constant factor."
            ),
            "recovery": "Re-fetch the streamflow (fetch_streamflow_data) so it is stored in m3/s.",
        }
    return None
