"""Compact scientific output evidence retained with a run, never reconstructed.

Arrays are intentionally omitted. This is a metric/uncertainty record, not a
reproducibility bundle or proof that the method and study scope are appropriate.
"""
from __future__ import annotations

import math
from typing import Any


def _compact(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(value, dict):
        return {str(k): _compact(v, depth + 1) for k, v in value.items()
                if not isinstance(v, (list, tuple))}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


def capture_result_evidence(result: dict) -> dict:
    """Keep exact scalar/dict values, CIs and checks without summary counts."""
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    uncertainty = data.get("_uncertainty", result.get("uncertainty"))
    flags = result.get("quality_flags")
    return {
        "schema_version": 1,
        "data": _compact({k: v for k, v in data.items() if not str(k).startswith("_")}),
        "uncertainty": _compact(uncertainty),
        "quality_flags": [_compact(f) for f in flags] if isinstance(flags, list) else None,
        "error": bool(result.get("error")),
        "status": result.get("status"),
    }
