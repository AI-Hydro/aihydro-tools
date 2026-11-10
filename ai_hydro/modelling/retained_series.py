"""Resolve the daily arrays a model job trains on from the study's own retained artefact.

``fetch_streamflow_data`` / ``data_fetch`` keep the full series in a content-addressed file
(``streamflow_<gauge>_<sha256[:12]>.json``) and record its path and digest on the slot
(``data._data_file``, ``meta.retained_series``). The job used to look for a fixed
``streamflow_<gauge>.json`` in the workspace instead: a name nothing writes (and with no
workspace there is no file at all), so training could not load the very series the study
had fetched (DEMO-1 rehearsal, finding 4b).

The slot is now authoritative. The job follows the slot's own pointer, re-digests the file and
refuses a mismatch with the digest the fetch recorded: it will not train on bytes the study did
not fetch. The old fixed name is a last resort, used only when the slot names no file at all
(forcing, and sessions written before the pointer existed); it carries no digest to check.

Free of any ``aihydro_modelling`` import so it runs in the public test suite.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("ai_hydro.modelling")


def _slot_pointer(result: dict) -> tuple[Optional[str], Optional[str]]:
    """``(path or ref, recorded digest)`` the slot names for its retained file, else ``(None, None)``."""
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    retained = meta.get("retained_series") if isinstance(meta.get("retained_series"), dict) else {}
    path = data.get("_data_file") or retained.get("path")
    digest = retained.get("digest")
    # A digest binds the file the slot recorded, not whichever other file ``_data_file`` may name.
    if digest and retained.get("path") and path and str(retained["path"]) != str(path):
        digest = None
    return (str(path) if path else None), (str(digest) if digest else None)


def _declare_input(session_id: Optional[str], path: Path, digest: str, workspace_dir: Any) -> None:
    """Name the file the job read in the call's lineage (a no-op outside a recorded call)."""
    try:
        from aihydro_core.records import input_ref
        from ai_hydro.session import run_records
        from ai_hydro.session.refs import to_ref

        run_records.declare_lineage(
            input_refs=[input_ref(to_ref(path, workspace_dir), digest, role="served_data")])
    except Exception as exc:          # lineage bookkeeping never costs a training run
        log.debug("input lineage not declared: %s", exc)


def load_slot_data(session: Any, slot: str, gauge_id: str) -> dict:
    """The slot's ``data`` dict with its daily arrays (``dates`` ...), loaded from the retained file.

    Raises ``ValueError`` (the caller turns it into a structured refusal) when the slot is
    empty, when the retained file is gone or its sha256 differs from the one recorded at fetch
    time, or when no file holding the arrays can be found.
    """
    result = getattr(session, slot, None)
    if result is None:
        tool = {"streamflow": "fetch_streamflow_data",
                "forcing": "fetch_forcing_data"}.get(slot, slot)
        raise ValueError(f"No {slot} cached for gauge {gauge_id}. Run {tool} first.")

    data = result.get("data", {})
    if "dates" in data:
        return data

    from aihydro_core.records import digest_bytes
    from ai_hydro.session.refs import resolve_ref

    workspace_dir = getattr(session, "workspace_dir", None)
    session_id = getattr(session, "session_id", None)
    pointer, recorded = _slot_pointer(result)
    problem: Optional[str] = None

    if pointer:
        path = resolve_ref(pointer, session_id=session_id, workspace_dir=workspace_dir)
        if path is None or not path.is_file():
            problem = f"the retained {slot} file {pointer!r} is not on disk"
        else:
            raw = path.read_bytes()
            actual = digest_bytes(raw)
            if recorded and actual != recorded:
                raise ValueError(
                    f"the retained {slot} file {path.name} has sha256 {actual}, but the fetch recorded "
                    f"{recorded}. The file changed after it was fetched; refusing to train on it.")
            try:
                loaded = json.loads(raw.decode("utf-8"))
            except ValueError as exc:
                raise ValueError(f"the retained {slot} file {path.name} is not valid JSON: {exc}") from exc
            if isinstance(loaded, dict) and "dates" in loaded:
                if recorded:
                    _declare_input(session_id, path, actual, workspace_dir)
                return loaded
            problem = f"the retained {slot} file {path.name} holds no daily arrays"

    if workspace_dir:
        legacy = Path(workspace_dir) / f"{slot}_{gauge_id}.json"
        if legacy.exists():
            loaded = json.loads(legacy.read_text())
            if isinstance(loaded, dict) and "dates" in loaded:
                return loaded

    detail = f" ({problem})" if problem else ""
    raise ValueError(
        f"{slot} data is missing daily arrays for gauge {gauge_id}{detail}. "
        f"Re-run the {'fetch_streamflow_data or data_fetch' if slot == 'streamflow' else 'fetch'} tool for this gauge.")
