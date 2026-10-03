"""Stdlib-only verifier for exported claim revision chains.

Mirrors ``aihydro_core.records.claim`` exactly: ``verify_claim_revision_dict``
(``ClaimRevision.from_dict(d).verify()``, constructor validation included) and
``verify_chain``. Digests use ``aihydro.c14n/1`` (tagging + RFC 8785), copied
from ``aihydro_core.records.canonical`` for JSON-native values.

This module must import nothing from ``ai_hydro`` or ``aihydro_core`` so it can
be vendored verbatim into a capsule's standalone ``replay.py``. The two
implementations are held in agreement by shared golden vectors
(``tests/data/claim_chain_vectors.json``, generated from core; see
``tests/test_capsule_claim_records.py``).

Integrity, not origin: a self-contained chain cannot reveal that its tail was
cut, or that its latest rows were rewritten and re-sealed. ``verify_exported``
therefore accepts an ``expected_head`` anchor (a ``revision_digest`` pinned
elsewhere, e.g. the registry row or the capsule manifest).
"""
from __future__ import annotations

import decimal
import hashlib
import math

CLAIM_REVISION_SCHEMA = "aihydro.claim_revision_record/1"
CANONICALIZATION = "aihydro.c14n/1"
ACTOR_KINDS = ("human", "agent", "package", "system")
REDACTED_KEY = "redacted_for_privacy"

_KNOWN_FIELDS = (
    "schema", "canonicalization", "session_id", "claim_id", "revision", "supersedes",
    "revision_digest", "content", "cause", "actor", "recorded_at", "record_digest",
)
_MAX_SAFE_INT = 2 ** 53
_HEX = "0123456789abcdef"


# --------------------------------------------------------------- c14n/1 (JSON-native)
def _encode(value):
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if -_MAX_SAFE_INT <= value <= _MAX_SAFE_INT:
            return value
        return {"$int": str(int(value))}
    if isinstance(value, float):
        if math.isnan(value):
            return {"$float": "nan"}
        if math.isinf(value):
            return {"$float": "inf" if value > 0 else "-inf"}
        return float(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, bool) or not isinstance(key, (str, int)):
                raise TypeError("dict key has no canonical form")
            skey = key if isinstance(key, str) else str(key)
            if skey in out:
                raise TypeError("dict keys collide after stringification")
            out[skey] = _encode(item)
        if any(k.startswith("$") for k in out):
            return {"$map": out}
        return out
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    raise TypeError("cannot encode %s" % type(value).__name__)


def _es_number(x):
    """ECMAScript Number::toString for a finite double (RFC 8785 3.2.2.3)."""
    if x == 0:
        return "0"
    if x < 0:
        return "-" + _es_number(-x)
    sign, digit_tuple, exp = decimal.Decimal(repr(x)).as_tuple()
    digits = "".join(map(str, digit_tuple)).rstrip("0") or "0"
    k = len(digits)
    n = len(digit_tuple) + exp
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digits
    e = n - 1
    exp_str = ("+" if e >= 0 else "-") + str(abs(e))
    if k == 1:
        return digits + "e" + exp_str
    return digits[0] + "." + digits[1:] + "e" + exp_str


