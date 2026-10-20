"""
Marker grammar parser for AI-Hydro synthesis prose.

Three marker kinds (see ``Citation Grammar`` in the session research.md / README):

  [run:<run_id>#<json_path>]   — number-level: links a numeric literal to
                                  a field in the session run-log.
  [claim:<claim_id>]           — claim-level: links a sentence-ending assertion
                                  to a ScientificClaim in the session ledger.
  [lit:<tag>]                  — whitelist: legitimately uncited value
                                  (literature, design counts, figure/table refs).

Markers are stripped from prose before display; they are never shown raw.

Token-typed numeric grammar
---------------------------
Numbers are not matched blindly.  The prose is first *tokenised*: digits inside
code spans, URLs, file paths, marker brackets, and any identifier-shaped token
(``run ids``, ``claim ids``, versions, ``EPSG:5070`` ...) are not quantities at
all.  Every remaining number is then given a category:

  exempt         year / date / list index / Figure N / identifier / HUC-EPSG-FIPS-NLCD
                 code / meta-count ("17 signatures") / bare integer with no unit
  sample_size    "7,305 daily observations"        — must be cited (hard)
  method_param   "bootstrap 90% CIs", "seed 42"    — advisory
  quantity       a decimal, a number with a unit, or a number next to a metric
                 name                              — must be cited (hard)

Only the ``sample_size`` and ``quantity`` categories can fail the audit.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Iterator

from .units import unit_after


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


@dataclass(frozen=True)
class ClassifiedNumeric:
    """A numeric literal plus the grammar's verdict on what kind of token it is."""
    span: NumericSpan
    category: str            # exempt | quantity | sample_size | method_param
    reason: str = ""         # for exempt: why (year, identifier, meta_count, ...)
    unit: str | None = None  # raw unit text directly after the number, if any


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
# scientific notation (both ASCII × and Unicode ×, and 1.2e-3), negative numbers
# (ASCII hyphen or U+2212 minus).
#
# Boundaries: a number may not start glued to a preceding token character
# (letter, digit, ``_ . : / #``) — so the digits of ``EPSG:5070``, ``v2.3.7``,
# ``a.5fcfedd8`` or ``1989-10-01``'s tail are never split out.  A sign is a sign
# only when it is not itself glued to a token (``5-10`` is a range, not 5 and -10).
# A number may be followed directly by a short unit (``5mm``) but not by other
# letters/digits.
_NUMERIC_RE = re.compile(
    r"""
    (?<![A-Za-z0-9_.:/#])
    (?:(?<![\w.])[-\u2212])?
    (?:
      \d{1,3}(?:,\d{3})+(?:\.\d+)?   # comma-formatted: 1,247  12,345,678  1,234.5
      | \d+(?:\.\d+)?                 # plain integer or decimal
    )
    (?:[eE][-+]?\d+)?                 # 1.2e-3
    (?:[×x]\s*10\s*[⁻\-]?\s*\d+)?     # 3×10-3
    \s*%?
    (?:
      (?=(?:mm|cm|km|ha|yr|cms|cfs|m|d|h)(?![A-Za-z0-9]))   # attached unit: 5mm
      | (?![A-Za-z0-9])
    )
    """,
    re.VERBOSE,
)

# --- tokenisation masks ----------------------------------------------------
# Each mask is (regex, reason).  Numbers overlapping a mask are not quantities.

# Any marker-shaped bracket, well-formed or not (digits in it are never prose quantities).
_ANY_MARKER_RE_FULL = re.compile(r"\[(?:run|claim|lit):[^\]\n]*\]")
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\")\]]+")
_PATH_RE = re.compile(
    r"(?<![\w)\]])(?:~|\.{1,2})?/(?:[\w.@+\-]+/)*[\w.@+\-]+"      # /abs/or/~/rel/path
    r"|\b[A-Za-z]:\\[^\s]+"                                        # C:\windows\path
    r"|\b[\w.\-]+(?:/[\w.\-]+)+\.[A-Za-z0-9]{1,6}\b"               # dir/file.ext
)
# Any identifier-shaped token: alnum runs joined by . _ : - / #
_MIXED_RE = re.compile(r"[A-Za-z0-9]+(?:[._:\-/#][A-Za-z0-9]+)+")
_COMPOUND_MODIFIER_RE = re.compile(r"^\d+(?:\.\d+)?-[A-Za-z]+$")        # 19-day, 7-day
_SCI_RE = re.compile(r"^\d+(?:\.\d+)?[eE][-+]?\d+$")                    # 1.2e-3
_QTY_WITH_UNIT_RE = re.compile(
    r"^[\d.,\-\u2013]+(?:mm|cm|km|m|ha|yr|d|h|s|cms|cfs|mg|kg|g|l)$", re.IGNORECASE)

