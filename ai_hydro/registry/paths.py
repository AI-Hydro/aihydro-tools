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
