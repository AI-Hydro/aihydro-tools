"""Minted identifiers: ULIDs and the ``prj_`` / ``st_`` ids built on them.

The backend mints research ids (the client never does). A ULID is 26 Crockford-base32 characters:
a 48-bit millisecond timestamp then 80 random bits, so ids sort by creation time.
"""
from __future__ import annotations

import os
import re
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

PROJECT_PREFIX = "prj_"
STUDY_PREFIX = "st_"

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
PROJECT_ID_RE = re.compile(rf"^{PROJECT_PREFIX}{_ULID}$")
STUDY_ID_RE = re.compile(rf"^{STUDY_PREFIX}{_ULID}$")


def mint_ulid(now_ms: int | None = None) -> str:
    """A new ULID (uppercase Crockford base32, 26 characters)."""
    ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    value = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


def mint_project_id() -> str:
    return PROJECT_PREFIX + mint_ulid()


def mint_study_id() -> str:
    return STUDY_PREFIX + mint_ulid()


def is_project_id(value: object) -> bool:
    return isinstance(value, str) and bool(PROJECT_ID_RE.match(value))
