"""
Async data-fetch job runner.

Called as:  python -m ai_hydro.mcp.runners.data_fetch_runner <artifact_dir>

Reads <artifact_dir>/job_config.json, calls aihydro_data.fetch(), serialises
the result to <artifact_dir>/, then writes a final status.json.  Designed to run
detached so the MCP server returns a job_id immediately.

Built on BaseJobRunner — subclasses only implement run_job(); the base class
handles config loading, logging, and status writing.  Before this runner had
180 lines of scaffolding; BaseJobRunner reduces it to the 60 lines of actual
domain logic below.

Status JSON schema
------------------
{
  "job_id":         str,
  "status":         "pending" | "running" | "complete" | "failed",
  "variable":       str | null,
  "product":        str | null,
  "source":         str | null,
  "result_file":    str | null,       # path to parquet/netCDF result
  "result_summary": {...} | null,     # head rows + stats, JSON-safe
  "citation":       str,
  "next_steps":     [...],
  "notes":          [...],
  "error":          {"code": str, "message": str, "recovery": str} | null,
  "log_path":       str,
  "updated_at":     "<iso8601>"
}
"""
from __future__ import annotations

import logging
from pathlib import Path

from ai_hydro.mcp.runners.base import BaseJobRunner

log = logging.getLogger("ai_hydro.mcp.runners.data_fetch_runner")


def _result_summary(result) -> dict:
    """Extract a JSON-safe summary from a FetchResult."""
    try:
        import pandas as pd
        d = result.data
        if isinstance(d, pd.DataFrame):
            return {
                "type":    "DataFrame",
                "rows":    len(d),
                "columns": list(d.columns),
                "head":    d.head(5).to_dict(orient="records"),
            }
    except Exception:
        pass
    return {"type": type(result.data).__name__, "repr": str(result.data)[:400]}


class DataFetchRunner(BaseJobRunner):
    log_name    = "fetch.log"
    logger_name = "ai_hydro.mcp.runners.data_fetch_runner"

    def run_job(self, cfg: dict, artifact_dir: Path) -> dict:
        """Fetch a variable via aihydro_data.fetch() and persist the result."""
        variable    = cfg.get("variable", "")
        geometry    = cfg.get("geometry")
        start       = cfg.get("start", "")
        end         = cfg.get("end", "")
        product     = cfg.get("product")
        aggregation = cfg.get("aggregation", "basin_mean")
        mode        = "manual" if product else "auto"

        log.info(
            "data_fetch: variable=%s start=%s end=%s product=%s",
            variable, start, end, product,
        )

        # ── Fetch ─────────────────────────────────────────────────────────────
        from aihydro_data import fetch as _adata_fetch
        result = _adata_fetch(
            variable=variable,
            geometry=geometry,
            start=start,
            end=end,
            mode=mode,
            product=product,
            aggregation=aggregation,
        )

        # ── Persist result to disk ────────────────────────────────────────────
        import pandas as pd
        result_file = None
        try:
            d = result.data
            if isinstance(d, pd.DataFrame):
                result_file = str(artifact_dir / "result.parquet")
                d.to_parquet(result_file, index=False)
            elif hasattr(d, "to_netcdf"):
                result_file = str(artifact_dir / "result.nc")
                d.to_netcdf(result_file)
            elif hasattr(d, "to_dataset"):
                result_file = str(artifact_dir / "result.nc")
                d.to_dataset(name=variable).to_netcdf(result_file)
            if result_file:
                log.info("Result saved to %s", result_file)
        except Exception as exc:
            log.warning("Could not persist result to disk: %s", exc)

        summary = _result_summary(result)
        log.info(
            "Fetch complete: product=%s source=%s rows=%s",
            result.product, result.source, summary.get("rows", "?"),
        )

        return {
            "variable":       variable,
            "product":        result.product,
            "source":         result.source,
            "result_file":    result_file,
            "result_summary": summary,
            "citation":       result.citation,
            "next_steps":     result.next_steps,
            "notes":          result.notes,
        }


if __name__ == "__main__":
    DataFetchRunner.main()
