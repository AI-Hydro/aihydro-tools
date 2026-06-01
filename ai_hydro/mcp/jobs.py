"""
Re-export shim — jobs block has moved to aihydro-core.

The implementation now lives in aihydro_core.jobs. This shim preserves all
existing imports (``from ai_hydro.mcp.jobs import start_job`` etc.) for one
release cycle. Migrate call-sites to ``from aihydro_core.jobs import ...``
at your own pace; this shim will be removed in a future minor version.

See AGENT_EXECUTION_MODEL.md §3 and AIHYDRO_CORE_DESIGN.md §5.
"""
from aihydro_core.jobs import (  # noqa: F401  (re-export)
    start_job,
    get_job_status,
    get_job_result,
    cancel_job,
    list_jobs,
)

__all__ = [
    "start_job",
    "get_job_status",
    "get_job_result",
    "cancel_job",
    "list_jobs",
]
