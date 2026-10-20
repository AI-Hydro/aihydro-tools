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

# Marker: [lit:<tag>]  — one token: author+year, design tag, passage hash.
# No whitespace: "[lit:fake 0.61]" is malformed, not a tag.
_LIT_MARKER_RE = re.compile(
    r"\[lit:([A-Za-z0-9][\w\-:.+/]{2,})\]"
)

# Any marker-shaped opener, well-formed or not.
_MARKER_OPEN_RE = re.compile(r"\[(?:run|claim|lit):")

# Any marker (used for stripping)
_ANY_MARKER_RE = re.compile(
    r"\[(run:[^\]]+|claim:[^\]]+|lit:[^\]]+)\]"
)

# Numeric literals: integers, decimals (also ".61"), percentages, comma-thousands,
# scientific notation (1.2e-3 and 3.2×10⁻³ / 3.2x10^-3), negative numbers (ASCII
# hyphen or U+2212 minus).
#
# Boundaries: a number may not start glued to a letter/digit (so ``Q95``, ``m3``,
# ``R2``, ``HUC8`` are names, not quantities) or to ``^`` (an exponent).  ``_ * ~ : / - .``
# before a number do NOT glue it: ``NSE:0.61``, ``KGE-0.61``, ``_0.61_``, ``~774km2``
# are all quantities.  Identifier-shaped tokens are masked separately (see
# ``_mixed_token_is_identifier``), never by a blanket "contains a separator" rule.
# A sign is a sign only when not itself glued to a token (``5-10`` is a range).
# A number may be followed directly by a short unit (``5mm``) but not other letters/digits.
_SUP = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_NUMERIC_RE = re.compile(
    r"""
    (?:(?<![A-Za-z0-9^])|(?<=\b[vV]))    # "v0.61" in prose is a number; "Q95", "m3" are names
    (?:(?<![\w.])[-\u2212])?
    (?:
      \d+(?:\.\d+)?\s?\^\s?[-\u2212]?\d+   # power form 10^3
      | \d{1,3}(?:,\d{3})+(?:\.\d+)?   # comma-formatted: 1,247  12,345,678  1,234.5
      | \d+(?:\.\d+)?                 # plain integer or decimal
      | \.\d+                         # .61
    )
    (?:[eE][-+]?\d+)?                 # 1.2e-3
    (?:\s?[×x*\u00b7]\s*10\s*(?:\^\s*)?[⁻\-\u2212]?\s*[\d⁰¹²³⁴⁵⁶⁷⁸⁹]+)?   # 3.2×10⁻³  3.2x10^-3
    \s*%?
    (?:
      (?=(?:mm|cm|km|ha|yr|cms|cfs|m|d|h)(?:[23\u00b2\u00b3]|/[A-Za-z]+)?(?![A-Za-z0-9]))   # attached unit: 5mm 412.5m3/s 774km2
      | (?![A-Za-z0-9])
    )
    """,
    re.VERBOSE,
)

_SUP_MAP = {c: str(i) for i, c in enumerate(_SUP)}


def parse_numeric(raw: str) -> float | None:
    """float value of a prose numeric literal (commas, %, unicode minus, ×10^n, e-notation)."""
    t = raw.strip().replace("\u2212", "-").replace(",", "").replace(" ", "")
    t = t.rstrip("%")
    exp = 0
    pw = re.fullmatch(r"(-?[\d.]+)\^(-?\d+)", t.replace("\u2212", "-"))
    if pw:
        try:
            return float(pw.group(1)) ** int(pw.group(2))
        except (ValueError, OverflowError, ZeroDivisionError):
            return None
    m = re.search(r"[×x*\u00b7]10\^?([-⁻\u2212]?)([\d⁰¹²³⁴⁵⁶⁷⁸⁹]+)$", t)
    if m:
        digits = "".join(_SUP_MAP.get(c, c) for c in m.group(2))
        exp = int(digits) * (-1 if m.group(1) else 1)
        t = t[:m.start()]
    try:
        return float(t) * (10.0 ** exp)
    except ValueError:
        return None


