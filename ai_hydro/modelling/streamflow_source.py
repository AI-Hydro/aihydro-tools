"""The model step's observed-flow source: names, validation and the recorded fields.

Kept free of any ``aihydro_modelling`` import so the tool kickoffs can validate the option
before (and without) the optional modelling package.
"""
from __future__ import annotations

from typing import Any

#: Where the observed discharge a model trains on comes from. "session" (the default) is the
#: series the study already fetched (``fetch_streamflow_data`` / ``data_fetch``); "camels" is
#: an explicit opt-in that downloads the CAMELS-US benchmark record through pygeohydro (a
#: network host the session never fetched from).
STREAMFLOW_SOURCES = ("session", "camels")
DEFAULT_STREAMFLOW_SOURCE = "session"

_STREAMFLOW_NATURE = {
    "session": "session-cached discharge fetched by the study (not re-downloaded for training)",
    "camels": "CAMELS-US benchmark daily discharge (1980-2014, 671 CONUS gauges) "
              "downloaded through pygeohydro",
}


def normalise_streamflow_source(value: Any) -> str:
    """Return the canonical source name, or raise a structured ``INVALID_STREAMFLOW_SOURCE``."""
    src = DEFAULT_STREAMFLOW_SOURCE if value is None else str(value).strip().lower()
    if src not in STREAMFLOW_SOURCES:
        from aihydro_core.primitives.errors import ToolError
        raise ToolError(
            code="INVALID_STREAMFLOW_SOURCE",
            message=f"streamflow_source must be one of {list(STREAMFLOW_SOURCES)}, got {value!r}.",
            recovery="Use 'session' (the study's own fetched flow) or 'camels' (explicit CAMELS-US download).",
        )
    return src


def streamflow_record(source: Any) -> dict:
    """The fields that state, in a result / sealed record, which flow series was trained on."""
    src = normalise_streamflow_source(source)
    return {"streamflow_source": src, "streamflow_data_nature": _STREAMFLOW_NATURE[src]}