_DATE_RES = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"),
    re.compile(r"\b(?:1[7-9]|20|21)\d{2}-(?:0[1-9]|1[0-2])\b(?!-)"),         # 1989-10
    re.compile(r"\b\d{4}/\d{1,2}/\d{1,2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
        r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?"
        r"(?:,?\s+\d{4})?\b"),
    re.compile(
        r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|"
        r"July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
        r"(?:,?\s+\d{4})?\b"),
]
_TIME_RE = re.compile(r"(?<![\d.])\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s?[AaPp][Mm])?(?![\d.])")
_VERSION_RE = re.compile(r"(?<![\w.])v?\d+(?:\.\d+){2,}(?![\w])")

# Geographic / classification codes: the number is a label, not a measurement.
_CODE_RES = [
    re.compile(r"\bHUC[-\s]?(?:\d{1,2})?\s*[=:]?\s*\d+\b", re.IGNORECASE),
    re.compile(r"\bEPSG\s*[:=]?\s*\d+\b", re.IGNORECASE),
    re.compile(r"\bFIPS(?:\s+code)?\s*[:=]?\s*\d+\b", re.IGNORECASE),
    re.compile(r"\bNLCD\s*(?:class|code)?\s*[:=]?\s*\d+\b", re.IGNORECASE),
    re.compile(r"\b(?:COMID|HRU|NHDPlus(?:\s*ID)?)\s*[:=#]?\s*\d+\b", re.IGNORECASE),
]

_STRUCT_BEFORE_RE = re.compile(
    r"(?:\b(?:Figure|Fig|Table|Section|Sec|Eq|Equation|Step|Appendix|Chapter|Panel|Phase|Wave|"
    r"Tier|Stage|Part|Item|Row|Column|Line|Page|pp?|No|Ref|Reference|Task|Request|Issue|"
    r"Finding|Layer|Plate|Supplement|Box|Slide)s?\.?|§|#)\s*[(\[]?$",
    re.IGNORECASE,
)
_LIST_INDEX_RE = re.compile(r"^[ \t]*(?:[-*+•][ \t]+)?\d{1,3}[.)][ \t]")

# Identifiers near a data-source word.
_ID_CONTEXT_RE = re.compile(
    r"\b(?:USGS|gauge|gage|station|site|COMID|HRU|NWIS|NHDPlus|catchment|basin\s+id)\b",
    re.IGNORECASE)

# Word classes for integers.
_META_NOUNS = {
    "attribute", "signature", "tool", "step", "issue", "item", "layer", "file",
    "claim", "run", "option", "panel",
}
_SAMPLE_NOUNS = {
    "day", "observation", "year", "event", "sample", "basin", "gauge", "gage",
    "station", "month",
}
_METHOD_WORDS = {
    "ci", "cis", "confidence", "alpha", "α", "pass", "passes", "block", "blocks",
    "bootstrap", "bootstraps", "bootstrapped", "seed", "seeds", "resample",
    "resamples", "resampling", "iterations", "replicates",
}
_METHOD_DECIMAL_LEAD = {"alpha", "α", "level", "seed", "confidence", "significance", "p"}
_METRIC_RE = re.compile(
    r"\b(?:NSE|KGE|RMSE|MAE|PBIAS|bias|correlation|R2|runoff|baseflow|BFI|elasticity|discharge|"
    r"streamflow|flow|precipitation|precip|evapotranspiration|PET|temperature|area|slope|depth|"
    r"aridity|snow|mean|median|std|variance|percentile|quantile|skill|score|coefficient|"
    r"probability|return\s+period|Q\d{1,3})\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-zα][A-Za-z']*")
