"""AI-Hydro state-directory resolution for the registry and approval stores.

``AIHYDRO_HOME`` overrides the default ``~/.aihydro``. It is read when a path
is *requested*, never at import time, so a test, bench run or CI job that sets
``AIHYDRO_HOME`` after the package is imported is still isolated from the
user's real registry (Slice 1b, ADR-002a).
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "AIHYDRO_HOME"


def aihydro_home() -> Path:
    """Return the AI-Hydro state directory (``$AIHYDRO_HOME`` or ``~/.aihydro``)."""
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".aihydro"


RULES_DIR_NAME = ".aihydrorules"


def rules_dir(workspace_dir: "str | os.PathLike[str] | None" = None) -> Path:
    """Where generated rules/context files (``research.md``, ``tools.md``) live.

    ``<workspace_dir>/.aihydrorules`` when the session has a workspace (the VS Code
    extension reads that directory as user rules), otherwise
    ``<aihydro_home>/.aihydrorules``. Never derived from the location of the code
    checkout: an install must not write beside, or above, its own source tree, and
    a per-run ``AIHYDRO_HOME`` must contain everything the run writes. Resolved at
    call time, like :func:`aihydro_home`.
    """
    if workspace_dir:
        return Path(workspace_dir) / RULES_DIR_NAME
    return aihydro_home() / RULES_DIR_NAME
