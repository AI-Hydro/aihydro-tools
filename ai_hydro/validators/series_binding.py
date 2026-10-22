"""Bind a validator to the daily series a computation actually used (Science Wave 3, PR-4b).

Before this, every streamflow validator read the session ``streamflow`` slot, which need not be the series the
signatures consumed (session study: a 2000-2020 slot, signatures over 1989-2009). Now:

* ``extract_hydrological_signatures`` writes the series it served (``_served_series``, from aihydro-watershed
  >= 0.2.0) to a session data file, and records ``meta.retained_series = {path, digest}`` (``digest`` = digest of
  the file bytes, the same record ``persist_streamflow_slot`` writes for the streamflow slot);
* a validator called with ``source="signatures"`` resolves ITS series from that file and checks it three ways:
  the file bytes against the recorded file digest, the series itself against the signatures' own
  ``streamflow_digest`` (recomputed with aihydro-watershed's digest scheme), and, when the caller passes
  ``series_digest``, against that;
* ``source="streamflow"`` (default) resolves from the streamflow slot, with the same checks where the slot
  records digests, and the caller's ``series_digest`` when given.

Any failed check is ``series_mismatch``: the validator returns ``insufficient_data`` and does NOT run on the other
series. An absent series (nothing retained) is ``series_not_retained`` and is reported separately, because it is
unknown, not wrong. Nothing here keys on a tool or metric name.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("ai_hydro.validators")

SERIES_MISMATCH = "series_mismatch"
SERIES_NOT_RETAINED = "series_not_retained"

SOURCES = ("signatures", "streamflow")

#: aihydro-watershed's digest scheme for the daily m3/s series ("sha256 over 'YYYY-MM-DD<TAB>repr(float)<LF>'
#: lines"; ``STREAMFLOW_DIGEST_SCHEME``). Reproduced here so the retained file can be re-verified against the
#: ``streamflow_digest`` the signatures recorded, without a watershed private import.
SERIES_DIGEST_SCHEME = (
    "sha256 over 'YYYY-MM-DD<TAB>repr(float)<LF>' lines of the daily m3/s series the signatures were computed from"
)


def series_digest(dates: list, q: list) -> str:
    """``sha256:<hex>`` of a daily series under aihydro-watershed's scheme (``None``/NaN is ``nan``)."""
    h = hashlib.sha256()
    for d, v in zip(dates, q):
        f = float("nan") if v is None else float(v)
        h.update(f"{str(d)[:10]}\t{f!r}\n".encode())
    return "sha256:" + h.hexdigest()


def _file_digest(path: Path) -> str:
    from aihydro_core.records import digest_bytes

    return digest_bytes(path.read_bytes())


@dataclass
class BoundSeries:
    """A resolved series, or the reason it could not be resolved (``problem`` is a code, ``None`` when resolved)."""

    source: str
    slot: str
    dates: list = field(default_factory=list)
    q: list = field(default_factory=list)
    qualifier_codes: Optional[list] = None
    approval_codes: Optional[list] = None
    file_digest: Optional[str] = None
    series_digest: Optional[str] = None
    checked_against: list = field(default_factory=list)       # which digests were verified
    gauge_id: Optional[str] = None
    problem: Optional[str] = None
    message: Optional[str] = None

    @property
    def resolved(self) -> bool:
        return self.problem is None

    @property
    def n_valid(self) -> int:
        return sum(1 for v in self.q if _finite(v))

    @property
    def period(self) -> tuple:
        return (str(self.dates[0])[:10], str(self.dates[-1])[:10]) if self.dates else (None, None)

    def describe(self) -> dict:
        """Compact, JSON-safe description for a validator's metadata (no arrays, no paths)."""
        start, end = self.period
        return {"source": self.source, "slot": self.slot, "series_digest": self.series_digest,
                "file_digest": self.file_digest, "start": start, "end": end,
                "n_days": len(self.dates), "n_valid_days": self.n_valid,
                "verified_against": list(self.checked_against)}


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _bad(source: str, slot: str, problem: str, message: str) -> BoundSeries:
    return BoundSeries(source=source, slot=slot, problem=problem, message=message)


def _check_expected(b: BoundSeries, expected: Optional[str]) -> BoundSeries:
    if expected and b.series_digest != expected:
        return _bad(b.source, b.slot, SERIES_MISMATCH,
                    f"the {b.source} series (digest {b.series_digest}, {b.period[0]} to {b.period[1]}) is not the "
                    f"series the caller named (digest {expected}); not validating a different series")
    if expected:
        b.checked_against.append("caller series_digest")
    return b