_CLAUSE_BREAK_RE = re.compile(r"[,;:()|\n]")


# Patterns that are never flagged as uncited (legacy helper, kept for callers):
#   - 4-digit years (1700–2199)
#   - "Figure N", "Table N", "Section N", "Eq. N", "Step N" directly before the number
_YEAR_RE = re.compile(r"^(?:1[7-9]|20|21)\d{2}$")


def _is_whitelisted_standalone(text_around: str, numeric_raw: str) -> bool:
    """True if the numeric itself is a year or directly follows Figure/Table/Section/Eq./Step.

    The old implementation scanned a +/-60 character window for ANY year or
    Figure reference, so an unrelated nearby "1989" excused "7,305".  The decision
    is now about the token itself.
    """
    raw = numeric_raw.strip()
    if _YEAR_RE.match(raw):
        return True
    for m in re.finditer(re.escape(raw), text_around):
        if _STRUCT_BEFORE_RE.search(text_around[max(0, m.start() - 16):m.start()]):
            return True
    return False


def _normalise_numeric(raw: str) -> str:
    """Strip commas, trailing %, spaces so float() works."""
    s = raw.replace("\u2212", "-").replace(",", "").replace("%", "").replace(" ", "")
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



# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Mask:
    start: int
    end: int
    reason: str


def _mixed_token_is_identifier(tok: str) -> bool:
    """A mixed token masks its digits only if it is identifier-shaped."""
    if not re.search(r"[A-Za-z]", tok):
        return False                      # 1989-10-01, 0.35-0.50: handled elsewhere
    if _COMPOUND_MODIFIER_RE.match(tok):
        return False                      # "19-day": the 19 is still a quantity
    if _SCI_RE.match(tok):
        return False                      # 1.2e-3
    if _QTY_WITH_UNIT_RE.match(tok):
        return False                      # 5-10mm
    return True


def _build_masks(text: str) -> list[_Mask]:
    masks: list[_Mask] = []

    def add(rx: re.Pattern, reason: str) -> None:
        for m in rx.finditer(text):
            if m.end() > m.start():
                masks.append(_Mask(m.start(), m.end(), reason))

    add(_FENCE_RE, "code")
    add(_INLINE_CODE_RE, "code")
    add(_ANY_MARKER_RE_FULL, "marker")
    add(_URL_RE, "url")
    add(_PATH_RE, "path")
    for rx in _CODE_RES:
        add(rx, "code_label")
    for rx in _DATE_RES:
        add(rx, "date")
    add(_TIME_RE, "date")
    add(_VERSION_RE, "version")
    for m in _MIXED_RE.finditer(text):
        if _mixed_token_is_identifier(m.group(0)):
            masks.append(_Mask(m.start(), m.end(), "identifier_token"))
    return masks


def _mask_for(masks: list[_Mask], start: int, end: int) -> _Mask | None:
    for mk in masks:
        if start < mk.end and end > mk.start:
            return mk
    return None


# ---------------------------------------------------------------------------
# Segments: the sentence / bullet / table cell a number lives in
# ---------------------------------------------------------------------------

_SENT_SPLIT_RE = re.compile(
    r"(?<=[.!?])\s+(?!`?\[(?:run|claim|lit):)(?=[A-Z`\[(*\"\u201c])")   # a marker never starts a sentence
_BLOCK_START_RE = re.compile(r"^[ \t]*(?:[-*+•][ \t]+|\d{1,3}[.)][ \t]+|#{1,6}[ \t]|\|)")


