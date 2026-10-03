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


def _spellings(path: Optional[str]) -> List[str]:
    if not path or not isinstance(path, str):
        return []
    out = []
    for p in (path, os.path.realpath(path)):
        p = p.rstrip("/\\")
        if len(p) >= 2 and p not in out and p != "/":
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
    rx = re.compile(r"(?<![\w./~<-])(" + "|".join(re.escape(p) for p, _t in pats) + r")(?![\w-]|\.\w)")
    return rx, table


def scrub_claim_text(text: str, workspace_dir: Optional[str] = None) -> str:
    rx, table = _compiled(workspace_dir)
    if rx is None or not isinstance(text, str):
        return text
    return rx.sub(lambda m: table[m.group(1)], text)


def _has_local_path(text: Any) -> bool:
    from aihydro_core.export.rocrate import PATH_PATTERN       # core's shared leak pattern
    return isinstance(text, str) and PATH_PATTERN.search(text) is not None


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
    remaining = [f for f in PROSE_FIELDS
                 if any(_has_local_path(x) for x in (claim.get(f) if isinstance(claim.get(f), list) else [claim.get(f)]))]
    if any(_has_local_path(sp.get("source_id")) for sp in (claim.get("evidence_spans") or []) if isinstance(sp, dict)):
        remaining.append("evidence_spans")
    return replaced, remaining


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