_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _es_string(s):
    out = ['"']
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _serialize(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return _es_number(v)
    if isinstance(v, str):
        return _es_string(v)
    if isinstance(v, list):
        return "[" + ",".join(_serialize(i) for i in v) + "]"
    items = sorted(v.items(), key=lambda kv: kv[0].encode("utf-16-be", "surrogatepass"))
    return "{" + ",".join(_es_string(k) + ":" + _serialize(val) for k, val in items) + "}"


def c14n_digest(value):
    """``sha256:<64 hex>`` of the canonical encoding (raises on unencodable input)."""
    return "sha256:" + hashlib.sha256(_serialize(_encode(value)).encode("utf-8")).hexdigest()


def is_digest(value):
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    hexpart = value[7:]
    return len(hexpart) == 64 and all(c in _HEX for c in hexpart)


# --------------------------------------------------------------- ClaimRevision mirror
def _valid(d):
    """The ClaimRevision constructor's checks (after ``from_dict`` fills defaults)."""
    if not d["session_id"] or not d["claim_id"]:
        return False
    revision = d["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return False
    if not is_digest(d["revision_digest"]):
        return False
    if d.get("record_digest") is not None and not is_digest(d["record_digest"]):
        return False
    supersedes = d.get("supersedes")
    if supersedes is not None and not is_digest(supersedes):
        return False
    if revision == 0 and supersedes is not None:
        return False
    if revision > 0 and supersedes is None:
        return False
    if not isinstance(d["content"], dict) or not isinstance(d["cause"], dict):
        return False
    cause = d["cause"]
    if not isinstance(cause.get("tool"), str) or not cause["tool"]:
        return False
    if not isinstance(cause.get("reason"), str) or not cause["reason"]:
        return False
    run_id = cause.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id):
        return False
    actor = d["actor"]
    if not isinstance(actor, dict) or actor.get("kind") not in ACTOR_KINDS or not actor.get("id"):
        return False
    return True


def verify_claim_revision(d):
    """True iff ``d`` is a valid, sealed, unmodified claim revision row.

    Mirrors ``aihydro_core.records.verify_claim_revision_dict`` (never trusts the
    declared digest; any constructor failure is False).
    """
    try:
        if not isinstance(d, dict):
            return False
        # from_dict: known fields present in d; defaults for the rest. recorded_at has
        # a clock default in core, so a row without it cannot match its digest.
        row = {k: d[k] for k in _KNOWN_FIELDS if k in d}
        if "recorded_at" not in row:
            return False
        row.setdefault("schema", CLAIM_REVISION_SCHEMA)
        row.setdefault("canonicalization", CANONICALIZATION)
        for required in ("session_id", "claim_id", "revision", "revision_digest",
                         "content", "cause", "actor"):
            if required not in row:
                return False
        if not _valid(row):
            return False
        if row.get("record_digest") is None:
            return False
        payload = {k: v for k, v in row.items() if v is not None and k != "record_digest"}
        for key, value in d.items():            # unknown fields round-trip into the digest
            if key not in _KNOWN_FIELDS:
                payload.setdefault(key, value)
        return c14n_digest(payload) == row["record_digest"]
    except Exception:
        return False


def verify_chain(rows):
    """True iff ``rows`` is one claim's complete, intact chain (mirrors core).

    Each row verifies, shares ``session_id`` and ``claim_id``, counts
    ``revision`` 0, 1, 2, ... in order, and ``supersedes`` equals the previous
    row's ``revision_digest``. An empty chain is False.
    """
    try:
        if not rows:
            return False
        first = rows[0]
        prev = None
        for i, row in enumerate(rows):
            if not verify_claim_revision(row) or row["revision"] != i:
                return False
            if row["session_id"] != first["session_id"] or row["claim_id"] != first["claim_id"]:
                return False
            if prev is not None and row.get("supersedes") != prev["revision_digest"]:
                return False
            prev = row
        return True
    except Exception:
        return False


# --------------------------------------------------------------- exported-file check
def verify_exported(doc, expected_heads=None):
    """Per-claim verdicts for a ``records/claim_revisions.json`` document.

    Returns ``{claim_id: {"status": ..., "revisions": n, "detail": str}}`` with
    status one of:

    * ``verified``: whole chain verifies (and matches ``expected_heads`` if given);
    * ``partial``: chain links hold and every non-redacted row verifies, but
      privacy-redacted stubs cannot be re-sealed from the capsule;
    * ``corrupt_at_export``: the source store failed its own check at export;
    * ``failed``: a seal, link, ordering or anchor check failed.

    ``expected_heads`` maps ``claim_id -> revision_digest`` pinned outside the
    chain; a tail that was cut or re-sealed then fails instead of verifying.
    Every row's ``session_id`` must equal the document's.
    """
    out = {}
    claims = doc.get("claims") if isinstance(doc, dict) else None
    if not isinstance(claims, dict):
        return out
    heads = expected_heads or {}
    for cid in sorted(claims):
        entry = claims[cid]
        rows = entry.get("rows") if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or entry.get("status") == "corrupt":
            out[cid] = {"status": "corrupt_at_export", "revisions": 0,
                        "detail": "source store failed verification at export; rows withheld"}
            continue
        if not isinstance(rows, list) or not rows:
            out[cid] = {"status": "failed", "revisions": 0, "detail": "no rows"}
            continue
        stubs = [r for r in rows if isinstance(r, dict) and r.get(REDACTED_KEY)]
        foreign = [r.get("revision") for r in rows
                   if not isinstance(r, dict) or r.get("session_id") != doc.get("session_id")]
        if foreign:
            out[cid] = {"status": "failed", "revisions": len(rows),
                        "detail": "row session_id differs from the document's (revision(s) %s)" % foreign}
            continue
        if not stubs:
            ok = verify_chain(rows)
            detail = "chain verifies" if ok else "seal, link or ordering check failed"
        else:
            ok, detail = _verify_with_stubs(rows)
        status = "failed" if not ok else ("partial" if stubs else "verified")
        if ok and cid in heads and rows[-1].get("revision_digest") != heads[cid]:
            status, detail = "failed", "head revision does not match the pinned anchor (tail cut or re-sealed)"
        out[cid] = {"status": status, "revisions": len(rows), "detail": detail}
    return out


def _verify_with_stubs(rows):
    first = rows[0]
    prev = None
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("revision") != i:
            return False, "revision numbering broken at position %d" % i
        if row.get("session_id") != first.get("session_id") or row.get("claim_id") != first.get("claim_id"):
            return False, "mixed session or claim at revision %d" % i
        if not row.get(REDACTED_KEY) and not verify_claim_revision(row):
            return False, "seal failed at revision %d" % i
        if prev is not None and row.get("supersedes") != prev.get("revision_digest"):
            return False, "link broken at revision %d" % i
        if not is_digest(row.get("revision_digest")):
            return False, "bad revision_digest at revision %d" % i
        prev = row
    return True, "links hold; redacted rows cannot be re-sealed from the capsule"
