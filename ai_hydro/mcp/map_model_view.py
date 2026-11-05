"""What the model is shown of the host map catalog: no absolute paths.

The host catalog (``~/.aihydro/map_layer_catalog.json``) holds absolute paths because the host and
``load_geojson_for_layer`` need them. Those stay as they are on disk and for every internal reader; only the
copy a tool returns to the model goes through here. A path inside the workspace becomes workspace-relative;
one outside it becomes ``OUTSIDE_WORKSPACE``, which names neither the user nor the machine.
"""
from __future__ import annotations

import ntpath
import os
import re
from typing import Any

OUTSIDE_WORKSPACE = "<outside-workspace>"

# Catalog entry fields, and ``metadata_snapshot`` keys, that can hold an absolute path.
PATH_KEYS = (
    "workspace_path", "geojson_cache_path", "raster_path", "inundation_3d_manifest_path", "source_path",
    "workspace_folder", "provenance_path", "display_geojson_path", "source_display_path", "source_uri",
)
_WIN_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_SCHEMES = ("file://", "vscode-file://vscode-app", "vscode-file://", "vscode-remote://", "vscode-userdata:")


def _is_abs(value: str) -> bool:
    return value.startswith(("/", "~", "\\")) or bool(_WIN_ABS.match(value))


def safe_path(value: Any, workspace_root: str | None) -> Any:
    """``value`` made relative to ``workspace_root`` when inside it, ``OUTSIDE_WORKSPACE`` when absolute and
    elsewhere. A relative path that climbs out (``..``) is ``OUTSIDE_WORKSPACE`` too. ``file://`` and
    ``vscode-file://`` URIs are read as the path they name. Other relative paths, ``http(s)`` URLs, empty and
    non-string values pass through unchanged."""
    if not isinstance(value, str) or not value:
        return value
    raw = value
    for scheme in _SCHEMES:
        if raw.lower().startswith(scheme):
            raw = raw[len(scheme):]
            break
    else:
        scheme = ""
    if scheme and not _is_abs(raw):
        return OUTSIDE_WORKSPACE if scheme.startswith(("vscode-remote", "vscode-userdata")) else value
    if not _is_abs(raw):
        parts = re.split(r"[\\/]+", raw)
        return OUTSIDE_WORKSPACE if ".." in parts else value
    if raw.startswith("\\") and not raw.startswith("\\\\"):       # backslash-rooted, no drive: the root is unknown
        return OUTSIDE_WORKSPACE
    if raw.startswith("~"):
        raw = os.path.expanduser(raw)
    if not workspace_root or not _is_abs(workspace_root):
        return OUTSIDE_WORKSPACE
    if _WIN_ABS.match(raw) or _WIN_ABS.match(workspace_root):
        a, b = ntpath.normcase(ntpath.normpath(raw)), ntpath.normcase(ntpath.normpath(workspace_root))
        if a == b:
            return "."
        if a.startswith(b.rstrip("\\") + "\\"):
            return ntpath.relpath(raw, workspace_root).replace("\\", "/")
        return OUTSIDE_WORKSPACE
    root = os.path.normpath(workspace_root)
    path = os.path.normpath(raw)
    if path == root:
        return "."
    if path.startswith(root.rstrip("/") + "/"):
        return os.path.relpath(path, root)
    return OUTSIDE_WORKSPACE


def safe_layers(layers: Any, workspace_root: str | None) -> list[Any]:
    """Catalog entries safe to show the model (``safe_tree`` on each). New objects: the input is not modified."""
    return [safe_tree(layer, workspace_root) for layer in layers] if isinstance(layers, list) else []


_PATH_KEY_SUFFIXES = ("_path", "_dir", "_root", "_folder", "_uri", "_url", "_file",
                      "Path", "Dir", "Root", "Folder", "Uri", "Url", "File", "Paths")
_PATH_KEY_NAMES = frozenset({"path", "paths", "uri", "url", "file", "dir", "folder", "root", "workspace", "cwd"})


def _is_path_key(key: Any) -> bool:
    return isinstance(key, str) and (key in PATH_KEYS or key.lower() in _PATH_KEY_NAMES
                                     or key.endswith(_PATH_KEY_SUFFIXES))


def scrub_text(text: str, workspace_root: str | None) -> str:
    """Free text with local absolute paths replaced (``session.refs.scrub_paths``)."""
    from ai_hydro.session.refs import scrub_paths

    return scrub_paths(text, workspace_root)


def safe_tree(value: Any, workspace_root: str | None, _in_path: bool = False) -> Any:
    """A copy of ``value`` (nested dicts and lists, such as a map event or a tool result) safe to show the model.

    Two passes per string. Under a path-named key (see ``_is_path_key``; the whole subtree below one counts, so a
    list or dict of paths is covered) the string goes through ``safe_path``. Every string, path-keyed or not, then
    goes through ``scrub_paths`` so a path inside a message or a note is replaced as well. Non-strings are copied.
    """
    if isinstance(value, dict):
        return {k: safe_tree(v, workspace_root, _in_path or _is_path_key(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_tree(v, workspace_root, _in_path) for v in value]
    if isinstance(value, str):
        out = safe_path(value, workspace_root) if _in_path else value
        return scrub_text(out, workspace_root) if out not in (OUTSIDE_WORKSPACE, ".") else out
    return value