def _read_arrays(path: Path) -> tuple:
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise ValueError("series file is not a JSON object")
    dates, q = body.get("dates"), body.get("q_cms")
    if not isinstance(dates, list) or not isinstance(q, list) or len(dates) != len(q) or not dates:
        raise ValueError("series file has no matching dates / q_cms arrays")
    return body, dates, q


def add_slot_codes(served: dict, qualification_codes: list) -> None:
    """Split the slot's per-value code strings (``"A"``, ``"A e"``, ``"P"``, ``"A ICE"``: the approval code first,
    then qualifier tokens, space separated) into ``approval_codes`` and ``qualifier_codes`` (comma-joined tokens,
    watershed's format) on a served series, in place."""
    approval, qualifier = [], []
    for c in qualification_codes:
        toks = str(c or "").replace(",", " ").split()
        approval.append(" ".join(t for t in toks if t in ("A", "P")))
        qualifier.append(",".join(t for t in toks if t not in ("A", "P")))
    served["approval_codes"], served["qualifier_codes"] = approval, qualifier


def retain_served_series(session_id: str, served: dict, *, tag: str = "signatures") -> Optional[dict]:
    """Write ``served`` (watershed's ``_served_series``) to a session data file; ``{"path", "digest", "name"}``.

    The file name carries a hash of the content, so a second run over a different series never overwrites the
    file an earlier sealed run recorded. ``None`` when the write fails (the caller then simply has no retained
    series, which a validator reports as ``series_not_retained``).
    """
    from ai_hydro.session import store

    payload = {k: served.get(k) for k in ("dates", "q_cms", "units", "qualifier_codes", "approval_codes")}
    if not payload["dates"] or payload["q_cms"] is None or len(payload["dates"]) != len(payload["q_cms"]):
        return None
    name = f"{tag}_series_{hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]}.json"
    path = store.write_session_data_file(session_id, name, payload)
    if not path:
        return None
    try:
        return {"path": path, "digest": _file_digest(Path(path)), "name": name}
    except OSError as exc:
        log.warning("could not digest retained signatures series %s: %s", name, exc)
        return None


def bind_signatures_series(session: Any, expected_digest: Optional[str] = None) -> BoundSeries:
    """The series the session's ``signatures`` result was computed from (see the module docstring)."""
    slot = session.get("signatures")
    if not isinstance(slot, dict) or not slot:
        return _bad("signatures", "signatures", "no_signatures_slot",
                    "no signatures result in this session; run extract_hydrological_signatures first")
    data = slot.get("data") if isinstance(slot.get("data"), dict) else {}
    meta = slot.get("meta") if isinstance(slot.get("meta"), dict) else {}
    retained = meta.get("retained_series") if isinstance(meta.get("retained_series"), dict) else {}
    data_file = data.get("_data_file")
    if not (data_file and retained.get("path") and retained.get("digest")):
        return _bad("signatures", "signatures", SERIES_NOT_RETAINED,
                    "the signatures result did not retain the series it used (computed before aihydro-tools "
                    "2.8.0 / aihydro-watershed 0.2.0, or from a source that serves no series); re-run "
                    "extract_hydrological_signatures to validate the series it consumed")
    if retained["path"] != data_file:
        return _bad("signatures", "signatures", SERIES_MISMATCH,
                    "meta.retained_series names another file than the result's _data_file")
    path = Path(str(data_file))
    try:
        have = _file_digest(path)
    except OSError as exc:
        return _bad("signatures", "signatures", SERIES_NOT_RETAINED,
                    f"the retained series file is gone or unreadable ({type(exc).__name__})")
    if have != retained["digest"]:
        return _bad("signatures", "signatures", SERIES_MISMATCH,
                    "the retained series file changed after the signatures were computed "
                    f"(digest {have} != recorded {retained['digest']})")
    try:
        body, dates, q = _read_arrays(path)
    except Exception as exc:                                               # noqa: BLE001
        return _bad("signatures", "signatures", SERIES_MISMATCH,
                    f"the retained series file cannot be read as a daily series ({type(exc).__name__}: {exc})")
    got = series_digest(dates, q)
    checked = ["retained file digest"]
    recorded = data.get("streamflow_digest")
    if isinstance(recorded, str) and recorded:
        if recorded != got:
            return _bad("signatures", "signatures", SERIES_MISMATCH,
                        "the retained series is not the series the signatures were computed from "
                        f"(series digest {got} != the result's streamflow_digest {recorded})")
        checked.append("signatures streamflow_digest")
    b = BoundSeries(
        source="signatures", slot="signatures", dates=list(dates), q=list(q),
        qualifier_codes=body.get("qualifier_codes"), approval_codes=body.get("approval_codes"),
        file_digest=have, series_digest=got, checked_against=checked,
        gauge_id=str(meta.get("params", {}).get("gauge_id") or "") or None,
    )
    return _check_expected(b, expected_digest)