def mantissa_ulp(raw: str) -> float:
    """Unit in the last place of the printed number (0.61 -> 0.01, 3.2×10⁻³ -> 1e-4)."""
    t = raw.strip().replace("\u2212", "-").replace(",", "").replace(" ", "").rstrip("%")
    exp = 0
    if re.fullmatch(r"-?[\d.]+\^-?\d+", t):
        return 1.0                       # a power is exact to its own precision
    m = re.search(r"[×x*\u00b7]10\^?([-⁻\u2212]?)([\d⁰¹²³⁴⁵⁶⁷⁸⁹]+)$", t)
    if m:
        digits = "".join(_SUP_MAP.get(c, c) for c in m.group(2))
        exp = int(digits) * (-1 if m.group(1) else 1)
        t = t[:m.start()]
    me = re.fullmatch(r"(.*?)[eE]([-+]?\d+)", t)
    if me:
        t, exp = me.group(1), exp + int(me.group(2))
    mant = t
    dec = len(mant.split(".")[1]) if "." in mant else 0
    return 10.0 ** (exp - dec)


# --- tokenisation masks ----------------------------------------------------
# Each mask is (regex, reason).  Numbers overlapping a mask are not quantities.

_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\")\]]+")
_PATH_RE = re.compile(
    r"(?<![\w)\]])(?:~|\.{1,2})?/(?=[\w.@+\-/]*[A-Za-z])(?:[\w.@+\-]+/)*[\w.@+\-]+"   # /abs/or/~/rel/path
    r"|\b[A-Za-z]:\\[^\s]+"                                                          # C:\windows\path
    r"|\b[\w.\-]+(?:/[\w.\-]+)+\.(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{1,6}\b"         # dir/file.ext
)
# Candidate identifier tokens: alnum runs joined by . _ : - / #
_MIXED_RE = re.compile(r"[A-Za-z0-9]+(?:[._:\-/#][A-Za-z0-9]+)+")
_HEX_TAIL_RE = re.compile(r"(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

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
# Versions only in a version context: v1.2.3 (semver) or "version 1.2". Bare "0.61.5" is a number.
_UNIT_EXP_RE = re.compile(r"(?<=[A-Za-z])-[123](?![\d.])")      # the -1 of "m3 s-1", "mm d-1"
_VERSION_RE = re.compile(
    r"(?<![\w.])v\d+(?:\.\d+){2,}(?![\w.])"
    r"|\b(?:version|release)\s+v?\d+(?:\.\d+)+(?![\w.])", re.IGNORECASE)

# Geographic / classification codes: the number is a label, not a measurement.
_CODE_RES = [
    re.compile(r"\bHUC[-\s]?(?:\d{1,2})?\s*[=:]?\s*\d+(?![.\d%])", re.IGNORECASE),
    re.compile(r"\bEPSG\s*[:=]?\s*\d+(?![.\d%])", re.IGNORECASE),
    re.compile(r"\bFIPS(?:\s+code)?\s*[:=]?\s*\d+(?![.\d%])", re.IGNORECASE),
    re.compile(r"\bNLCD\s*(?:class|code)?\s*[:=]?\s*\d+(?![.\d%])", re.IGNORECASE),
    re.compile(r"\b(?:COMID|HRU|NHDPlus(?:\s*ID)?)\s*[:=#]?\s*\d+(?![.\d%])", re.IGNORECASE),
]

_STRUCT_BEFORE_RE = re.compile(
    r"(?:\b(?:Figure|Fig|Table|Section|Sec|Eq|Equation|Step|Appendix|Chapter|Panel|Phase|Wave|"
    r"Tier|Stage|Part|Item|Row|Column|Line|Page|pp?|No|Ref|Reference|Task|Request|Issue|"
    r"Finding|Layer|Plate|Supplement|Box|Slide)s?\.?|§|#)\s*[(\[]?$",
    re.IGNORECASE,
)
_LIST_INDEX_RE = re.compile(r"^[ \t]*(?:[-*+•][ \t]+)?\d{1,3}[.)][ \t]")

# A long integer is an identifier only when a data-source word sits right beside it
# ("USGS 01031500", "gauge id 04085427", "COMID 22678241").
_ID_KEYWORDS = {"usgs", "gauge", "gage", "station", "site", "comid", "hru", "nwis", "nhdplus", "catchment"}

# Word classes for integers.
_META_NOUNS = {
    "attribute", "signature", "tool", "step", "issue", "item", "layer", "file",
    "claim", "run", "option", "panel",
}
_SAMPLE_NOUNS = {
    "yr", "yrs", "day", "observation", "year", "event", "sample", "basin", "gauge", "gage",
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
    r"probability|return\s+period|Q\d{1,3}|elevation|elev|altitude|peak|drainage|length|volume|"
    r"yield|load|concentration|rainfall|storage|recharge|height|gradient|temp|width|stage|"
    r"velocity|duration|frequency|recurrence|discharges)\b",
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
    """
    A mixed token (``a.b-c``) hides its digits only if it has a real id shape:

      * 3+ segments with an 8+ digit segment (run/claim ids: ``sigs.20261020.01031500.dd4ed1e7``)
      * a hex tail of 8+ chars mixing digits and letters (``watershed.ce56bc643d4c``)
      * a UUID

    Anything else (``NSE:0.61``, ``KGE-0.61``, ``site_0.61``, ``bias-12.5%``, ``Q95-8.2``)
    is prose: its number is extracted and audited.
    """
    if not re.search(r"[A-Za-z]", tok):
        return False                      # 1989-10-01, 0.35-0.50: handled elsewhere
    if _UUID_RE.fullmatch(tok):
        return True
    segs = [x for x in re.split(r"[._:\-/#]", tok) if x]
    if len(segs) >= 3 and any(re.fullmatch(r"\d{8,}", x) for x in segs):
        return True
    if len(segs) >= 2 and _HEX_TAIL_RE.fullmatch(segs[-1].lower()):
        return True
    return False


def _strict_marker_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for rx in (_RUN_MARKER_RE, _CLAIM_MARKER_RE, _LIT_MARKER_RE):
        spans += [(m.start(), m.end()) for m in rx.finditer(text)]
    return sorted(spans)


def find_malformed_markers(text: str) -> list[tuple[int, int, str]]:
    """
    Marker-shaped brackets (``[run:``, ``[claim:``, ``[lit:``) that fail the strict
    grammar: ``[run:r1#key_outputs.nse 0.61]``, ``[lit:fake 0.61]``, ``[run:foo 0.61``.
    Returns (start, end, raw).
    """
    good = {a for a, _ in _strict_marker_spans(text)}
    out = []
    for m in _MARKER_OPEN_RE.finditer(text):
        if m.start() in good:
            continue
        close = text.find("]", m.start())
        nl = text.find("\n", m.start())
        end = close + 1 if close != -1 and (nl == -1 or close < nl) else min(
            len(text), m.start() + 40 if nl == -1 else min(nl, m.start() + 40))
        out.append((m.start(), end, text[m.start():end]))
    return out


def _code_span_hides_quantity(chunk: str) -> bool:
    """A code span with a decimal, a percent or a unit-bearing number is prose, not code."""
    for m in _NUMERIC_RE.finditer(chunk):
        raw = m.group(0).strip()
        if "." in raw or raw.endswith("%") or unit_after(chunk, m.end()) or \
                (m.end() > 0 and chunk[m.end() - 1:m.end()] == " " and unit_after(chunk, m.end() - 1)):
            return True
    return False


def _build_masks(text: str) -> list[_Mask]:
    masks: list[_Mask] = []

    def add(rx: re.Pattern, reason: str) -> None:
        for m in rx.finditer(text):
            if m.end() > m.start():
                masks.append(_Mask(m.start(), m.end(), reason))

    for rx in (_FENCE_RE, _INLINE_CODE_RE):
        for m in rx.finditer(text):
            if not _code_span_hides_quantity(m.group(0)):
                masks.append(_Mask(m.start(), m.end(), "code"))
    for a, b in _strict_marker_spans(text):
        masks.append(_Mask(a, b, "marker"))
    add(_URL_RE, "url")
    add(_PATH_RE, "path")
    for rx in _CODE_RES:
        add(rx, "code_label")
    for rx in _DATE_RES:
        add(rx, "date")
    add(_TIME_RE, "date")
    add(_VERSION_RE, "version")
    for m in _UNIT_EXP_RE.finditer(text):
        if re.search(r"(?:\b[smdhyL]|yr|ha|m[23]?)$", text[max(0, m.start() - 3):m.start()]):
            masks.append(_Mask(m.start(), m.end(), "unit_exponent"))
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
    return re.fullmatch(r"\d{1,3}(?:,\d{3})+|\d+", raw.strip().lstrip("-−")) is not None


def _blank_markers(text: str) -> str:
    """Text with every marker-shaped bracket replaced by spaces (same offsets).

    Word windows must never read a marker's own text: ``[run:r1#q95]`` would otherwise
    contribute the word "run" (a meta-count noun) next to the number it cites.
    """
    out = list(text)
    for m in re.finditer(r"\[(?:run|claim|lit):[^\]\n]*\]?", text):
        for i in range(m.start(), m.end()):
            out[i] = " "
    return "".join(out)


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


_PERIOD_PREPS = {"in", "of", "during", "since", "until", "from", "by", "on", "for", "to", "through",
                 "before", "after", "between", "and", "year", "yr", "wy"}


def _table_header_context(text: str) -> list[tuple[int, int, str]]:
    """
    For every body cell of a markdown table, the text of its column header:
    returns (cell_lo, cell_hi, header_text).  ``| Area (km²) | 774 |`` -> the 774 cell
    inherits "Area (km²)" (a metric word and a unit).
    """
    out: list[tuple[int, int, str]] = []
    pos = 0
    group: list[tuple[int, str]] = []          # (line_start, line) of consecutive table rows

    def flush() -> None:
        if not group:
            return
        def cells(ls: int, line: str) -> list[tuple[int, int, str]]:
            res, cur = [], 0
            idx = [i for i, c in enumerate(line) if c == "|"]
            bounds = [-1] + idx + ([len(line)] if not line.rstrip().endswith("|") else [])
            for a, b in zip(bounds, bounds[1:]):
                if a + 1 <= b:
                    res.append((ls + a + 1, ls + b, line[a + 1:b]))
            if res and idx and not line[:idx[0]].strip():
                res = res[1:]                      # text before the leading pipe: not a cell
            return res
        header = cells(*group[0]) if len(group) >= 2 else []
        body = group[1:] if len(group) >= 2 else group
        for ls, line in body:
            if re.fullmatch(r"[\s|:\-]+", line):
                continue
            row = cells(ls, line)
            for k, (lo, hi, _t) in enumerate(row):
                head = header[k][2] if k < len(header) else ""
                label = row[k - 1][2] if k >= 1 else ""
                out.append((lo, hi, f"{head} {label}"))
        group.clear()

    for line in text.split("\n"):
        if line.lstrip().startswith("|") and line.count("|") >= 2:
            group.append((pos, line))
        else:
            flush()
        pos += len(line) + 1
    flush()
    return out


def _header_unit(header: str) -> str | None:
    """Unit named in a table header's brackets: ``Area (km²)`` -> ``km²``."""
    for m in re.finditer(r"[(\[]([^)\]]+)[)\]]", header):
        inner = m.group(1).strip()
        if unit_after("0 " + inner, 1):
            return inner
    return None


def classify_numerics(
    text: str,
    known_ids: Iterable[str] = (),
) -> list[ClassifiedNumeric]:
    """
    Classify every numeric token in ``text`` (see module docstring).

    ``known_ids`` must be the session's OWN identifiers only (session id, site /
    canonical id): never ids a model can write into claims or run outputs.  Even those
    exempt a bare integer (>= 5 digits, no unit) and nothing else.
    """
    ids_exact = {str(i).strip() for i in known_ids if str(i).strip().isdigit() and len(str(i).strip()) >= 5}
    ids_norm = {_norm_id(i) for i in ids_exact}

    masks = _build_masks(text)
    segs = segments(text)
    wtext = _blank_markers(text)
    tables = _table_header_context(text)
    marker_starts = [m.start for m in extract_markers(text) if m.kind != MarkerKind.CLAIM]
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
        unit = "%" if raw.rstrip().endswith("%") else unit_after(wtext, num.end)
        # the regex may have swallowed one trailing space; a unit starts right after it
        if unit is None and num.end > 0 and wtext[num.end - 1:num.end] == " ":
            unit = unit_after(wtext, num.end - 1)
        hdr_metric = False
        for lo, hi, htxt in tables:
            if lo <= num.start < hi:
                if unit is None:
                    unit = _header_unit(htxt)
                hdr_metric = _METRIC_RE.search(htxt) is not None
                break
        is_int = _is_integer_raw(raw)
        digits = _digits_only(raw)
        before = _words_before(wtext, num.start, seg_lo, 3)
        after = _words_after(wtext, num.end, seg_hi, 2)
        after_sing = [_singular(w) for w in after]
        seg_text = wtext[seg_lo:seg_hi]
        seg_has_marker = any(seg_lo <= ms < seg_hi for ms in marker_starts)
        seg_has_metric = hdr_metric or _METRIC_RE.search(seg_text) is not None

        def done(cat: str, reason: str = "") -> None:
            out.append(ClassifiedNumeric(num, cat, reason, unit))

        # list numbering "1. First step"
        if _LIST_INDEX_RE.match(text[line_lo:num.end + 2]) and text[line_lo:num.start].strip(" \t-*+•") == "":
            done("exempt", "list_index"); continue
        # "Figure 3", "Table 2", "Step 4", "§2", "#7"
        if _STRUCT_BEFORE_RE.search(text[max(0, num.start - 16):num.start]):
            done("exempt", "structural_ref"); continue
        # a year is exempt only as a whole token: no unit/sign/decimals, and not the value of a
        # metric ("flow 1990" is a quantity, "flow in 2019" is a period)
        if unit is None and _YEAR_RE.match(raw):
            prev = before[-1] if before else ""
            metric_adjacent = (prev not in _PERIOD_PREPS) and any(
                _METRIC_RE.search(w) for w in before[-3:]) or hdr_metric
            if not metric_adjacent:
                done("exempt", "year"); continue

        # identifiers.  Never ids that the model can write; only (a) the session's own id and
        # (b) 8-15 digit integers directly beside USGS/gauge/station/COMID/HRU.  Both require
        # no unit and 5+ digits: "12345678 cfs at USGS" is a flow, not an id.
        if digits is not None and unit is None and len(digits) >= 5:
            if digits in ids_exact or _norm_id(digits) in ids_norm:
                done("exempt", "identifier"); continue
            if 8 <= len(digits) <= 15 and (
                any(w in _ID_KEYWORDS for w in before[-2:]) or (after and after[0] in _ID_KEYWORDS)
            ):
                done("exempt", "identifier"); continue

        # meta-counts: 17 signatures, 60 attributes (integer, no unit, closed noun list,
        # and no metric word anywhere in the sentence: "peak 412 runs" is not a count)
        if is_int and unit is None and not seg_has_metric and any(w in _META_NOUNS for w in after_sing):
            done("exempt", "meta_count"); continue

        # method parameters (bootstrap 90% CIs, alpha = 0.05, seed 42, block 19)
        meth_hit = any(w in _METHOD_WORDS or _singular(w) in _METHOD_WORDS for w in before[-2:] + after)
        if meth_hit:
            decimal_ok = is_int or raw.rstrip().endswith("%") or any(w in _METHOD_DECIMAL_LEAD for w in before[-2:])
            if decimal_ok:
                done("method_param", "method_parameter"); continue

        # sample sizes / record lengths: 7,305 daily observations, 21 years
        if is_int and any(w in _SAMPLE_NOUNS for w in after_sing):
            done("sample_size"); continue

        has_decimal = ("." in raw) or bool(re.search(r"[eE][-+]?\d", raw)) or bool(
            re.search(r"[×x*·]\s*10", raw))
        near_metric = False
        if not has_decimal and unit is None:
            ctx = wtext[max(seg_lo, num.start - 40):min(seg_hi, num.end + 40)]
            near_metric = hdr_metric or _METRIC_RE.search(ctx) is not None
        if has_decimal or unit is not None or near_metric:
            done("quantity"); continue

        # a bare integer in a sentence that carries a citation marker is being cited as a value
        if seg_has_marker:
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
