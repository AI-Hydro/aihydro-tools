"""Runtime side-effect recorder for the readOnlyHint guard (sys.addaudithook).

An audit hook cannot be removed, so one process-wide hook is installed on first use and only
records while a ``recording()`` block is active. It records the events that mean a tool changed its
environment or reached outside the process: file opens for writing, mkdir, rename/remove/rmdir,
copy/rmtree/truncate/chmod/utime/symlink, tempfile creation, sqlite3 connections, sockets and DNS,
and subprocess/exec/fork. The caller decides which recorded events are allowed.
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
from dataclasses import dataclass

_LOCK = threading.Lock()
_ACTIVE: list["Recorder"] = []
_INSTALLED = False

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

_PATH_EVENTS = {
    "os.mkdir", "os.rename", "os.replace", "os.remove", "os.rmdir", "os.truncate", "os.chmod",
    "os.chown", "os.utime", "os.symlink", "os.link", "shutil.copyfile", "shutil.copymode",
    "shutil.copystat", "shutil.rmtree", "shutil.move", "shutil.copytree", "shutil.make_archive",
    "tempfile.mkstemp", "tempfile.mkdtemp", "sqlite3.connect",
}
_PROCESS_EVENTS = {
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
    "os.forkpty",
}
_NETWORK_EVENTS = {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.bind"}


@dataclass(frozen=True)
class Event:
    kind: str       # "write" | "path" | "process" | "network"
    name: str
    detail: str

    def __str__(self) -> str:
        return f"{self.name}: {self.detail}"


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []


def _hook(event: str, args: tuple) -> None:
    if not _ACTIVE:
        return
    ev: Event | None = None
    if event == "open":
        path, mode, flags = (list(args) + [None, None])[:3]
        mode = mode if isinstance(mode, str) else ""
        flags = flags if isinstance(flags, int) else 0
        if any(c in mode for c in "wax+") or flags & _WRITE_FLAGS:
            ev = Event("write", "open", f"{path!s} mode={mode!r}")
    elif event in _PATH_EVENTS:
        ev = Event("path", event, " ".join(str(a) for a in args[:2]))
    elif event in _PROCESS_EVENTS:
        ev = Event("process", event, str(args[:1]))
    elif event in _NETWORK_EVENTS:
        ev = Event("network", event, str(args[-1:] if event != "socket.getaddrinfo" else args[:2]))
    if ev is not None:
        with _LOCK:
            for rec in _ACTIVE:
                rec.events.append(ev)


@contextlib.contextmanager
def recording():
    global _INSTALLED
    if not _INSTALLED:
        sys.addaudithook(_hook)
        _INSTALLED = True
    rec = Recorder()
    with _LOCK:
        _ACTIVE.append(rec)
    try:
        yield rec
    finally:
        with _LOCK:
            _ACTIVE.remove(rec)
