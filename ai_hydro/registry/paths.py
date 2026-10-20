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
        # Absolute, so a later os.chdir (mcp_server chdirs into the cache dir) cannot move the home.
        return Path(override).expanduser().absolute()
    return Path.home() / ".aihydro"


def pin_aihydro_home() -> Path:
    """Rewrite a set ``AIHYDRO_HOME`` to its absolute form in ``os.environ``.

    A relative value is re-read on every call, so a later ``os.chdir`` would move the
    home (and child processes would see yet another one). Server entry points that
    change directory call this first. Returns the resolved home.
    """
    home = aihydro_home()
    if os.environ.get(ENV_VAR):
        os.environ[ENV_VAR] = str(home)
    return home


class HomePath(os.PathLike):
    """A path under the AI-Hydro home that is resolved on every use, not at import.

    Module-level constants such as ``store._SESSIONS_DIR`` are patched by tests and
    imported by name elsewhere, so they must stay module attributes; this keeps them
    attributes while still honouring a later ``AIHYDRO_HOME``. ``/`` extends lazily,
    every other ``Path`` method and property is delegated to the path resolved now.
    Assigning a plain ``Path`` over the attribute (a test override) still works.
    """

    __slots__ = ("_parts",)

    def __init__(self, *parts: str) -> None:
        self._parts = parts

    def resolve_now(self) -> Path:
        return aihydro_home().joinpath(*self._parts)

    def __fspath__(self) -> str:
        return os.fspath(self.resolve_now())

    def __truediv__(self, other: "str | os.PathLike[str]") -> "HomePath":
        return HomePath(*self._parts, os.fspath(other))

    def __rtruediv__(self, other: "str | os.PathLike[str]") -> Path:
        return Path(other) / self.resolve_now()

    def __getattr__(self, name: str):
        if name.startswith("_"):        # private/dunder lookups (pickle, copy) must not recurse via _parts
            raise AttributeError(name)
        return getattr(self.resolve_now(), name)

    def __str__(self) -> str:
        return str(self.resolve_now())

    def __repr__(self) -> str:
        return f"HomePath({self.resolve_now()!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, HomePath):
            other = other.resolve_now()
        return self.resolve_now() == other

    def __hash__(self) -> int:
        return hash(self.resolve_now())


def home_path(*parts: str) -> HomePath:
    """``<aihydro_home>/<parts...>`` as a lazily resolved module-level constant."""
    return HomePath(*parts)


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
