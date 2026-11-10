"""``data_nature``: the observed-vs-modelled label aihydro-data attaches to every product.

aihydro-data >= 0.2.10 declares it per product (``ProductSpec.data_nature``) and carries it into
``FetchResult.data_nature`` and the ``data_fetch`` result. Earlier releases have no such field.
This module only *carries* what the data layer recorded, next to ``spatial_support``; it never
derives a class from a product name or a source. The empty string means "not recorded", and any
value outside the six-class vocabulary is treated the same way (a typo or a future class would
otherwise pass as a claim the data layer never made).
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

log = logging.getLogger("ai_hydro.data.nature")

#: The six classes (aihydro-data ``DataNature``; keep in step with that Literal).
DATA_NATURE_CLASSES = (
    "gauge_observed", "gridded_obs_derived", "reanalysis",
    "modelled", "satellite_derived", "mapped",
)

NOT_RECORDED = ""


def clean_nature(value: Any) -> str:
    """The value if it is one of the six classes, else ``""`` (not recorded)."""
    return value if isinstance(value, str) and value in DATA_NATURE_CLASSES else NOT_RECORDED


def nature_of(obj: Any) -> str:
    """``data_nature`` of a FetchResult-like object or a result mapping; ``""`` when absent.

    Safe on aihydro-data 0.2.9, whose ``FetchResult`` has no such field.
    """
    if obj is None:
        return NOT_RECORDED
    if isinstance(obj, Mapping):
        return clean_nature(obj.get("data_nature"))
    return clean_nature(getattr(obj, "data_nature", None))


def product_nature(product_id: str) -> str:
    """The registry's declared ``data_nature`` for ``product_id``; ``""`` when the installed
    aihydro-data predates the field, does not know the product, or cannot be imported."""
    try:
        from aihydro_data import get_product

        return clean_nature(getattr(get_product(product_id), "data_nature", None))
    except (ImportError, LookupError):      # no aihydro-data, or an unknown product id (KeyError)
        return NOT_RECORDED
    except Exception as exc:                # unexpected: say so, still never raise into a fetch
        log.warning("data_nature lookup for %s failed: %s: %s", product_id, type(exc).__name__, exc)
        return NOT_RECORDED
