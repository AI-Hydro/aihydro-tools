"""Offline replay of the recorded USGS Water Data API bodies (tests/fixtures/usgs_waterdata/).

Copied verbatim from aihydro-data's recordings (not re-recorded); installed over the
adapter's single network seam ``usgs_waterdata._http_get``. An unrecorded request fails
the test loudly - there is no network."""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

FIX = Path(__file__).parent / "fixtures" / "usgs_waterdata"
INDEX = {e["name"]: e for e in json.loads((FIX / "index.json").read_text())["entries"]}


def _read(path: Path) -> bytes:
    b = path.read_bytes()
    return gzip.decompress(b) if path.suffix == ".gz" else b


def body(name: str) -> bytes:
    return _read(FIX / INDEX[name]["file"])


def doc(name: str) -> dict:
    return json.loads(body(name))


def _key(url: str, params=None) -> tuple:
    if params:
        url = url + "?" + urlencode(params)
    u = urlparse(url)
    return (u.hostname, u.path, tuple(sorted(parse_qsl(u.query))))


class Replay:
    def __init__(self, override=None):
        """``override(url, params) -> (status, headers, body) | None`` is consulted first."""
        self.override = override
        self.table = {_key(e["url"]): e for e in INDEX.values()}
        self.calls: list[tuple] = []

    def __call__(self, url, params=None, headers=None):
        self.calls.append((url, dict(params) if params else None))
        if self.override:
            r = self.override(url, params)
            if r is not None:
                return r
        e = self.table.get(_key(url, params))
        assert e is not None, f"unrecorded request: {url} {params}"
        return e["status"], dict(e["headers"]), _read(FIX / e["file"])


def ok(obj, status=200):
    return status, {"Date": "Sat, 17 Oct 2026 00:00:00 GMT"}, json.dumps(obj).encode()


EMPTY = {"type": "FeatureCollection", "features": [], "numberReturned": 0, "links": []}
