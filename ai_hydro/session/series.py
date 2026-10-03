"""Retained daily series: write one next to the session, read one back by run id.

A tool that serves a time series (``data_fetch``, ``fetch_streamflow_data``)
writes the full series to a file next to the session (or in the workspace) and
binds the file's digest to its sealed run record (``extra.retained_files``).
A later tool names the series by that run id. ``load_run_series`` resolves the
id to the retained file, re-digests the file against the producer's recorded
digest, and declares the producer as a parent so lineage and input digests are
sealed in the consumer's record.

File format (``aihydro.series/1``)::

    {"schema": "aihydro.series/1", "dates": ["YYYY-MM-DD", ...],
     "values": [float | null, ...], "value_column": str, "units": str, ...}

Files written by ``fetch_streamflow_data`` (``dates`` + ``q_cms``) are read
too. A run whose record names no retained file is "aggregate-only": only the
statistics its row recorded are available.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from aihydro_core.records import digest_bytes, input_ref

log = logging.getLogger("ai_hydro.session.series")

SERIES_SCHEMA = "aihydro.series/1"

#: Keys under which a retained file may hold the primary value array.
_VALUE_KEYS = ("values", "q_cms")
#: Recorded descriptors echoed verbatim (first present key wins).
_META_KEYS = {
    "units": ("units",),
    "product": ("product", "_aihydro_data_product"),
    "source": ("source", "_aihydro_data_source"),
    "variable": ("variable",),
    "timestep": ("timestep", "interval"),
    "spatial_support": ("spatial_support",),
    "aggregation_actual": ("aggregation_actual",),
    "value_column": ("value_column",),
}


class SeriesLoadError(ValueError):
    """The run cannot be resolved to a usable series (plain input error)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class RunSeries:
    """What ``load_run_series`` found for a run id."""

    run_id: str
    session_id: str
    tool: Optional[str]
    values_available: bool
    dates: list = field(default_factory=list)
    values: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)          # units, product, ... as recorded
    file_ref: Optional[str] = None
    file_digest: Optional[str] = None
    recorded_statistics: dict = field(default_factory=dict)   # aggregate-only: the row's key_outputs
    lineage_declared: bool = False


def retain_series(session_id: str, filename: str, payload: dict) -> Optional[tuple[str, str]]:
    """Write ``payload`` as JSON and return ``(path, sha256 digest of the file bytes)``.

    Workspace first, else next to the session file (the lean session JSON drops
    long arrays). ``None`` when neither location is writable.
    """
    saved = None
    try:
        from ai_hydro.session import HydroSession

        saved = HydroSession.load(session_id).write_workspace_file(filename, payload)
    except Exception as exc:
        log.debug("workspace write skipped (%s): %s", filename, exc)
    if not saved:
        from ai_hydro.session import store

        saved = store.write_session_data_file(session_id, filename, payload)
    if not saved:
        return None
    try:
        return saved, digest_bytes(Path(saved).read_bytes())
    except Exception as exc:
        log.warning("could not digest retained series %s: %s", saved, exc)
        return None


def _first(d: dict, keys: tuple) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _retained_file_of(record: dict) -> Optional[dict]:
    files = (record.get("extra") or {}).get("retained_files") or []
    for f in files:
        if isinstance(f, dict) and f.get("path") and f.get("digest"):
            return f
    return None


def load_run_series(session_id: str, run_id: str, *, declare: bool = True) -> RunSeries:
    """Resolve ``run_id`` to its retained series (or its recorded statistics).

    Raises ``SeriesLoadError`` for an unknown run id, a retained file that is
    gone or whose digest no longer matches the producer's sealed digest, or a
    file that is not a series. When ``declare`` is true the producer is
    declared as a parent of the running call (a no-op outside a recorded call).
    """
    from ai_hydro.session import run_records, store
    from ai_hydro.session.refs import resolve_ref

    if not isinstance(run_id, str) or not run_id.strip():
        raise SeriesLoadError("INVALID_INPUT", "series must be the run_id of a prior run")
    run_id = run_id.strip()
    row = store._run_log_read_one(session_id, run_id)
    if row is None:
        raise SeriesLoadError("RUN_NOT_FOUND", f"no run {run_id!r} in session {session_id!r}")
    record = row.get("record") if isinstance(row.get("record"), dict) else {}
    tool = record.get("tool") or row.get("tool_name")

    retained = _retained_file_of(record)
    out = RunSeries(run_id=run_id, session_id=session_id, tool=tool, values_available=False)
    edge = run_records.parent_edge_for_run(session_id, run_id)

    if retained is None:
        out.recorded_statistics = {k: v for k, v in (row.get("key_outputs") or {}).items()
                                   if not str(k).startswith("_")}
        if declare and edge:
            out.lineage_declared = run_records.declare_lineage(
                parents=[edge["parent"]], input_refs=[edge["input_ref"]])
        return out

    workspace = None
    try:
        from ai_hydro.session import HydroSession

        workspace = HydroSession.load(session_id).workspace_dir
    except Exception:
        pass
    path = resolve_ref(retained["path"], session_id, workspace)
    if path is None or not Path(path).exists():
        raise SeriesLoadError(
            "RETAINED_SERIES_MISSING",
            f"run {run_id!r} recorded a retained series ({retained['path']}) but the file is not present")
    raw = Path(path).read_bytes()
    actual = digest_bytes(raw)
    if actual != retained["digest"]:
        raise SeriesLoadError(
            "RETAINED_SERIES_DIGEST_MISMATCH",
            f"the retained series of run {run_id!r} no longer matches the digest the run recorded "
            f"(recorded {retained['digest']}, file {actual})")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise SeriesLoadError("RETAINED_SERIES_UNREADABLE", f"retained series is not JSON: {exc}") from exc
    values_key = next((k for k in _VALUE_KEYS if isinstance(body, dict) and isinstance(body.get(k), list)), None)
    if not isinstance(body, dict) or not isinstance(body.get("dates"), list) or values_key is None:
        raise SeriesLoadError("RETAINED_SERIES_UNREADABLE",
                              "retained file has no 'dates' array with a 'values' (or 'q_cms') array")
    if values_key == "q_cms" and not body.get("units"):
        body = {**body, "units": "m3/s"}               # the legacy file's recorded unit
    out.values_available = True
    out.dates = body["dates"]
    out.values = body[values_key]
    out.meta = {k: _first(body, keys) for k, keys in _META_KEYS.items()}
    out.file_ref = retained["path"]
    out.file_digest = actual
    if declare and edge:
        refs = [edge["input_ref"], input_ref(retained["path"], actual, role="served_data")]
        out.lineage_declared = run_records.declare_lineage(parents=[edge["parent"]], input_refs=refs)
    return out
