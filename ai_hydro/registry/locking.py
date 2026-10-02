"""Cross-process advisory lock for read-modify-write file stores.

POSIX: ``fcntl.flock`` (exclusive) on a sidecar lock file. Windows:
``msvcrt.locking`` on the first byte of the same sidecar. If neither module is
available the lock degrades to a no-op with a one-time warning, so concurrent
writers on such a platform are not serialised. The lock is advisory: it
protects cooperating AI-Hydro processes from each other, not from a process
that ignores it.
"""
from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Iterator

log = logging.getLogger("ai_hydro.registry")

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    _fcntl = None
try:  # Windows
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None

_warned = False


@contextlib.contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive cross-process lock on ``lock_path`` for the block."""
    global _warned
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
        elif _msvcrt is not None:  # pragma: no cover
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)
        elif not _warned:  # pragma: no cover
            _warned = True
            log.warning("No file-locking primitive available; registry writes are not serialised.")
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            elif _msvcrt is not None:  # pragma: no cover
                os.lseek(fd, 0, os.SEEK_SET)
                _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)
