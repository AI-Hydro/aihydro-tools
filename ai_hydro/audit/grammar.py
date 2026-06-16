"""
Marker grammar parser for AI-Hydro synthesis prose.

Three marker kinds (see Rules/CITATION_GRAMMAR.md for the full spec):

  [run:<run_id>#<json_path>]   — number-level: links a numeric literal to
                                  a field in the session run-log.
  [claim:<claim_id>]           — claim-level: links a sentence-ending assertion
                                  to a ScientificClaim in the session ledger.
  [lit:<tag>]                  — whitelist: legitimately uncited value
                                  (literature, design counts, figure/table refs).

Markers are stripped from prose before display; they are never shown raw.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterator


class MarkerKind(str, Enum):
    RUN   = "run"
    CLAIM = "claim"
    LIT   = "lit"


@dataclass(frozen=True)
class ParsedMarker:
    kind: MarkerKind
    raw: str          # the full marker string including brackets
    start: int        # character offset of '[' in source text
    end: int          # character offset of ']' + 1

    # run markers
    run_id: str | None = None
    json_path: str | None = None

    # claim markers
    claim_id: str | None = None

    # lit markers
    lit_tag: str | None = None


@dataclass(frozen=True)
class NumericSpan:
    """A numeric literal found in the prose."""
    raw: str          # e.g. "0.82", "7%", "1,247", "3.2×10⁻³"
    normalised: str   # cleaned string ready for float() — no commas, no %
    start: int
    end: int          # exclusive


# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------

# Marker: [run:<run_id>#<json_path>]
# run_id  format: word chars + dots + hyphens (e.g. sigs.20260610.q7.ab3f)
# json_path format: dotted identifiers (e.g. metrics.nse, data.bfi)
_RUN_MARKER_RE = re.compile(
    r"\[run:([A-Za-z0-9._\-]+)#([A-Za-z0-9._\-]+)\]"
)

# Marker: [claim:<claim_id>]  — alphanumeric, hyphens, underscores
_CLAIM_MARKER_RE = re.compile(
    r"\[claim:([A-Za-z0-9_\-]+)\]"
)

# Marker: [lit:<tag>]  — freeform tag (author+year, design, etc.)
_LIT_MARKER_RE = re.compile(
    r"\[lit:([^\]]+)\]"
)

# Any marker (used for stripping)
_ANY_MARKER_RE = re.compile(
    r"\[(run:[^\]]+|claim:[^\]]+|lit:[^\]]+)\]"
)

# Numeric literals: integers, decimals, percentages, comma-thousands,
# scientific notation (both ASCII × and Unicode ×), negative numbers.
# Also matches plain integers preceded by – or - (negative).
# Does NOT match 4-digit standalone years (handled separately as whitelist).
_NUMERIC_RE = re.compile(
    r"""
    (?<![A-Za-z\d])          # not preceded by letter/digit (avoid matching mid-word)
    -?                       # optional sign
    (?:
      \d{1,3}(?:,\d{3})+    # comma-formatted: 1,247  or 12,345,678
      | \d+(?:\.\d+)?        # plain integer or decimal
    )
    (?:[×x]\s*10\s*[⁻\-]?\s*\d+)?  # optional scientific notation suffix
    \s*%?                    # optional percent sign
    (?![A-Za-z\d])           # not followed by letter/digit
    """,
    re.VERBOSE,
)

# Patterns that are never flagged as uncited, regardless of markers:
#   - 4-digit years (1950–2099)
#   - "Figure N", "Table N", "Section N", "Eq. N", "Step N"
#   - standalone integers 0–9 used as counts when near no data context
_WHITELIST_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(19|20)\d{2}\b"),                         # years
    re.compile(r"\b(?:Figure|Table|Section|Eq\.|Step)\s+\d+\b", re.IGNORECASE),
]


def _is_whitelisted_standalone(text_around: str, numeric_raw: str) -> bool:
    """Return True if the numeric in its local context matches a whitelist pattern."""
    for pat in _WHITELIST_PATTERNS:
        if pat.search(text_around):
            return True
    return False


def _normalise_numeric(raw: str) -> str:
    """Strip commas, trailing %, spaces so float() works."""
    s = raw.replace(",", "").replace("%", "").replace(" ", "")
    # Remove unicode multiply prefix for scientific notation cleanup
    s = re.sub(r"[×x].*", "", s)
    return s.strip("-").strip() if s.startswith("-") else s.strip()


def extract_markers(text: str) -> list[ParsedMarker]:
    """
    Extract all recognised markers from prose text.
    Returns them sorted by start position.
    """
    markers: list[ParsedMarker] = []

    for m in _RUN_MARKER_RE.finditer(text):
        markers.append(ParsedMarker(
            kind=MarkerKind.RUN,
            raw=m.group(0),
            start=m.start(),
            end=m.end(),
            run_id=m.group(1),
            json_path=m.group(2),
        ))

    for m in _CLAIM_MARKER_RE.finditer(text):
        markers.append(ParsedMarker(
            kind=MarkerKind.CLAIM,
            raw=m.group(0),
            start=m.start(),
            end=m.end(),
            claim_id=m.group(1),
        ))

    for m in _LIT_MARKER_RE.finditer(text):
        markers.append(ParsedMarker(
            kind=MarkerKind.LIT,
            raw=m.group(0),
            start=m.start(),
            end=m.end(),
            lit_tag=m.group(1),
        ))

    markers.sort(key=lambda x: x.start)
    return markers


def extract_numerics(text: str) -> list[NumericSpan]:
    """
    Extract all numeric literals from prose, skipping those inside markers.
    """
    # Build a set of character ranges that are inside markers (skip them).
    marker_ranges: list[tuple[int, int]] = [
        (m.start(), m.end()) for m in _ANY_MARKER_RE.finditer(text)
    ]

    spans: list[NumericSpan] = []
    for m in _NUMERIC_RE.finditer(text):
        raw = m.group(0).strip()
        if not raw:
            continue
        start, end = m.start(), m.end()
        # Skip if inside a marker
        if any(ms <= start < me for ms, me in marker_ranges):
            continue
        spans.append(NumericSpan(
            raw=raw,
            normalised=_normalise_numeric(raw),
            start=start,
            end=end,
        ))
    return spans


def strip_markers(text: str) -> str:
    """Remove all markers from prose, leaving clean display text."""
    return _ANY_MARKER_RE.sub("", text).strip()


def _context_window(text: str, start: int, end: int, radius: int = 60) -> str:
    """Return a short excerpt centred around [start:end] for error messages."""
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    excerpt = text[lo:hi]
    if lo > 0:
        excerpt = "…" + excerpt
    if hi < len(text):
        excerpt = excerpt + "…"
    return excerpt
