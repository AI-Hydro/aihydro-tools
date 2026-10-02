"""Place-identity helpers shared by the ledger, skeptic and approval CLI (slice 3, ADR-003).

One USGS site-id rule and one alias-equivalence helper, so a claim scoped by a
``BasinRef`` and a claim scoped by the gauge label are recognised as covering
the same place. aihydro-watershed mints refs; nothing here mints.

Rule (decision O3): a USGS site id is 8-15 digits. The 7-digit legacy form
accepted by ``HydroSession`` warnings (session/store.py, ``site_id_format``)
stays a *label only* there: it is never an identity and never counts as
gauge-shaped for claim scope.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

BASIN_ID_RE = re.compile(r"^aihydro:basin:sha256:[0-9a-f]{64}$")
USGS_SITE_ID_RE = re.compile(r"^\d{8,15}$")
USGS_SITE_ID_SEARCH_RE = re.compile(r"\b(\d{8,15})\b")
BASIN_REF_REQUIRED = "BASIN_REF_REQUIRED"

# Session and claim ids end up inside the `aihydro-approve <session> <claim>` command a
# human is told to run, so they must be safe, shell-inert tokens.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
INVALID_ID = "INVALID_ID"


class InvalidIdError(ValueError):
    code = INVALID_ID

    def __init__(self, kind: str, value: Any, *, stored: bool = False,
                 session_id: Any = None, claim_id: Any = None):
        self.kind, self.value = kind, value
        self.session_id, self.claim_id = session_id, claim_id
        why = ("It was stored before ids were restricted: it stays readable as a label but cannot "
               "be promoted, because the approval command cannot be built safely for it. "
               "Re-add the claim under a new, valid id." if stored else
               "Choose an id matching that pattern.")
        super().__init__(
            f"{kind} {str(value)[:80]!r} is not a valid id: it must match "
            f"{SAFE_ID_RE.pattern} (letters, digits, '.', '_', '-'; at most 128 characters; "
            f"starting with a letter or digit). {why}")

    def to_dict(self) -> dict:
        return {"error": True, "code": INVALID_ID, "kind": self.kind, "message": str(self),
                "session_id": self.session_id, "claim_id": self.claim_id,
                "recovery": f"Use a {self.kind} matching {SAFE_ID_RE.pattern}.",
                "next_tools": ["add_claim"]}


def is_safe_id(value: Any) -> bool:
    return isinstance(value, str) and SAFE_ID_RE.match(value) is not None


def require_safe_id(kind: str, value: Any, *, stored: bool = False) -> str:
    if not is_safe_id(value):
        raise InvalidIdError(kind, value, stored=stored)
    return value


def require_safe_ids(session_id: Any, claim_id: Any, *, stored: bool = False) -> None:
    """Check both ids; the refusal envelope carries both at top level."""
    for kind, value in (("session_id", session_id), ("claim_id", claim_id)):
        if not is_safe_id(value):
            raise InvalidIdError(kind, value, stored=stored, session_id=session_id, claim_id=claim_id)


def is_usgs_site_id(value: Any) -> bool:
    """True iff ``value`` is an 8-15 digit USGS site id string."""
    return isinstance(value, str) and USGS_SITE_ID_RE.match(value.strip()) is not None


def is_basin_id(value: Any) -> bool:
    return isinstance(value, str) and BASIN_ID_RE.match(value) is not None


def usgs_ids_of_ref(ref: Optional[Mapping[str, Any]]) -> set[str]:
    """USGS site ids a BasinRef dict is aliased to (``usgs`` scheme, valid ids only)."""
    out: set[str] = set()
    for alias in (ref or {}).get("aliases") or []:
        if isinstance(alias, Mapping) and alias.get("scheme") == "usgs" and is_usgs_site_id(alias.get("id")):
            out.add(str(alias["id"]).strip())
    return out


def session_basin_ref(session: Any) -> Optional[dict]:
    """The BasinRef dict held by the session's (active) watershed slot, or None."""
    ws = getattr(session, "watershed", None)
    data = (ws or {}).get("data") if isinstance(ws, Mapping) else None
    ref = (data or {}).get("basin_ref") if isinstance(data, Mapping) else None
    return ref if isinstance(ref, Mapping) else None


def retained_refs(session: Any, claim: Optional[Mapping[str, Any]] = None) -> dict[str, dict]:
    """``{id: BasinRef dict}`` of every verified ref the session retains.

    Sources: the ``basin_ref`` of every watershed result in the session (all
    features, not only the active one) and the verified full refs a claim carried
    when it was added (``claim["basin_ref_records"]``). An id that no retained,
    verified ref carries is not retained, whatever it looks like.
    """
    from aihydro_core.records.place import verify_basin_ref_dict
    cands: list[Any] = [session_basin_ref(session)]
    slots = getattr(session, "_slots", None)
    if isinstance(slots, Mapping):
        for by_key in (slots.get("watershed") or {}).values():
            for res in (by_key or {}).values():
                data = res.get("data") if isinstance(res, Mapping) else None
                if isinstance(data, Mapping):
                    cands.append(data.get("basin_ref"))
    if claim:
        cands.extend((claim.get("basin_ref_records") or {}).values())
    out: dict[str, dict] = {}
    for ref in cands:
        if isinstance(ref, Mapping) and is_basin_id(ref.get("id")) and verify_basin_ref_dict(ref):
            out[ref["id"]] = dict(ref)
    return out


