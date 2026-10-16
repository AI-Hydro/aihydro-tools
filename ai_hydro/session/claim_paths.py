"""Write-time handling of local paths in claims (never in identifiers).

A sealed revision cannot be rewritten, so a local path inside one could only ever be withheld wholesale on export (a
privacy stub whose head can no longer be read). The writer therefore never creates one:

1. It replaces, in the claim's prose (statement, rationale, limitations), the *concrete* local prefixes of this
   machine, and nothing else: the session workspace -> ``<workspace>``, AIHYDRO_HOME -> ``<aihydro-home>``, the home
   directory -> ``<home>``, the system temp directory -> ``<tmp>`` (absolute spellings only, resolved and unresolved,
   longest first, at a path boundary; a placeholder is itself a boundary, so the rewrite is a fixpoint).
2. It then runs the exporter's own test (:func:`row_problems`, the predicate behind :func:`find_local_path`) on the
   WHOLE projected revision and refuses (``LOCAL_PATH_IN_TEXT``, field named, nothing recorded) if anything in it, prose
   or identifier, still looks like a local path. Identifiers (evidence source ids, basin ids, claim ids) are never
   rewritten, and any accepted write is therefore never stubbed by the exporter, by construction.

The predicate is a heuristic by design: core's leak pattern, the run-log scrub's reading, case-insensitive home/system
roots, ``~/`` and ``$HOME``. It has known false positives on scientific prose (``HDF /var/streamflow``, ``A:/B ratio``,
``~PET/P``): those are refused and must be rephrased (see docs/evidence-integrity.md). Institutional mounts such as
``/data/...`` are outside it.

The claim projection (``claim_revision_fields``) stays pure, so claims stored before this change keep reading in sync with
their approvals; they are normalised only at their next write.
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
            return tok.strip("\"'(),;").rstrip(".:!?")
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


def row_problems(row: Any, workspace_dir: Optional[str] = None) -> List[Tuple[str, str]]:
    """``[(field path, offending substring)]`` for every string (and dict key) in ``row`` that the exporter would
    withhold: the run-log scrub would change it, or :func:`find_local_path` finds a local path in it."""
    from ai_hydro.session.refs import scrub_paths
    out: List[Tuple[str, str]] = []

    def check(text: str, where: str):
        bad = find_local_path(text)
        if bad is None and scrub_paths(text, workspace_dir) != text:
            bad = text.strip()[:80]
        if bad is not None:
            out.append((where, bad))

    def walk(x, where):
        if isinstance(x, str):
            check(x, where)
        elif isinstance(x, dict):
            for k, v in x.items():
                sub = f"{where}.{k}" if where else str(k)
                if isinstance(k, str):
                    check(k, sub)
                walk(v, sub)
        elif isinstance(x, (list, tuple)):
            for i, v in enumerate(x):
                walk(v, f"{where}[{i}]")

    walk(row, "")
    return out


def row_has_local_path(row: Any, workspace_dir: Optional[str] = None) -> bool:
    """Whether the capsule exporter must withhold this stored row. Same predicate the writer refuses on."""
    return bool(row_problems(row, workspace_dir))


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


def refusal(problems: List[Tuple[str, str]], *, legacy_stored: bool = False) -> dict:
    """The structured teaching error returned when a claim holds a local path that cannot be normalised."""
    field, bad = problems[0]
    bad = bad.rstrip(".,;:!?")
    if field.startswith("evidence_spans") or field.startswith("evidence_versions") or field.endswith("source_id"):
        recovery = ("The evidence source must be an identifier, not a local file path. Cite a run id, a DOI or a named "
                    "dataset as the source instead (source_id), and keep the file's location out of the claim.")
    elif field.startswith("scope"):
        recovery = ("Scope values are names, periods and identifiers, not file paths. Use a basin, gauge or dataset "
                    "name instead of a location on disk.")
    else:
        recovery = ("Rephrase the text without the path, or cite the source through evidence_spans (a run id, DOI or "
                    "named dataset) instead of quoting a location in the text. Example: write 'the 90% interval of "
                    "the baseflow index' rather than naming the file that holds it.")
    if legacy_stored:
        recovery = ("This text was stored earlier and update_claim_status cannot rephrase it. Call add_claim with the "
                    "same claim id and rephrased text (status 'retracted' is accepted there too), then update the "
                    "status.")
    return {"error": True, "code": "LOCAL_PATH_IN_TEXT",
            "message": (f"The {field} contains something that looks like a local file path that could not be "
                        f"normalised: {bad!r}. Nothing was recorded."),
            "field": field, "offending_text": bad, "recovery": recovery}


class LocalPathRefused(Exception):
    """Raised by the writer when the projected revision still holds a local path; carries the refusal dict."""

    def __init__(self, refusal_dict: dict):
        super().__init__(refusal_dict["message"])
        self.refusal = refusal_dict


def report_note(replaced: List[str], remaining: List[str]) -> dict:
    """Neutral tool-result fields (the same in every evaluation arm). A remaining path is refused by the writer, so
    ``remaining`` is not reported for accepted writes."""
    out: dict = {}
    if replaced:
        out["paths_scrubbed"] = replaced
        out["paths_note"] = ("Local filesystem prefixes in these fields were replaced by placeholders "
                             "(<workspace>, <home>, <tmp>) before the claim was recorded.")
    return out
