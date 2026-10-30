"""Date-window helpers for Earth Engine queries.

Same helper as ``aihydro_data.sources._gee_vendored.dates`` (this package carries its own copy of the
GEE extraction code); a test keeps the two in step.


``ee.ImageCollection.filterDate(start, end)`` is start-inclusive and
**end-exclusive** (https://developers.google.com/earth-engine/apidocs/ee-imagecollection-filterdate).
aihydro-data's public contract is an INCLUSIVE end ("2020-01-01".."2020-01-31" is 31 days), so every
``filterDate`` call passes its end through :func:`filter_end` - the only place that knows about the
off-by-one. A single-day request (start == end) would otherwise match nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta


def filter_end(end: str) -> str:
    """The exclusive ``filterDate`` end for an inclusive calendar-day ``end``.

    ``"2020-01-31"`` -> ``"2020-02-01"``. An end that carries a time of day other than midnight
    names an instant, not a day, and is returned unchanged.
    """
    text = str(end).strip()
    if len(text) == 10:
        return (datetime.strptime(text, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.hour == parsed.minute == parsed.second == parsed.microsecond == 0:
        return (parsed.date() + timedelta(days=1)).isoformat()
    return text
