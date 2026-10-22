"""Compact scientific output evidence retained with a run, never reconstructed.

Arrays are intentionally omitted. This is a metric/uncertainty record, not a
reproducibility bundle or proof that the method and study scope are appropriate.
"""
from __future__ import annotations

import math
from typing import Any


#: A short list of plain strings survives compaction inside the uncertainty block: the machine-readable ``scope``
#: of an interval (``{"varies": [...], "assumes": [...], "excludes": [...]}``) is exactly such lists, and the
#: claim gate reads them back from the run row. Numeric arrays and long lists are still dropped.
_MAX_KEPT_STRING_LIST = 16


def _compact(value: Any, depth: int = 0, keep_string_lists: bool = False) -> Any:
    if depth > 8:
        return None
    if isinstance(value, dict):
        return {str(k): _compact(v, depth + 1, keep_string_lists) for k, v in value.items()
                if not isinstance(v, (list, tuple)) or _is_keepable_string_list(v, keep_string_lists)}
    if isinstance(value, (list, tuple)):          # only a short string list inside the uncertainty block
        return [str(x) for x in value] if _is_keepable_string_list(value, keep_string_lists) else None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


def _is_keepable_string_list(v: Any, enabled: bool) -> bool:
    return (enabled and isinstance(v, (list, tuple)) and len(v) <= _MAX_KEPT_STRING_LIST
            and all(isinstance(x, str) for x in v))


def capture_result_evidence(result: dict) -> dict:
    """Keep exact scalar/dict values, CIs and checks without summary counts."""
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    uncertainty = data.get("_uncertainty", result.get("uncertainty"))
    flags = result.get("quality_flags")
    return {
        "schema_version": 1,
        "data": _compact({k: v for k, v in data.items() if not str(k).startswith("_")}),
        "uncertainty": _compact(uncertainty, keep_string_lists=True),
        "quality_flags": [_compact(f) for f in flags] if isinstance(flags, list) else None,
        "error": bool(result.get("error")),
        "status": result.get("status"),
    }
