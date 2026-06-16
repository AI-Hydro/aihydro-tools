"""
Async data-fetch MCP tools — background dispatch for slow sources.

data_fetch_background:
    Kick off any aihydro_data.fetch() call as a background job and return
    immediately with a job_id.  Use this instead of data_fetch when the
    backend is known to be slow (e.g. GloFAS / EWDS which queues requests)
    or the date range is large.  Poll with get_job_status / get_data_fetch_result.

get_data_fetch_result:
    Retrieve the completed result for a data_fetch_background job.
    Returns the result summary and path to the persisted parquet/netCDF file.

Design note: this module lives in aihydro-tools (not aihydro-data) because
ai_hydro.mcp.jobs is a tools-layer primitive that cannot be imported by the
data layer without creating an upward dependency edge.  The runner
(ai_hydro.mcp.runners.data_fetch_runner) is the boundary-crossing point —
it lives in tools but calls aihydro_data.fetch() as a subprocess.
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp import jobs
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp.tools_data_async")


def _artifact_dir(job_id: str) -> Path:
    base = Path.home() / ".aihydro" / "data_jobs"
    base.mkdir(parents=True, exist_ok=True)
    return base / job_id


def _pending_status(job_id: str, artifact_dir: Path, variable: str) -> dict:
    return {
        "job_id":   job_id,
        "status":   "pending",
        "variable": variable,
        "product":  None,
        "source":   None,
        "result_file": None,
        "result_summary": None,
        "citation": "",
        "next_steps": [],
        "notes": [],
        "error":    None,
        "log_path": str(artifact_dir / "fetch.log"),
        "updated_at": None,
    }


@mcp.tool()
def data_fetch_background(
    variable: str,
    geometry: Any,
    start: str,
    end: str,
    product: str | None = None,
    aggregation: str = "basin_mean",
) -> dict:
    """
    Kick off a data fetch as a background job and return a job_id immediately.

    Use instead of data_fetch when the backend may queue requests (e.g.
    GloFAS/EWDS for global streamflow) or the date range is large (multi-year
    daily data). The job runs in a detached subprocess; poll its progress with
    get_job_status(job_id) and retrieve the result with get_data_fetch_result(job_id).

    Parameters
    ----------
    variable : str
        Canonical variable name — same as data_fetch. 'streamflow', 'precipitation', etc.
    geometry : any JSON-serialisable form
        (lat, lon) list, [minx, miny, maxx, maxy] bbox, GeoJSON dict, WKT string,
        or a USGS gauge-ID string. Must be JSON-serialisable (no shapely objects).
    start : str
        ISO-8601 start date, e.g. '2000-01-01'.
    end : str
        ISO-8601 end date (inclusive).
    product : str | None
        Pin a specific product ID (e.g. 'GLOFAS_STREAMFLOW'). None = auto-route.
    aggregation : str
        'basin_mean' (default) | 'basin_sum' | 'centroid' | 'raw_raster'.

    Returns
    -------
    dict
        {job_id, status:"pending", variable, poll_with, retrieve_with, log_path,
         started_at, artifact_dir, _note}
    """
    try:
        # Validate geometry is JSON-serialisable (the runner must store it in JSON).
        try:
            json.dumps(geometry)
        except (TypeError, ValueError) as exc:
            return {
                "error": True,
                "code": "GEOMETRY_NOT_SERIALISABLE",
                "message": (
                    f"geometry must be JSON-serialisable for background dispatch: {exc}. "
                    "Pass a (lat, lon) list, a bbox list, a GeoJSON dict, or a WKT string."
                ),
                "recovery": "Convert geometry to a GeoJSON dict or (lat, lon) list first.",
                "next_tools": ["data_validate_request"],
            }

        job_id     = uuid.uuid4().hex[:12]
        art_dir    = _artifact_dir(job_id)
        art_dir.mkdir(parents=True, exist_ok=True)

        config = {
            "job_id":      job_id,
            "variable":    variable,
            "geometry":    geometry,
            "start":       start,
            "end":         end,
            "product":     product,
            "aggregation": aggregation,
        }

        job = jobs.start_job(
            kind="data_fetch",
            runner_module="ai_hydro.mcp.runners.data_fetch_runner",
            config=config,
            artifact_dir=art_dir,
            log_name="fetch.log",
            status_seed=_pending_status(job_id, art_dir, variable),
        )

        return {
            "job_id":        job["job_id"],
            "status":        "pending",
            "variable":      variable,
            "product_hint":  product or "(auto-routed)",
            "artifact_dir":  job["artifact_dir"],
            "log_path":      job["log_path"],
            "started_at":    job["started_at"],
            "wait_with":     f"wait_for_job('{job['job_id']}')",
            "retrieve_with": f"get_data_fetch_result('{job['job_id']}')",
            "_note": (
                "Job dispatched. For GloFAS (EWDS) this typically takes 1–5 min "
                "depending on queue load. Wait for it with wait_for_job('{}') — one "
                "call blocks server-side at zero token cost until it finishes; do NOT "
                "poll in a loop. When done, call get_data_fetch_result() to load the result."
            ).format(job["job_id"]),
        }

    except Exception as exc:
        log.error("data_fetch_background failed: %s", exc)
        return _tool_error_to_dict(exc)


@mcp.tool()
def get_data_fetch_result(job_id: str) -> dict:
    """
    Retrieve the result of a completed data_fetch_background job.

    Returns the result summary (head rows, shape) and the path to the
    persisted parquet / netCDF file for downstream use.

    Parameters
    ----------
    job_id : str
        The job_id returned by data_fetch_background.

    Returns
    -------
    dict
        On complete: {status, variable, product, source, citation, next_steps,
                      result_summary, result_file, notes}.
        On pending/running: {status, job_id, message}.
        On failed: structured error envelope.
    """
    try:
        art_dir = _artifact_dir(job_id)
        status_path = art_dir / "status.json"

        if not status_path.exists():
            # Try the jobs registry
            raw = jobs.get_job_status(job_id)
            return raw if raw else {
                "error": True,
                "code": "JOB_NOT_FOUND",
                "message": f"No data_fetch job found with job_id={job_id!r}.",
                "recovery": "Check job_id or call list_jobs() to see recent jobs.",
                "next_tools": ["list_jobs"],
            }

        status = json.loads(status_path.read_text())
        s = status.get("status")

        if s in ("pending", "running"):
            return {
                "status": s,
                "job_id": job_id,
                "variable": status.get("variable"),
                "message": "Job still running. Poll again in 30–60 s.",
                "poll_with": f"get_data_fetch_result('{job_id}')",
            }

        if s == "failed":
            err = status.get("error") or {}
            return {
                "error": True,
                "code": err.get("code", "FETCH_FAILED"),
                "message": err.get("message", "Data fetch job failed."),
                "recovery": err.get("recovery", "Check log_path for details."),
                "log_path": status.get("log_path"),
                "next_tools": ["data_validate_request", "data_doctor"],
            }

        # complete
        return {
            "status":         "complete",
            "job_id":         job_id,
            "variable":       status.get("variable"),
            "product":        status.get("product"),
            "source":         status.get("source"),
            "citation":       status.get("citation", ""),
            "next_steps":     status.get("next_steps", []),
            "notes":          status.get("notes", []),
            "result_summary": status.get("result_summary"),
            "result_file":    status.get("result_file"),
            "_note": (
                "Full result saved to result_file (parquet/netCDF). "
                "Load with pd.read_parquet(result_file) or xr.open_dataset(result_file)."
            ),
        }

    except Exception as exc:
        log.error("get_data_fetch_result failed: %s", exc)
        return _tool_error_to_dict(exc)
