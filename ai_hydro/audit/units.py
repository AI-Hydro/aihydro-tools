"""
Unit recognition for the Answer Auditor.

Two jobs, both pure text:

  * ``unit_after(text, pos)``  — is the number that ends at ``pos`` followed by
    a unit?  (decides whether a bare integer is a quantity; names the unit for
    ``unit_mismatch``)
  * ``units_conflict(prose_unit, declared_unit)`` — do two unit strings name
    different units?  Only a *recognised* prose unit can conflict, so an unknown
    token never produces a false ``unit_mismatch``.

Units are normalised (``m³/s`` == ``m3/s`` == ``cms`` == ``m3 s-1``) before
comparison.  The table is deliberately small: a hydrology answer's units, not a
general unit library.
"""
from __future__ import annotations

import re

# Alternation order matters (first match wins), so phrases come before their prefixes.
# Single-letter units that are also common words or symbols (s, d, h, g, t, K, in) are
# deliberately absent: "17 s", "3 in" are not safe to call units.
_LEN = r"(?:kilomet(?:re|er)s?|met(?:re|er)s?|miles?|feet|foot|inch(?:es)?|ft|mi|km|m)"
_UNIT_ALTS = (
    # volumetric flow: cubic metres per second, cubic feet per second
    r"(?:cubic|cu\.?)\s+(?:met(?:re|er)s?|m|feet|foot|ft)\s+(?:per|/)\s*(?:second|sec|s)|"
    r"(?:litres?|liters?)\s+per\s+(?:second|sec)|l\s?/\s?s|mgd|"
    # rates: millimetres per day, metres per second
    r"(?:millimet(?:re|er)s?|mm|met(?:re|er)s?)\s+per\s+(?:second|sec|day|year|hour|hr)|"
    # areas
    r"(?:square|sq\.?)\s*(?:kilomet(?:re|er)s?|met(?:re|er)s?|miles?|feet|foot|km|mi|ft|m|ha)|"
    r"m³\s?/\s?s|m3\s?/\s?s|m\^3\s?/\s?s|m³\s?s⁻¹|m3\s?s-1|cumecs?|cms|cfs|"
    r"ft³/s|ft3/s|"
    r"mm\s?/\s?(?:day|d|yr|year|hr|h)|mm\s?d⁻¹|mm\s?d-1|mm\s?(?:yr|y)⁻¹|mm\s?yr-1|"
    r"mg\s?/\s?L|mg\s?L⁻¹|ppm|ppb|W\s?/\s?m²|W\s?m⁻²|"
    r"km²|km2|km\^2|mi²|mi2|m²|m2|m³|m3|millimet(?:re|er)s?|"
    r"mm|cm|km|hectares?|ha|acres?|"
    # temperature / angle
    r"(?:degrees?|deg\.?|°)\s?(?:celsius|fahrenheit|[CFNSEW])(?![A-Za-z])|celsius|fahrenheit|degrees?|°|‰|"
    # percent
    r"per\s?cent|percentage\s+points?|percent|%|"
    # time-counts
    r"days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|weeks?|months?|years?|yrs?|"
    # lengths / masses
    rf"{_LEN}|kg|mg"
)

_UNIT_AFTER_RE = re.compile(rf"[ \t]?(?P<u>{_UNIT_ALTS})(?![A-Za-z0-9])", re.IGNORECASE)

# Prose units whose presence marks a *time-count* rather than a physical unit;
# these are never compared against a declared unit map.
_TIME_COUNT = {"day", "hour", "minute", "second", "week", "month", "year"}

