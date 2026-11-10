"""Seal the run-log rows a background model job writes (DEMO-1 rehearsal, finding 4d; P6).

The MCP server seals every tool call through ``RunRecordMiddleware``: it opens a per-call capture,
the run-log writers note the rows they store, and ``run_records.record_call`` attaches a sealed v2
record (input digest, output digest, parents, environment digest) to each of them. A training job is
a separate process the middleware never sees, so the rows it wrote (the ``propose_and_train`` row from
``post_run`` and the ``model`` slot row from ``put_result``) stayed unsealed and a capsule listed them
under ``rows_without_sealed_record``.

This is the same mechanism, not a new format: the job opens the same capture around the work that
writes the rows and calls the same ``record_call`` when its result is final. The tool name is the one
the job was launched as, the arguments are the job's own inputs (spec or flat parameters, observed-flow
source) and the result is the model result the job reports. Reads of the session's slots
(watershed, streamflow, forcing, camels) become the record's parents automatically, as for any tool.

Every step is best-effort: a recording failure is logged and never fails a finished model.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

log = logging.getLogger("ai_hydro.modelling.job_seal")


class JobSeal:
    """Context for the rows one training job writes; ``seal`` attaches their records."""

    def __init__(self, tool: str, session_id: str, arguments: dict) -> None:
        self.tool = tool
        self.session_id = session_id
        self.arguments = dict(arguments)
        self._started = time.monotonic()
        self._capture: Any = None
        self._token: Any = None
        try:
            from ai_hydro.mcp.enforcement import _generate_unique_run_id
            from ai_hydro.session import run_records

            self._id_factory = _generate_unique_run_id
            self._capture, self._token = run_records.begin_capture(
                tool=tool, arguments=self.arguments, chat_id=None,
                study_id=session_id, id_factory=_generate_unique_run_id)
        except Exception as exc:
            log.warning("job sealing unavailable: %s", exc)

    @property
    def active(self) -> bool:
        return self._capture is not None

    def note(self, **notes: Any) -> None:
        """Facts about the run that go into the record's ``extra`` (needs the capture to be open)."""
        if self._token is None:
            return
        try:
            from ai_hydro.session import run_records

            run_records.declare_lineage(**notes)
        except Exception as exc:
            log.debug("job note skipped: %s", exc)

    def close(self) -> None:
        """Close the capture (idempotent)."""
        if self._token is None:
            return
        try:
            from ai_hydro.session import run_records

            run_records.end_capture(self._token)
        except Exception as exc:
            log.debug("end_capture skipped: %s", exc)
        self._token = None

    def seal(self, result: dict) -> Optional[Any]:
        """Close the capture and seal every row the job wrote. Returns the ``RecordOutcome`` or None."""
        self.close()
        if self._capture is None:
            return None
        try:
            from ai_hydro.session import run_records

            outcome = run_records.record_call(
                tool=self.tool, arguments=self.arguments, result=result, failure=None,
                capture=self._capture, chat_id=None, id_factory=self._id_factory,
                duration_ms=(time.monotonic() - self._started) * 1000.0,
                mcp_client="background_job", context={"study_id": self.session_id, "source": "job"})
            if outcome.record_error:
                log.warning("job record: %s", outcome.record_error)
            return outcome
        except Exception as exc:
            log.warning("job rows were not sealed: %s", exc)
            return None
