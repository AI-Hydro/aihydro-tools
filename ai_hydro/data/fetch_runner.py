"""
Async data-fetch subprocess runner.

Called as:  python -m ai_hydro.data.fetch_runner <artifact_dir>

Reads <artifact_dir>/job_config.json, runs a single ``aihydro_data.fetch()``
in this detached process, and writes <artifact_dir>/status.json checkpoints.
This is what lets a *slow* data source (notably GloFAS, whose EWDS retrieval is
queued and can take minutes) run without blocking the MCP server — the tool
dispatches a job and returns a handle immediately; the agent polls for the
result.

Why this lives in aihydro-tools (not aihydro-data):
    ``aihydro_data.fetch()`` is a clean *synchronous* primitive — the
    async/dispatch policy is an application concern, not a data-layer one.
    Keeping the runner here preserves the layering (core ← data ← tools) while
    reusing aihydro-core's jobs substrate (the same one train_hydro_model uses).

job_config.json schema
----------------------
{
  "job_id":   str,
  "variable": str,                 # e.g. "streamflow"
  "geometry": <GeoJSON dict | [lat,lon] | bbox | WKT str>,
  "start":    "YYYY-MM-DD",
  "end":      "YYYY-MM-DD",
  "product":  str | null,          # pin a product, else auto-route
  "aggregation": str,              # default "basin_mean"
  "session_id":  str | null,       # if set, result is cached into the session
  "sessions_dir": str | null       # test-isolation override
}

status.json schema  (read by aihydro_core.jobs.get_job_status)
--------------------------------------------------------------
{
  "job_id": str,
  "status": "pending" | "running" | "complete" | "failed",
  "progress": {"stage": str},
  "partial_results": {... compact streamflow dict ...} | null,
  "error": {"code": str, "message": str} | null,
  "log_path": str,
  "updated_at": "<iso8601>"
}
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


log = logging.getLogger("ai_hydro.data.fetch_runner")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _write_status(
    artifact_dir: Path,
    job_id: str,
    status: str,
    progress: dict | None = None,
    partial_results: dict | None = None,
    error: dict | None = None,
) -> None:
    payload = {
        "job_id": job_id,
        "status": status,
        "progress": progress or {"stage": status},
        "partial_results": partial_results,
        "error": error,
        "log_path": str(artifact_dir / "fetch.log"),
        "updated_at": _now(),
    }
    (artifact_dir / "status.json").write_text(json.dumps(payload, indent=2))


def _series_to_compact(result, variable: str) -> dict:
    """Map a FetchResult (DataFrame time series) → the compact dict the
    streamflow tool already returns, plus served-product provenance."""
    import pandas as pd

    df = result.data
    out: dict = {
        "variable": variable,
        "product": result.product,
        "source": result.source,
        "citation": result.citation,
        "units": "m3/s",
    }
    if isinstance(df, pd.DataFrame) and variable in df.columns and "date" in df.columns:
        dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist()
        vals = [
            float(v) if (v is not None and v == v) else None  # NaN → None
            for v in df[variable].tolist()
        ]
        out.update({
            "dates": dates,
            "q_cms": vals,
            "n_days": len(vals),
        })
        valid = [v for v in vals if isinstance(v, (int, float))]
        if valid:
            out["q_mean_cms"] = round(sum(valid) / len(valid), 4)
            out["q_max_cms"] = round(max(valid), 4)
            out["q_min_cms"] = round(min(valid), 4)
            out["n_missing"] = len(vals) - len(valid)
        # Surface the GloFAS snap diagnostics if present (which river cell, area match).
        snap = getattr(df, "attrs", {}).get("glofas_snap")
        if snap:
            out["snap"] = snap
    # Decision trail (observed-vs-modelled, fallback path).
    if getattr(result, "fallback_history", None):
        out["fallback_history"] = result.fallback_history
    return out


def run(artifact_dir: Path) -> None:
    config_path = artifact_dir / "job_config.json"
    if not config_path.exists():
        sys.exit(f"job_config.json not found in {artifact_dir}")

    cfg = json.loads(config_path.read_text())
    job_id: str = cfg["job_id"]
    variable: str = cfg["variable"]
    geometry = cfg["geometry"]
    start: str = cfg["start"]
    end: str = cfg["end"]
    product = cfg.get("product")
    aggregation: str = cfg.get("aggregation", "basin_mean")
    session_id = cfg.get("session_id")
    sessions_dir = cfg.get("sessions_dir")

    logging.basicConfig(
        filename=str(artifact_dir / "fetch.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    _write_status(artifact_dir, job_id, "running", progress={"stage": "fetching"})

    try:
        from aihydro_data import fetch as _fetch

        result = _fetch(
            variable=variable,
            geometry=geometry,
            start=start,
            end=end,
            mode="manual" if product else "auto",
            product=product,
            aggregation=aggregation,  # type: ignore[arg-type]
        )
        compact = _series_to_compact(result, variable)

        # Optionally cache into the research session so downstream tools reuse it.
        if session_id:
            try:
                if sessions_dir:
                    from ai_hydro.session import store as _store
                    _store._SESSIONS_DIR = Path(sessions_dir)
                from ai_hydro.session import HydroSession
                session = HydroSession.load(session_id)
                session._slots["streamflow"] = {  # type: ignore[attr-defined]
                    "data": compact,
                    "meta": {"tool": "data_fetch_async", "source": compact.get("product")},
                }
                session.save()
            except Exception as cache_err:  # caching is best-effort
                log.warning("session cache failed: %s", cache_err)

        _write_status(
            artifact_dir, job_id, "complete",
            progress={"stage": "complete"},
            partial_results=compact,
        )
        log.info("fetch complete: %s via %s (%s rows)",
                 variable, compact.get("product"), compact.get("n_days"))

    except Exception:
        tb = traceback.format_exc()
        log.error("fetch failed:\n%s", tb)
        _write_status(
            artifact_dir, job_id, "failed",
            error={"code": "FETCH_ERROR", "message": tb[-600:]},
        )
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python -m ai_hydro.data.fetch_runner <artifact_dir>")
    run(Path(sys.argv[1]))