_ALIASES = {
    "cms": "m3/s", "cumec": "m3/s", "cumecs": "m3/s", "m3/sec": "m3/s",
    "cubicmetrespersecond": "m3/s", "cubicmeterspersecond": "m3/s", "cubicmetrepersecond": "m3/s",
    "cubicmeterpersecond": "m3/s", "cum/s": "m3/s",
    "cubicfeetpersecond": "cfs", "cubicfootpersecond": "cfs", "ft3/s": "cfs", "cuftpersecond": "cfs",
    "litrespersecond": "l/s", "literspersecond": "l/s", "litrepersecond": "l/s",
    "mm/day": "mm/d", "mm/dy": "mm/d", "mmperday": "mm/d", "millimetrespermay": "mm/d",
    "millimetresperday": "mm/d", "millimetersperday": "mm/d", "mm/year": "mm/yr", "mm/y": "mm/yr",
    "mmperyear": "mm/yr", "millimetresperyear": "mm/yr", "millimetersperyear": "mm/yr",
    "mm/hr": "mm/h", "mmperhour": "mm/h",
    "percent": "%", "pct": "%", "percent": "%", "percentpoints": "%", "percentagepoint": "%",
    "percentagepoints": "%", "percentage point": "%",
    "squarekilometres": "km2", "squarekilometers": "km2", "squarekilometre": "km2",
    "squarekilometer": "km2", "sqkm": "km2", "km^2": "km2",
    "squaremetres": "m2", "squaremeters": "m2", "squaremetre": "m2", "squaremeter": "m2",
    "sqm": "m2", "m^2": "m2", "squaremiles": "mi2", "squaremile": "mi2", "sqmi": "mi2", "mi^2": "mi2",
    "squarefeet": "ft2", "sqft": "ft2",
    "hectares": "ha", "hectare": "ha", "acres": "acre",
    "metres": "m", "meters": "m", "metre": "m", "meter": "m",
    "kilometres": "km", "kilometers": "km", "kilometre": "km", "kilometer": "km",
    "millimetres": "mm", "millimeters": "mm", "millimetre": "mm", "millimeter": "mm",
    "feet": "ft", "foot": "ft", "miles": "mi", "mile": "mi", "inches": "in", "inch": "in",
    "m^3": "m3",
    "degc": "degc", "degreescelsius": "degc", "degreecelsius": "degc", "celsius": "degc",
    "degreesc": "degc", "degreec": "degc",
    "degf": "degf", "degreesfahrenheit": "degf", "fahrenheit": "degf", "degreesf": "degf",
    "degrees": "deg", "degree": "deg",
    "days": "day", "d": "day", "hours": "hour", "hrs": "hour", "hr": "hour", "h": "hour",
    "minutes": "minute", "mins": "minute", "min": "minute", "seconds": "second",
    "secs": "second", "sec": "second", "s": "second", "weeks": "week", "months": "month",
    "years": "year", "yrs": "year", "yr": "year", "y": "year",
}

# Declared units that mean "a pure number" and so agree with a bare ratio or %.
_DIMENSIONLESS = {"", "-", "1", "fraction", "ratio", "dimensionless", "unitless", "none", "[-]", "(-)"}


_LEN_CANON = {
    "kilometres": "km", "kilometers": "km", "kilometre": "km", "kilometer": "km", "km": "km",
    "metres": "m", "meters": "m", "metre": "m", "meter": "m", "m": "m",
    "miles": "mi", "mile": "mi", "mi": "mi", "feet": "ft", "foot": "ft", "ft": "ft", "ha": "ha",
}


def normalise_unit(unit: str | None) -> str:
    """Canonical lower-case spelling of a unit string (``m³ s⁻¹`` -> ``m3/s``)."""
    if unit is None:
        return ""
    u = str(unit).strip().lower()
    u = (u.replace("³", "3").replace("²", "2").replace("⁻¹", "-1").replace("⁻²", "-2")
          .replace("·", " ").replace("^", ""))
    u = re.sub(r"\s*/\s*", "/", u)
    # "m3 s-1" / "mm d-1" / "mm yr-1"  ->  "m3/s" / "mm/d" / "mm/yr"
    u = re.sub(r"^([a-z0-9]+)\s+([a-z]+)-1$", r"\1/\2", u)
    key = re.sub(r"\s+", "", u).replace(".", "")
    # square X / sq X  ->  X2
    m = re.fullmatch(r"(?:square|sq)([a-z]+)", key)
    if m and m.group(1) in _LEN_CANON:
        return _LEN_CANON[m.group(1)] + "2"
    # cubic X per second  ->  m3/s | cfs
    m = re.fullmatch(r"(?:cubic|cu)([a-z]+)(?:per|/)(?:second|sec|s)", key)
    if m and m.group(1) in _LEN_CANON:
        return "cfs" if _LEN_CANON[m.group(1)] == "ft" else _LEN_CANON[m.group(1)] + "3/s"
    key = key.replace("°", "deg") if key.startswith("°") and len(key) > 1 else key
    if key == "°":
        key = "deg"
    return _ALIASES.get(key, key)


def unit_after(text: str, pos: int) -> str | None:
    """Return the raw unit text directly after ``pos`` (at most one space), else None."""
    m = _UNIT_AFTER_RE.match(text, pos)
    return m.group("u") if m else None


def is_time_count_unit(unit: str | None) -> bool:
    return normalise_unit(unit) in _TIME_COUNT


def units_conflict(prose_unit: str | None, declared_unit: str | None) -> bool:
    """True when the prose unit is recognised, the run declares one, and they differ."""
    if not prose_unit or declared_unit is None:
        return False
    p, d = normalise_unit(prose_unit), normalise_unit(declared_unit)
    if not p or p in _TIME_COUNT:
        return False
    if p == d:
        return False
    if p == "%" and d in _DIMENSIONLESS:
        return False          # percent of a stored fraction is the value check's job
    if p.startswith("deg") and d.startswith("deg") and p in ("deg", "degn", "degs", "dege", "degw"):
        return False          # degrees N/W/E vs a coordinate unit: do not guess
    return True
