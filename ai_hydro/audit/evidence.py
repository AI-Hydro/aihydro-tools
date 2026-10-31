"""
Run-log evidence access for the Answer Auditor.

Pure lookups over run-log entries (plain dicts):

  * path aliasing   — ``data.X`` == ``key_outputs.X`` == ``evidence.data.X``
  * hollow runs     — an entry that recorded no outputs cannot back any number
  * declared units  — the unit a run states for a field (``units`` map)
  * leaf scan       — every numeric leaf, for "did you mean this marker" hints

Storage shape of a run-log entry (what the markers resolve against)::

    {
      "run_id": ..., "tool_name": ..., "timestamp": ...,
      "key_outputs": {"<field>": value, ..., "units": {"<field>": "<unit>"}},   # canonical
      "evidence": {"data": {...mirror of key_outputs...},
                   "uncertainty": {"<field>": {"value", "ci_low", "ci_high", "n", "ci_level"}}},
      "inputs": {...tool arguments...},
      "derived_from": [...]                      # reuse records only
    }

The canonical marker path is ``key_outputs.<field>``.  The other spellings are
accepted so that a model that read ``data`` in a tool result is not punished for
the storage layout.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterator

# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #

_DATA_ROOTS = ("evidence.data.", "key_outputs.", "data.")
_UNC_ROOTS = ("evidence.uncertainty.", "uncertainty.", "_uncertainty.", "key_outputs._uncertainty.")
_INPUT_ROOTS = ("inputs.", "params.", "parameters.", "input.", "args.")


def resolve_json_path(obj: Any, path: str) -> tuple[bool, Any]:
    """
    Walk a dotted JSON path into obj.
    Returns (found: bool, value: Any).
    Works on dicts and lists (integer path segments index lists).
    """
    parts = path.split(".")
    cur = obj
    for part in parts:
        if cur is None:
            return False, None
        if isinstance(cur, dict):
            if part not in cur:
                return False, None
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return False, None
        else:
            return False, None
    return True, cur


def candidate_paths(path: str) -> list[str]:
    """The storage paths a marker path may mean, canonical spelling first."""
    path = path.strip()
    out: list[str] = []

    def add(p: str) -> None:
        if p and p not in out:
            out.append(p)

    add(path)  # the literal spelling always wins
    for root in _DATA_ROOTS:
        if path.startswith(root):
            rest = path[len(root):]
            add("key_outputs." + rest)
            add("evidence.data." + rest)
            add("data." + rest)
            return out
    for root in _UNC_ROOTS:
        if path.startswith(root):
            rest = path[len(root):]
            add("evidence.uncertainty." + rest)
            add("key_outputs._uncertainty." + rest)
            return out
    for root in _INPUT_ROOTS:
        if path.startswith(root):
            add("inputs." + path[len(root):])
            return out
    if path.split(".", 1)[0] not in ("key_outputs", "evidence", "inputs", "record", "units"):
        add("key_outputs." + path)
        add("evidence.data." + path)
    return out


def resolve_marker_path(entry: dict, path: str) -> tuple[bool, Any, str]:
    """Resolve ``path`` against a run entry with aliasing -> (found, value, resolved_path)."""
    for cand in candidate_paths(path):
        found, value = resolve_json_path(entry, cand)
        if found:
            return True, value, cand
    return False, None, path


def canonical_path(resolved: str) -> str:
    """Normalise a resolved path to its canonical ``key_outputs.`` / ``evidence.`` spelling."""
    for root in ("evidence.data.", "data."):
        if resolved.startswith(root):
            return "key_outputs." + resolved[len(root):]
    return resolved


def available_fields(entry: dict) -> list[str]:
    """Top-level field names a marker may use (key_outputs plus evidence.data)."""
    names: list[str] = []
    ko = entry.get("key_outputs")
    if isinstance(ko, dict):
        names += [k for k in ko if k != "units"]
    ed = (entry.get("evidence") or {}).get("data") if isinstance(entry.get("evidence"), dict) else None
    if isinstance(ed, dict):
        names += [k for k in ed if k not in names and k != "units"]
    return names


# --------------------------------------------------------------------------- #
# Hollow evidence
# --------------------------------------------------------------------------- #

def is_hollow(entry: Any) -> bool:
    """True when a run recorded no outputs at all (minimal entry, cache-hit stub, error)."""
    if not isinstance(entry, dict):
        return True
    ko = entry.get("key_outputs")
    ev = entry.get("evidence")
    ed = ev.get("data") if isinstance(ev, dict) else None
    return not (isinstance(ko, dict) and ko) and not (isinstance(ed, dict) and ed)


def derived_from(entry: Any) -> list[str]:
    """Run ids a reuse record points at (``derived_from``), if the entry carries any."""
    if not isinstance(entry, dict):
        return []
    raw = entry.get("derived_from")
    if raw is None and isinstance(entry.get("record"), dict):
        raw = entry["record"].get("derived_from")
    if raw is None:
        return []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    ids: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        rid = item.get("run_id") if isinstance(item, dict) else item
        if isinstance(rid, str) and rid:
            ids.append(rid)
    return ids


# --------------------------------------------------------------------------- #
# Declared units
# --------------------------------------------------------------------------- #

def _unit_str(v: Any) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, dict):
        for k in ("unit", "units"):
            if isinstance(v.get(k), str) and v[k].strip():
                return v[k].strip()
    return None


def declared_unit(entry: dict, resolved_path: str) -> str | None:
    """
    The unit the run declares for the field at ``resolved_path``, or None.

    Looks, for field ``f``, in a ``units`` map next to the value (``key_outputs.units[f]``,
    ``evidence.data.units[f]``, ``evidence.units[f]``, top-level ``units[f]``) and in
    ``<f>_units`` / ``<f>_unit`` siblings.  A scalar ``units`` on a run describes only
    its main series, never every field, so it is not used.
    """
    parts = canonical_path(resolved_path).split(".")
    if not parts:
        return None
    fields = [parts[-1]]
    # evidence.uncertainty.<f>.<value|ci_low|ci_high> carries <f>'s unit
    if len(parts) >= 4 and parts[0] == "evidence" and parts[1] == "uncertainty" \
            and parts[-1] in ("value", "ci_low", "ci_high"):
        fields = [parts[2]]
    elif len(parts) >= 4 and parts[0] == "evidence" and parts[1] == "uncertainty":
        return None     # n, ci_level, block_size: counts/params, no physical unit
    ev = entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {}
    ko = entry.get("key_outputs") if isinstance(entry.get("key_outputs"), dict) else {}
    maps = [
        ko.get("units"),
        (ev.get("data") or {}).get("units") if isinstance(ev.get("data"), dict) else None,
        ev.get("units"),
        entry.get("units"),
    ]
    for f in fields:
        for m in maps:
            if isinstance(m, dict) and f in m:
                u = _unit_str(m[f])
                if u:
                    return u
        for sib in (ko, ev.get("data") if isinstance(ev.get("data"), dict) else {}):
            for key in (f"{f}_units", f"{f}_unit"):
                u = _unit_str(sib.get(key)) if isinstance(sib, dict) else None
                if u:
                    return u
    # A signatures row written before the watershed emitted a `units` map has none. Its flows are mm/day,
    # so "2.017 m3/s" must still be flagged: fall back to the signatures table for that row only.
    from ai_hydro.science import signature_units as _su

    rec = entry.get("record") if isinstance(entry.get("record"), dict) else {}
    tool = entry.get("tool_name") or rec.get("tool")
    if _su.is_signatures(slot=entry.get("slot"), tool=tool):
        for f in fields:
            if f in _su.SIGNATURE_UNITS:
                return _su.SIGNATURE_UNITS[f]
    return None


# --------------------------------------------------------------------------- #
# Numeric leaf scan (suggestions)
# --------------------------------------------------------------------------- #

_MAX_LEAVES = 600


def _walk(prefix: str, node: Any, depth: int) -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        if depth >= 4:
            return
        for k, v in node.items():
            if isinstance(k, str) and (k.startswith("_") and k != "_uncertainty"):
                continue
            yield from _walk(f"{prefix}.{k}", v, depth + 1)
    elif isinstance(node, bool):
        return
    elif isinstance(node, (int, float)):
        if isinstance(node, float) and not math.isfinite(node):
            return
        yield prefix, node


def iter_leaves(entry: Any) -> Iterator[tuple[str, Any]]:
    """Numeric leaves of a run entry as (canonical_path, value); key_outputs mirrors are not repeated."""
    if not isinstance(entry, dict):
        return
    ko = entry.get("key_outputs") if isinstance(entry.get("key_outputs"), dict) else {}
    ev = entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {}
    n = 0
    seen: set[str] = set()
    for prefix, node in (
        ("key_outputs", ko),
        ("evidence.data", ev.get("data")),
        ("evidence.uncertainty", ev.get("uncertainty")),
        ("inputs", entry.get("inputs")),
    ):
        if not isinstance(node, dict):
            continue
        for path, val in _walk(prefix, node, 0):
            cp = canonical_path(path)
            if cp in seen:
                continue
            seen.add(cp)
            yield cp, val
            n += 1
            if n >= _MAX_LEAVES:
                return
