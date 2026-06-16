"""
BaseJobRunner — shared scaffolding for all AI-Hydro async job runners.

Every heavy-work runner (data_fetch, model training, continental delineation …)
should subclass BaseJobRunner and implement only run_job().  The base class owns:

  - Argument parsing   (reads artifact_dir from sys.argv[1])
  - File-logging setup (logs → <artifact_dir>/<log_name>)
  - Config loading     (reads job_config.json, passes cfg dict to run_job)
  - Status writing     (pending → running → complete | failed)
  - Structured errors  (code / message / recovery in every failure path)

Usage
-----
In your runner module::

    from ai_hydro.mcp.runners.base import BaseJobRunner

    class MyRunner(BaseJobRunner):
        log_name = "my_task.log"

        def run_job(self, cfg: dict) -> dict:
            # Do the actual work.  Raise any exception on failure.
            result = do_expensive_work(cfg["param"])
            # Return a dict of ADDITIONAL fields to merge into status.json.
            return {"my_output": result, "notes": ["used algorithm X"]}

    if __name__ == "__main__":
        MyRunner.main()

The returned dict is merged on top of the base status payload, so you can add
any keys you like.  Standard keys (job_id, status, log_path, error, updated_at)
are managed by the base class and should NOT be included in the return value.

Status JSON written by the base class
--------------------------------------
{
  "job_id":     "<artifact_dir name>",
  "status":     "pending" | "running" | "complete" | "failed",
  "error":      null | {"code": str, "message": str, "recovery": str},
  "log_path":   str,
  "updated_at": "<iso8601>",
  <...keys from run_job() return dict...>
}
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("ai_hydro.mcp.runners.base")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class BaseJobRunner(ABC):
    """Abstract base for AI-Hydro async job runners."""

    #: Override in subclass to name the log file inside the artifact_dir.
    log_name: str = "job.log"

    #: Override in subclass to set the root logger name.
    logger_name: str = "ai_hydro.mcp.runner"

    def _write_status(self, artifact_dir: Path, payload: dict) -> None:
        """Write status.json atomically (best-effort)."""
        try:
            tmp = artifact_dir / "_status.tmp"
            tmp.write_text(json.dumps(payload, indent=2, default=str))
            tmp.replace(artifact_dir / "status.json")
        except Exception as exc:
            log.warning("Failed to write status.json: %s", exc)

    def _base_payload(self, artifact_dir: Path) -> dict:
        return {
            "job_id":     artifact_dir.name,
            "status":     "pending",
            "error":      None,
            "log_path":   str(artifact_dir / self.log_name),
            "updated_at": _now(),
        }

    @abstractmethod
    def run_job(self, cfg: dict, artifact_dir: Path) -> dict:
        """
        Execute the job described by *cfg* (the parsed job_config.json).

        Parameters
        ----------
        cfg          : dict   — the parsed job_config.json
        artifact_dir : Path   — where to write output files (e.g. result.parquet)

        Returns a dict of additional fields to merge into the final status.json.
        Raise any exception on failure — the base class catches it and writes a
        structured "failed" status.
        """

    def _extract_error(self, exc: Exception) -> dict:
        """Build a structured error dict from any exception."""
        code     = getattr(exc, "code", "JOB_FAILED")
        recovery = ""
        if hasattr(exc, "recovery"):
            recovery = str(exc.recovery)
        elif hasattr(exc, "to_dict"):
            recovery = exc.to_dict().get("recovery", "")
        return {
            "code":     code,
            "message":  str(exc)[:600],
            "recovery": recovery,
        }

    def execute(self, artifact_dir: Path) -> None:
        """
        Full lifecycle: setup → config → run_job → status.

        Call this from the runner's __main__ block.
        """
        # ── 1. File logging ───────────────────────────────────────────────────
        log_path = artifact_dir / self.log_name
        logging.basicConfig(
            filename=str(log_path),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        _log = logging.getLogger(self.logger_name)

        base = self._base_payload(artifact_dir)

        # ── 2. Read config ────────────────────────────────────────────────────
        cfg_path = artifact_dir / "job_config.json"
        try:
            cfg = json.loads(cfg_path.read_text())
        except Exception as exc:
            _log.error("Cannot read job_config.json: %s", exc)
            self._write_status(artifact_dir, {
                **base,
                "status": "failed",
                "error": {
                    "code": "CONFIG_READ_FAILED",
                    "message": str(exc)[:400],
                    "recovery": (
                        "The job config file is missing or malformed. "
                        "Resubmit the job via the MCP tool."
                    ),
                },
                "updated_at": _now(),
            })
            return

        # ── 3. Mark running ───────────────────────────────────────────────────
        self._write_status(artifact_dir, {**base, "status": "running", "updated_at": _now()})
        _log.info("Job %s starting with config: %s", base["job_id"], json.dumps(cfg)[:200])

        # ── 4. Execute ────────────────────────────────────────────────────────
        try:
            extra = self.run_job(cfg, artifact_dir)
            if not isinstance(extra, dict):
                extra = {}
        except Exception as exc:
            tb = traceback.format_exc(limit=8)
            _log.error("Job failed: %s\n%s", exc, tb)
            self._write_status(artifact_dir, {
                **base,
                "status": "failed",
                "error":  self._extract_error(exc),
                "updated_at": _now(),
            })
            return

        # ── 5. Mark complete ──────────────────────────────────────────────────
        final = {**base, "status": "complete", "updated_at": _now(), **extra}
        self._write_status(artifact_dir, final)
        _log.info("Job %s complete", base["job_id"])

    @classmethod
    def main(cls) -> None:
        """Entry point: parse sys.argv[1] as artifact_dir and execute."""
        if len(sys.argv) < 2:
            prog = f"python -m {cls.__module__}"
            print(f"Usage: {prog} <artifact_dir>", file=sys.stderr)
            sys.exit(1)
        cls().execute(Path(sys.argv[1]))