def segments(text: str) -> list[tuple[int, int]]:
    """Split prose into (start, end) segments: sentences, list items, table cells.

    A marker binds only inside its segment, so a marker in the next table row or
    the next bullet can no longer be borrowed by the previous row's number.  A
    single newline inside a paragraph is *soft* (wrapped prose keeps one segment).
    """
    out: list[tuple[int, int]] = []
    lines = text.split("\n")
    # group lines into blocks
    pos = 0
    block_lo: int | None = None
    block_is_table = False

    def flush(hi: int) -> None:
        nonlocal block_lo, block_is_table
        if block_lo is None:
            return
        lo = block_lo
        chunk = text[lo:hi]
        if block_is_table:
            cur = lo
            for m in re.finditer(r"\|", chunk):
                if lo + m.start() > cur:
                    out.append((cur, lo + m.start()))
                cur = lo + m.end()
            if cur < hi:
                out.append((cur, hi))
        else:
            cur = lo
            for m in _SENT_SPLIT_RE.finditer(chunk):
                out.append((cur, lo + m.start() + 1))
                cur = lo + m.end()
            out.append((cur, hi))
        block_lo = None
        block_is_table = False

    for line in lines:
        line_lo, line_hi = pos, pos + len(line)
        pos = line_hi + 1
        if not line.strip():
            flush(line_lo)
            continue
        starts_block = bool(_BLOCK_START_RE.match(line))
        is_table = line.lstrip().startswith("|") or line.count("|") >= 2
        if block_lo is None:
            block_lo, block_is_table = line_lo, is_table
        elif starts_block or is_table or block_is_table:
            flush(line_lo)
            block_lo, block_is_table = line_lo, is_table
        # else: soft-wrapped continuation of the current paragraph
    flush(len(text))
    out = [(a, b) for a, b in out if b > a]
    out.sort()
    return out


def segment_of(segs: list[tuple[int, int]], pos: int) -> tuple[int, int]:
    """The segment containing ``pos`` (or the whole span if none does)."""
    i = bisect.bisect_right([a for a, _ in segs], pos) - 1
    if 0 <= i < len(segs) and segs[i][0] <= pos <= segs[i][1]:
        return segs[i]
    return (0, 0) if not segs else (pos, pos)


# ---------------------------------------------------------------------------
# Public extraction
# ---------------------------------------------------------------------------

def _numeric_matches(text: str) -> Iterator[NumericSpan]:
    for m in _NUMERIC_RE.finditer(text):
        raw = m.group(0).strip()
        if not raw:
            continue
        yield NumericSpan(raw=raw, normalised=_normalise_numeric(raw),
                          start=m.start(), end=m.end())


def extract_numerics(text: str) -> list[NumericSpan]:
    """
    Extract numeric literals from prose, skipping tokens that are not numbers:
    anything inside a marker, code span, URL or file path, an identifier-shaped
    token (run/claim ids, versions, EPSG:5070 ...), a date or a HUC/EPSG/FIPS/NLCD
    code.  Years and other classifiable tokens ARE returned; use
    ``classify_numerics`` to see what each one is.
    """
    masks = _build_masks(text)
    return [n for n in _numeric_matches(text) if _mask_for(masks, n.start, n.end) is None]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _digits_only(raw: str) -> str | None:
    d = raw.replace(",", "").strip()
    return d if d.isdigit() else None


def _norm_id(s: str) -> str:
    return str(s).strip().lstrip("0") or "0"


def _is_integer_raw(raw: str) -> bool:
    return re.fullmatch(r"\d{1,3}(?:,\d{3})+|\d+", raw.strip().lstrip("-\u2212")) is not None


def _words_after(text: str, end: int, seg_hi: int, n: int) -> list[str]:
    tail = text[end:min(seg_hi, end + 80)]
    cut = _CLAUSE_BREAK_RE.search(tail)
    if cut:
        tail = tail[:cut.start()]
    tail = re.split(r"[.!?](?:\s|$)", tail)[0]
    return [w.lower() for w in _WORD_RE.findall(tail)][:n]


def _words_before(text: str, start: int, seg_lo: int, n: int) -> list[str]:
    head = text[max(seg_lo, start - 80):start]
    cut = None
    for cut in _CLAUSE_BREAK_RE.finditer(head):
        pass
    if cut:
        head = head[cut.end():]
    return [w.lower() for w in _WORD_RE.findall(head)][-n:]


