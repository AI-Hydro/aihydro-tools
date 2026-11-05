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


def _is_abs(value: str) -> bool:
    return value.startswith(("/", "~")) or bool(_WIN_ABS.match(value))


def safe_path(value: Any, workspace_root: str | None) -> Any:
    """``value`` made relative to ``workspace_root`` when inside it, ``OUTSIDE_WORKSPACE`` when absolute and
    elsewhere. Relative paths, URLs, empty and non-string values pass through unchanged."""
    if not isinstance(value, str) or not value:
        return value
    raw = value[len("file://"):] if value.lower().startswith("file://") else value
    if not _is_abs(raw):
        return value
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


def _safe_mapping(mapping: dict[str, Any], root: str | None) -> dict[str, Any]:
    return {k: (safe_path(v, root) if k in PATH_KEYS else v) for k, v in mapping.items()}


def safe_layers(layers: Any, workspace_root: str | None) -> list[Any]:
    """Catalog entries with every path field made safe. New objects: the input is not modified."""
    out: list[Any] = []
    for layer in layers if isinstance(layers, list) else []:
        if not isinstance(layer, dict):
            out.append(layer)
            continue
        entry = _safe_mapping(layer, workspace_root)
        snap = entry.get("metadata_snapshot")
        if isinstance(snap, dict):
            entry["metadata_snapshot"] = _safe_mapping(snap, workspace_root)
        out.append(entry)
    return out


_PATH_KEY_SUFFIXES = ("_path", "_dir", "_root", "_folder", "_uri", "Path", "Dir", "Root", "Folder", "Uri")


def _is_path_key(key: Any) -> bool:
    return isinstance(key, str) and (key in PATH_KEYS or key == "path" or key.endswith(_PATH_KEY_SUFFIXES))


def safe_tree(value: Any, workspace_root: str | None) -> Any:
    """A copy of ``value`` (nested dicts and lists, such as a map event) with every path-named field made safe.

    A field is path-named when its key is in ``PATH_KEYS`` or is ``path`` or ends in ``_path``, ``_dir``, ``_root``,
    ``_folder``, ``_uri`` (or the camelCase forms). Other values, GeoJSON included, are copied as they are.
    """
    if isinstance(value, dict):
        return {k: (safe_path(v, workspace_root) if _is_path_key(k) and isinstance(v, str)
                    else safe_tree(v, workspace_root)) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_tree(v, workspace_root) for v in value]
    return value