def bind_streamflow_series(session: Any, expected_digest: Optional[str] = None) -> BoundSeries:
    """The series in the session ``streamflow`` slot, verified where the slot records digests."""
    sf = session.get("streamflow")
    if not isinstance(sf, dict) or not sf:
        return _bad("streamflow", "streamflow", "no_streamflow_slot",
                    "no streamflow slot in this session; fetch streamflow first")
    data = sf.get("data") if isinstance(sf.get("data"), dict) else {}
    meta = sf.get("meta") if isinstance(sf.get("meta"), dict) else {}
    dates, q = data.get("dates") or [], data.get("q_cms") or []
    data_file = data.get("_data_file")
    file_digest: Optional[str] = None
    checked: list = []
    body: dict = {}
    if data_file and Path(str(data_file)).is_file():
        try:
            file_digest = _file_digest(Path(str(data_file)))
        except OSError:
            file_digest = None
        retained = meta.get("retained_series") if isinstance(meta.get("retained_series"), dict) else {}
        if file_digest and retained.get("digest"):
            if retained["digest"] != file_digest or retained.get("path") != data_file:
                return _bad("streamflow", "streamflow", SERIES_MISMATCH,
                            "the streamflow retained file changed after it was fetched "
                            f"(digest {file_digest} != recorded {retained['digest']})")
            checked.append("retained file digest")
        if not dates or not q or len(dates) != len(q):
            try:
                body, dates, q = _read_arrays(Path(str(data_file)))
            except Exception:                                              # noqa: BLE001
                dates, q = data.get("dates") or [], data.get("q_cms") or []
    if not dates or not q or len(dates) != len(q):
        return _bad("streamflow", "streamflow", SERIES_NOT_RETAINED,
                    "the streamflow slot holds no full dates / q_cms arrays (stored compactly and the "
                    "retained file is missing); re-fetch streamflow")
    b = BoundSeries(
        source="streamflow", slot="streamflow", dates=list(dates), q=list(q),
        qualifier_codes=(body.get("qualifier_codes") or data.get("qualifier_codes")),
        approval_codes=(body.get("approval_codes") or data.get("approval_codes")),
        file_digest=file_digest, series_digest=series_digest(dates, q), checked_against=checked,
        gauge_id=str(meta.get("params", {}).get("gauge_id") or "") or None,
    )
    return _check_expected(b, expected_digest)


def bind_series(session: Any, source: str = "streamflow", series_digest_expected: Optional[str] = None) -> BoundSeries:
    """Resolve ``source`` ("signatures" | "streamflow"); an unknown source is a problem, not a fallback."""
    if source == "signatures":
        return bind_signatures_series(session, series_digest_expected)
    if source == "streamflow":
        return bind_streamflow_series(session, series_digest_expected)
    return _bad(str(source), str(source), "unknown_source",
                f"source must be one of {SOURCES}, got {source!r}")


def bound_series_problem(problem: str, message: str, source: str = "signatures") -> BoundSeries:
    """An unresolved ``BoundSeries`` (what the signatures wrapper hands the validators when nothing was retained)."""
    return _bad(source, source, problem, message)


def bound_series_from_served(served: dict, *, file_digest: str, gauge_id: Optional[str] = None,
                             recorded_streamflow_digest: Optional[str] = None) -> BoundSeries:
    """The series a signatures call just served, bound without a slot lookup (used by the wrapper's auto-run).

    ``served`` is the in-memory ``_served_series``; it is verified against ``recorded_streamflow_digest`` (the
    digest the watershed recorded for the same series) exactly as a later ``source="signatures"`` call would.
    """
    dates, q = served.get("dates") or [], served.get("q_cms") or []
    if not dates or len(dates) != len(q):
        return _bad("signatures", "signatures", SERIES_NOT_RETAINED, "the signatures served no daily series")
    got = series_digest(dates, q)
    checked = ["retained file digest"]
    if recorded_streamflow_digest:
        if got != recorded_streamflow_digest:
            return _bad("signatures", "signatures", SERIES_MISMATCH,
                        f"served series digest {got} != recorded streamflow_digest {recorded_streamflow_digest}")
        checked.append("signatures streamflow_digest")
    return BoundSeries(
        source="signatures", slot="signatures", dates=list(dates), q=list(q),
        qualifier_codes=served.get("qualifier_codes"), approval_codes=served.get("approval_codes"),
        file_digest=file_digest, series_digest=got, checked_against=checked, gauge_id=gauge_id)
