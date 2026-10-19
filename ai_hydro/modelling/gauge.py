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