def _singular(w: str) -> str:
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def classify_numerics(
    text: str,
    known_ids: Iterable[str] = (),
) -> list[ClassifiedNumeric]:
    """
    Classify every numeric token in ``text`` (see module docstring).

    ``known_ids`` are identifiers of the session (site id, gauge ids, canonical ids,
    claim basins): a bare number equal to one of them is a label, not a measurement.
    """
    ids_exact = {str(i).strip() for i in known_ids if str(i).strip()}
    ids_norm = {_norm_id(i) for i in ids_exact if str(i).isdigit() and len(str(i)) >= 5}

    masks = _build_masks(text)
    segs = segments(text)
    out: list[ClassifiedNumeric] = []
    seen_masks: set[int] = set()

    for num in _numeric_matches(text):
        raw = num.raw
        mk = _mask_for(masks, num.start, num.end)
        if mk is not None:
            key = id(mk)
            if key not in seen_masks:
                seen_masks.add(key)
                out.append(ClassifiedNumeric(num, "exempt", mk.reason))
            continue

        seg_lo, seg_hi = segment_of(segs, num.start)
        line_lo = text.rfind("\n", 0, num.start) + 1
        unit = "%" if raw.rstrip().endswith("%") else unit_after(text, num.end)
        # the regex may have swallowed one trailing space; a unit starts right after it
        if unit is None and num.end > 0 and text[num.end - 1:num.end] == " ":
            unit = unit_after(text, num.end - 1)
        is_int = _is_integer_raw(raw)
        digits = _digits_only(raw)

        def done(cat: str, reason: str = "") -> None:
            out.append(ClassifiedNumeric(num, cat, reason, unit))

        # list numbering "1. First step"
        if _LIST_INDEX_RE.match(text[line_lo:num.end + 2]) and text[line_lo:num.start].strip(" \t-*+•") == "":
            done("exempt", "list_index"); continue
        # "Figure 3", "Table 2", "Step 4", "§2", "#7"
        if _STRUCT_BEFORE_RE.search(text[max(0, num.start - 16):num.start]):
            done("exempt", "structural_ref"); continue
        # a year is exempt only as a whole token with no unit / sign / decimals
        if unit is None and _YEAR_RE.match(raw):
            done("exempt", "year"); continue

        # identifiers: the session's own ids, long ids near a data-source word,
        # and zero-padded ids (a quantity does not start with 0 and have 5+ digits)
        if digits is not None:
            if digits in ids_exact or (len(digits) >= 5 and _norm_id(digits) in ids_norm):
                done("exempt", "identifier"); continue
            if 8 <= len(digits) <= 15:
                ctx = text[max(seg_lo, num.start - 40):min(seg_hi, num.end + 25)]
                if _ID_CONTEXT_RE.search(ctx):
                    done("exempt", "identifier"); continue
            if len(digits) >= 5 and digits.startswith("0"):
                done("exempt", "identifier"); continue

        before = _words_before(text, num.start, seg_lo, 2)
        after = _words_after(text, num.end, seg_hi, 2)
        after_sing = [_singular(w) for w in after]

        # meta-counts: 17 signatures, 60 attributes (integer, no unit, closed noun list)
        if is_int and unit is None and any(w in _META_NOUNS for w in after_sing):
            done("exempt", "meta_count"); continue

        # method parameters (bootstrap 90% CIs, alpha = 0.05, seed 42, block 19)
        meth_hit = any(w in _METHOD_WORDS or _singular(w) in _METHOD_WORDS for w in before + after)
        if meth_hit:
            decimal_ok = is_int or raw.rstrip().endswith("%") or any(w in _METHOD_DECIMAL_LEAD for w in before)
            if decimal_ok:
                done("method_param", "method_parameter"); continue

        # sample sizes / record lengths: 7,305 daily observations, 21 years
        if is_int and any(w in _SAMPLE_NOUNS for w in after_sing):
            done("sample_size"); continue

        has_decimal = ("." in raw) or bool(re.search(r"[eE][-+]?\d", raw))
        near_metric = False
        if not has_decimal and unit is None:
            ctx = text[max(seg_lo, num.start - 40):min(seg_hi, num.end + 40)]
            near_metric = _METRIC_RE.search(ctx) is not None
        if has_decimal or unit is not None or near_metric:
            done("quantity"); continue

        done("exempt", "bare_integer")

    return out


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
