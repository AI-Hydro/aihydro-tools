"""Write-time normalisation of local paths in claim prose (never in identifiers).

A claim's text, rationale and limitations are sealed scientific prose. A sealed revision cannot be rewritten, so a
local path inside it can only ever be withheld wholesale on export (a privacy stub whose head can no longer be read).
The writer therefore replaces, before the revision is sealed, the *concrete* local prefixes of this machine and nothing
else:

* the session's workspace directory  -> ``<workspace>``
* AIHYDRO_HOME                       -> ``<aihydro-home>``
* the user's home directory          -> ``<home>``
* the system temp directory          -> ``<tmp>``

(each in its resolved and its unresolved spelling, longest first). There is no "looks like a path" heuristic, so a
scientific string such as ``HDF /var/streamflow``, ``/group/var``, ``A:/B ratio`` or ``mm/day`` is never rewritten
unless it is literally under one of those prefixes. The placeholders are not local paths to core's ``PATH_PATTERN``,
so the crate validates. Anything that still matches that pattern afterwards is *not* rewritten: the revision is
sealed as given and withheld as a stub at export (existing behaviour). Identifiers (claim id, evidence source ids,
basin ids) are never touched.

The function is a fixpoint (``normalise(normalise(x)) == normalise(x)``): placeholders contain no prefix.
The claim projection (``claim_revision_fields``) stays pure and does not scrub, so claims stored before this change
keep reading in sync with their approvals; they are normalised only at their next write.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, List, Optional, Tuple

PROSE_FIELDS = ("claim", "confidence_rationale", "limitations")


_MIN_PREFIX = 4        # "/tmp" is the shortest sane absolute prefix; "/", "/a" and relative names are not prefixes


def _spellings(path: Optional[str]) -> List[str]:
    """Absolute spellings of ``path`` only. A relative ``workspace_dir`` ("results") would otherwise become a word
    prefix and rewrite prose ("The results show" -> "The <workspace> show")."""
    if not path or not isinstance(path, str) or not os.path.isabs(path):
        return []
    out = []
    for p in (path, os.path.realpath(path)):
        p = p.rstrip("/\\")
        if len(p) >= _MIN_PREFIX and os.path.isabs(p) and p not in out:
            out.append(p)
    return out


def concrete_prefixes(workspace_dir: Optional[str] = None) -> List[Tuple[str, str]]:
    """``[(prefix, placeholder), ...]`` longest prefix first (ties: workspace, aihydro-home, home, tmp)."""
    ordered: List[Tuple[str, str]] = []
    sources = [(workspace_dir, "<workspace>"), (os.environ.get("AIHYDRO_HOME"), "<aihydro-home>"),
               (str(Path.home()), "<home>"), (tempfile.gettempdir(), "<tmp>"), ("/tmp", "<tmp>")]
    for raw, token in sources:
        for p in _spellings(raw):
            if p not in {q for q, _t in ordered}:
                ordered.append((p, token))
    return sorted(ordered, key=lambda it: -len(it[0]))        # stable: equal lengths keep the order above


def _compiled(workspace_dir):
    pats = concrete_prefixes(workspace_dir)
    if not pats:
        return None, {}
    table = {p: t for p, t in pats}
    # boundary before: not part of a longer token or path; after: not a longer name (a path separator is fine)
    rx = re.compile(r"(?<![\w./~<>-])(" + "|".join(re.escape(p) for p, _t in pats) + r")(?![\w-]|\.\w)")
    return rx, table


def scrub_claim_text(text: str, workspace_dir: Optional[str] = None) -> str:
    rx, table = _compiled(workspace_dir)
    if rx is None or not isinstance(text, str):
        return text
    return rx.sub(lambda m: table[m.group(1)], text)


# Obvious home / system roots, matched case-insensitively (case-insensitive volumes spell /Users as /users).
# Institutional mounts such as /data, /nfs or /gpfs are NOT in the heuristic: they are ordinary directory names
# that appear in scientific prose, so they are neither detected nor rewritten (documented; sealed as given).
_LOCAL_ROOT_CI = re.compile(
    r"(?i)(?:^|[\s\"'(=:,;\[])/(?:users|home|private|var|tmp|opt|root|mnt|volumes|srv|scratch|etc)/")


def find_local_path(text: Any) -> Optional[str]:
    """The ONE test for "this text holds a local file path": the offending substring, or ``None``.

    Used by ``add_claim`` / ``update_claim_status`` (refusal), by the capsule exporter (what it withholds) and by the
    tool's report, so the three can never disagree. A hit is core's shared leak pattern, the run-log scrub's reading of
    the text, or a case-insensitive home/system root; ``~`` and ``$HOME`` spellings count as home.
    """
    if not isinstance(text, str) or not text:
        return None
    from aihydro_core.export.rocrate import PATH_PATTERN       # core's shared leak pattern
    from ai_hydro.session.refs import scrub_paths

    def hit(t: str) -> bool:
        return (PATH_PATTERN.search(t) is not None or _LOCAL_ROOT_CI.search(t) is not None
                or scrub_paths(t, None) != t)

    for tok in text.split():
        if hit(tok):
            return tok.strip("\"'(),;")
    return text[:60] if hit(text) else None


def _has_local_path(text: Any) -> bool:
    return find_local_path(text) is not None


def strings_of(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings_of(k)
            yield from strings_of(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from strings_of(v)


def row_has_local_path(row: Any, workspace_dir: Optional[str] = None) -> bool:
    """Whether the capsule exporter must withhold this stored row: the run-log scrub would change it, or any string
    in it holds a local path by :func:`find_local_path`. Same predicate the tool reports."""
    from ai_hydro.session.refs import scrub_value
    import json as _json
    changed = _json.dumps(scrub_value(row, workspace_dir), sort_keys=True, default=str) != \
        _json.dumps(row, sort_keys=True, default=str)
    return changed or any(find_local_path(t) for t in strings_of(row))


def normalise_claim(claim: dict, workspace_dir: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """Replace concrete local prefixes in the claim's prose fields, in place.

    Returns ``(replaced_fields, remaining_fields)``: fields changed, and fields (including evidence source ids,
    which are never rewritten) that still hold something core treats as a local path.
    """
    replaced: List[str] = []
    for f in PROSE_FIELDS:
        v = claim.get(f)
        if isinstance(v, str):
            nv = scrub_claim_text(v, workspace_dir)
            if nv != v:
                claim[f] = nv
                replaced.append(f)
        elif isinstance(v, list):
            nv = [scrub_claim_text(x, workspace_dir) if isinstance(x, str) else x for x in v]
            if nv != v:
                claim[f] = nv
                replaced.append(f)
    remaining: List[str] = []
    for f in PROSE_FIELDS:
        v = claim.get(f)
        for x in (v if isinstance(v, list) else [v]):
            if _has_local_path(x):
                remaining.append(f)
                break
    return replaced, remaining


def prose_problems(claim: dict, workspace_dir: Optional[str] = None) -> List[Tuple[str, str]]:
    """``[(field, offending substring)]`` left in the prose after concrete-prefix normalisation, without mutating
    ``claim``. Evidence and other identifiers are not prose and are never inspected here."""
    probe = {k: (list(v) if isinstance(v, list) else v) for k, v in claim.items() if k in PROSE_FIELDS}
    normalise_claim(probe, workspace_dir)
    out = []
    for f in PROSE_FIELDS:
        v = probe.get(f)
        for x in (v if isinstance(v, list) else [v]):
            bad = find_local_path(x)
            if bad:
                out.append((f, bad))
                break
    return out


def refusal(problems: List[Tuple[str, str]]) -> dict:
    """The structured teaching error returned when claim prose holds a path that cannot be normalised."""
    field, bad = problems[0]
    return {"error": True, "code": "LOCAL_PATH_IN_TEXT",
            "message": (f"The {field} contains something that looks like a local file path that could not be "
                        f"normalised: {bad!r}. Nothing was recorded."),
            "field": field, "offending_text": bad,
            "recovery": ("Rephrase the text without the path, or cite the file through evidence (evidence_spans) "
                         "instead of quoting its location in the text. Example: write 'the 90% interval of the "
                         "baseflow index' rather than naming the file that holds it.")}


def report_note(replaced: List[str], remaining: List[str]) -> dict:
    """Neutral tool-result fields (the same in every evaluation arm)."""
    out: dict = {}
    if replaced:
        out["paths_scrubbed"] = replaced
        out["paths_note"] = ("Local filesystem prefixes in these fields were replaced by placeholders "
                             "(<workspace>, <home>, <tmp>) before the claim was recorded.")
    if remaining:
        out["paths_remaining"] = remaining
        out["paths_remaining_note"] = ("These fields still contain a local path that could not be replaced exactly; "
                                       "this revision is recorded as given and withheld from exports.")
    return out
