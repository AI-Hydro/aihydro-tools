"""Project key for map state: the one place that decides how a directory names its project.

Until the project registry (T-P1) is live, a project is the directory: ``dir:<sha256(realpath)>``
(UX program 2026-11 section 1.1). The host module ``projectIdentity.ts`` computes the same value.
When T-P1 lands, switch ``project_key`` to return the ``prj_...`` id; nothing else changes. Callers
must treat the key as opaque.
"""
from __future__ import annotations

import hashlib
import os


def project_key(workspace: str | os.PathLike | None) -> str | None:
    """``dir:<sha256(realpath(workspace))>``, or None when there is no workspace."""
    if not workspace:
        return None
    return "dir:" + hashlib.sha256(os.path.realpath(os.fspath(workspace)).encode("utf-8")).hexdigest()
