"""Location-independent references to files a run retained.

Sealed run records and capsules must not carry absolute local paths: they leak
the user's home directory and mean nothing on another machine. A retained file
is instead referenced by

    ``session-data:<file name>``   a file in the session data directory
                                   (``<session>.data.<name>``), or
    ``workspace:<relative path>``  a file under the session's workspace dir.

The file's digest, recorded next to the ref, stays its identity; the ref is only
a locator. Legacy records that hold an absolute path still resolve (they are
never rewritten: a sealed row is immutable).
"""
from __future__ import annotations

import re
from pathlib import Path

SESSION_DATA = "session-data:"
WORKSPACE = "workspace:"


def is_ref(value: object) -> bool:
    return isinstance(value, str) and value.startswith((SESSION_DATA, WORKSPACE))


def to_ref(path: str | Path, workspace_dir: str | Path | None = None) -> str:
    """Location-independent ref for ``path``; never returns an absolute path.

    Already-a-ref input is returned unchanged. A file under ``workspace_dir``
    becomes ``workspace:<relative>``; anything else is named by its file name
    under ``session-data:`` (the session data dir is flat).
    """
    if is_ref(path):
        return str(path)
    p = Path(path)
    if workspace_dir:
        try:
            rel = p.resolve().relative_to(Path(workspace_dir).resolve())
            return WORKSPACE + rel.as_posix()
        except (ValueError, OSError):
            pass
    return SESSION_DATA + p.name


def ref_name(ref_or_path: str | Path) -> str:
    """Capsule file name for a retained file ("<session>.data.<name>" -> <name>)."""
    s = str(ref_or_path)
    for scheme in (SESSION_DATA, WORKSPACE):
        if s.startswith(scheme):
            s = s[len(scheme):]
            break
    return Path(s).name.split(".data.", 1)[-1]


def resolve_ref(ref: str | Path, session_id: str | None = None,
                workspace_dir: str | Path | None = None) -> Path | None:
    """Local path for a ref (or a legacy absolute path); None if not locatable."""
    s = str(ref)
    if s.startswith(SESSION_DATA):
        from ai_hydro.session import store

        name = Path(s[len(SESSION_DATA):]).name  # no traversal out of the dir
        return store._SESSIONS_DIR / name
    if s.startswith(WORKSPACE):
        ws = workspace_dir
        if ws is None and session_id:
            try:
                from ai_hydro.session.store import HydroSession

                ws = HydroSession.load(session_id).workspace_dir
            except Exception:
                ws = None
        if not ws:
            return None
        root = Path(ws).resolve()
        cand = (root / s[len(WORKSPACE):]).resolve()
        return cand if cand == root or root in cand.parents else None
    return Path(s)  # legacy absolute path


_WIN_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/])")


def _is_abs_str(s: str) -> bool:
    return (s.startswith("/") and len(s) > 1) or bool(_WIN_ABS.match(s))


def _under(s: str, base: Path | str | None) -> str | None:
    """Relative remainder of ``s`` under ``base`` (POSIX or Windows spelling), else None."""
    if base is None:
        return None
    b = str(base).replace("\\", "/").rstrip("/")
    n = s.replace("\\", "/")
    win = bool(_WIN_ABS.match(s)) or bool(_WIN_ABS.match(str(base)))
    if not b:
        return None
    nn, bb = (n.casefold(), b.casefold()) if win else (n, b)
    if nn == bb:
        return ""
    if nn.startswith(bb + "/"):
        return n[len(b) + 1:]
    return None


def _portable_str(s: str, sessions_dir: Path, ws: Path | None, home: Path) -> str:
    if not _is_abs_str(s):
        return s
    rel = _under(s, sessions_dir)
    if rel is not None:
        return SESSION_DATA + rel
    rel = _under(s, ws)
    if rel is not None:
        return WORKSPACE + rel
    rel = _under(s, home)
    if rel is not None:
        return "~/" + rel if rel else "~"
    return s


# Absolute path inside free text: quoted (may contain spaces) or bare.
_TEXT_PATH = re.compile(
    r"""(?P<q>['"])(?P<qp>(?:/|[A-Za-z]:[\\/]|\\\\)[^'"]*)(?P=q)"""
    r"""|(?<![\w:/.\\-])(?P<bp>(?:/[^\s'"<>()\[\],;]+|[A-Za-z]:[\\/][^\s'"<>()\[\],;]*|\\\\[^\s'"<>()\[\],;]+))"""
)


def scrub_paths(text: str, workspace_dir: str | Path | None = None) -> str:
    """Free-text scrub: every absolute path becomes a ref, ``~/...`` or ``<abs>/basename``.

    For text that gets sealed (error messages); the sealed digest must never
    cover a home directory. Session dir -> ``session-data:``, workspace ->
    ``workspace:``, home -> ``~/``, any other absolute path -> ``<abs>/<basename>``.
    """
    from ai_hydro.session import store

    sessions_dir = Path(store._SESSIONS_DIR)
    ws = Path(workspace_dir) if workspace_dir else None
    home = Path.home()

    def one(path: str) -> str:
        out = _portable_str(path, sessions_dir, ws, home)
        if out != path:
            return out
        base = re.split(r"[\\/]+", path.rstrip("\\/"))[-1]
        return "<abs>/" + base if base else "<abs>"

    def sub(m: re.Match) -> str:
        if m.group("q"):
            return m.group("q") + one(m.group("qp")) + m.group("q")
        return one(m.group("bp"))

    return _TEXT_PATH.sub(sub, str(text))


def portable(obj, workspace_dir: str | Path | None = None):
    """Copy of ``obj`` with local absolute paths replaced by refs / ``~/``.

    For exports (capsule ``session.json``): strings under the session data dir
    become ``session-data:``, under the workspace ``workspace:``, under the home
    dir ``~/``. Other strings are untouched. Never mutates ``obj``.
    """
    from ai_hydro.session import store

    sessions_dir = Path(store._SESSIONS_DIR)
    ws = Path(workspace_dir) if workspace_dir else None
    home = Path.home()

    def walk(x):
        if isinstance(x, str):
            return _portable_str(x, sessions_dir, ws, home)
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x

    return walk(obj)
