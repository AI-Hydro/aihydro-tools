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
import tempfile
import time
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


class LockTimeout(TimeoutError):
    """A bounded lock wait ran out (the lock is held by another thread or process)."""


_warned_unsupported = False


def _is_contended(exc: OSError) -> bool:
    """True only when the error means "someone else holds the lock".

    EAGAIN/EWOULDBLOCK on POSIX; EACCES (and EDEADLK) from ``msvcrt.locking`` on Windows. Anything
    else (ENOLCK, ENOTSUP, EOPNOTSUPP, ENOSYS, EBADF on filesystems with no ``flock``) is not
    contention and must not be reported as a busy study.
    """
    import errno

    codes = {errno.EAGAIN, errno.EWOULDBLOCK}
    if _msvcrt is not None:  # pragma: no cover - Windows
        codes |= {errno.EACCES, errno.EDEADLK}
    return exc.errno in codes


@contextlib.contextmanager
def file_lock_bounded(lock_path: Path, timeout: float) -> Iterator[None]:
    """``file_lock`` that gives up after ``timeout`` seconds with ``LockTimeout``.

    Polls a non-blocking acquire (10 ms steps up to 100 ms), so a holder that never lets go costs
    the waiter a bounded wait instead of a hung thread. When the filesystem cannot lock at all
    (ENOLCK, ENOTSUP, ...) it degrades to no cross-process lock (the caller's in-process lock still
    applies) and warns once; that is never reported as contention.
    """
    global _warned_unsupported
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + max(0.0, float(timeout))
    acquired = False
    try:
        delay = 0.01
        while True:
            try:
                if _fcntl is not None:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                elif _msvcrt is not None:  # pragma: no cover
                    os.lseek(fd, 0, os.SEEK_SET)
                    _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
                else:
                    break                      # no primitive at all: in-process lock only
                acquired = True
                break
            except OSError as exc:
                if not _is_contended(exc):
                    if not _warned_unsupported:
                        _warned_unsupported = True
                        log.warning("File locking is unavailable on this filesystem (%s); %s is "
                                    "serialised within this process only.", exc, lock_path.name)
                    break
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"lock {lock_path.name} not acquired within {timeout:g}s") from None
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                delay = min(delay * 2, 0.1)
        yield
    finally:
        if acquired:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            elif _msvcrt is not None:  # pragma: no cover
                os.lseek(fd, 0, os.SEEK_SET)
                _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        os.close(fd)


def atomic_write_text(path: Path, text: str, prefix: str = ".tmp_") -> None:
    """Write ``text`` to ``path`` atomically: temp file in the same directory, fsync, ``os.replace``.

    A reader sees the old file or the new one, never a half-written file. The caller holds
    ``file_lock`` when the write is the end of a read-modify-write.
    """
    path = Path(path)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=prefix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