def ref_matches_label(ref: Mapping[str, Any], label: str) -> bool:
    """A label names the same place as ``ref``.

    A bare digit label matches only a ``usgs`` alias (COMIDs and other schemes
    look like site ids and must never bind by shape). Other schemes match only
    as an explicit CURIE ``scheme:id``; the ref id matches as itself.
    """
    label = str(label).strip()
    if label == ref.get("id"):
        return True
    if label in usgs_ids_of_ref(ref):
        return True
    if ":" in label:
        scheme, _, ident = label.partition(":")
        return any(isinstance(a, Mapping) and a.get("scheme") == scheme and str(a.get("id")) == ident
                   for a in ref.get("aliases") or [])
    return False


def claim_covered_ids(scope: Mapping[str, Any], refs_by_id: Optional[Mapping[str, Mapping[str, Any]]] = None) -> set[str]:
    """Every label a claim scope covers: ``basins`` labels, the bound ref ids, and the
    ``usgs`` alias ids of each bound ref that is actually retained. An entry's free-text
    label counts only if it genuinely names its ref (``ref_matches_label``).
    """
    covered = {str(b).strip() for b in scope.get("basins") or []}
    for entry in scope.get("basin_refs") or []:
        if not isinstance(entry, Mapping) or not entry.get("id"):
            continue
        covered.add(str(entry["id"]).strip())
        ref = (refs_by_id or {}).get(entry["id"])
        if ref:
            covered |= usgs_ids_of_ref(ref)
            if entry.get("label") and ref_matches_label(ref, entry["label"]):
                covered.add(str(entry["label"]).strip())
    return covered


def gauge_shaped(basins: Iterable[Any], basin_refs: Optional[Iterable[Mapping[str, Any]]] = None,
                 refs_by_id: Optional[Mapping[str, Mapping[str, Any]]] = None) -> bool:
    """True iff the scope is non-empty and every basin is a USGS site id, or is
    bound to a BasinRef carrying a ``usgs`` alias.
    """
    basins = [str(b) for b in basins or []]
    if not basins:
        return False
    bound = list(basin_refs or [])
    ref_dicts = [(refs_by_id or {}).get(e.get("id")) for e in bound if isinstance(e, Mapping)]
    ref_usgs = set().union(*(usgs_ids_of_ref(r) for r in ref_dicts if r)) if ref_dicts else set()
    return all(is_usgs_site_id(b) or b.strip() in ref_usgs for b in basins)


BASIN_REF_UNKNOWN = "BASIN_REF_UNKNOWN"


class BasinRefUnknownError(ValueError):
    """A bound basin id is not a verified ref retained by the session."""
    code = BASIN_REF_UNKNOWN

    def __init__(self, ids: list, session_id: Any = None, claim_id: Any = None):
        self.ids, self.session_id, self.claim_id = list(ids), session_id, claim_id
        super().__init__(
            f"basin_refs {self.ids} do not name a verified BasinRef retained in this session. "
            "Delineate the basin in this session (its basin_ref is then retained), or pass the "
            "full basin_ref dict returned by the delineation tool.")

    def to_dict(self) -> dict:
        return {"error": True, "code": BASIN_REF_UNKNOWN, "session_id": self.session_id,
                "claim_id": self.claim_id, "message": str(self),
                "recovery": "Delineate the basin in this session, then call add_claim again.",
                "next_tools": ["delineate_watershed", "delineate_watershed_from_point", "add_claim"]}


class BasinRefRequiredError(ValueError):
    """Promotion of a basin-scoped claim without ``scope.basin_refs`` (fail closed, O2)."""
    code = BASIN_REF_REQUIRED

    def __init__(self, claim_id: str, basins: list, session_id: Any = None):
        self.claim_id, self.session_id = claim_id, session_id
        super().__init__(
            f"Claim '{claim_id}' is scoped to basins {list(basins)} but carries no basin_refs. "
            "Promotion needs canonical basin identity: run delineate_watershed (or "
            "delineate_watershed_from_point) in this session, then re-add the claim so it "
            "binds to the delineated basin (or pass basin_refs explicitly).")

    def to_dict(self) -> dict:
        return {
            "error": True, "code": BASIN_REF_REQUIRED, "session_id": self.session_id,
            "claim_id": self.claim_id,
            "message": str(self),
            "recovery": "Delineate the basin in this session, then call add_claim again for the "
                        "same claim id; labels are never back-filled with identities.",
            "next_tools": ["delineate_watershed", "delineate_watershed_from_point", "add_claim"],
        }
