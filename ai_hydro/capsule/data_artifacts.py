"""
Served-data artifacts for a capsule.

A capsule that cannot support independent recomputation of its claims is not an
executable research object. Session slots strip long arrays on save, so by
export time the discharge series a signature was computed from is usually gone
from ``session.json``. This module puts the series back into the capsule as a
stdlib-readable CSV under ``data/`` and records, for each file, its sha256, the
run that produced it, and exactly how it was obtained.

Mechanism. ``fetch_streamflow_data`` retains the series it served (in the
workspace, or beside the session file when there is none) and points the slot's
``_data_file`` at it. Export consumes exactly that retained artifact. In order:

1. ``retained_data_file``    the slot's ``_data_file`` JSON (the run's own bytes);
2. ``session_slot``          arrays still in the slot (short series);
3. ``aihydro_data_refetch``  fallback for sessions written before the fetch tool
                             retained its series: ``aihydro_data.fetch`` with
                             the request recorded in the slot
                             (``meta.params``). The aihydro-data disk cache
                             answers when it holds the request
                             (``cache_hit: true``); otherwise the provider is
                             queried again and the entry says so
                             (``cache_hit: false``), because a re-query is not
                             the bytes the run saw.

The retained JSON is copied verbatim into ``data/`` (``retained_artifact``,
with its sha256) and the CSV is derived from it.

The recovered series is not trusted blindly. The entry records ``n_rows`` against
the slot's recorded ``n_days``, compares the series digest with the
``<run_id>#q_cms`` ``served_data`` ref a consuming run recorded (when one did),
and, when the session holds a ``baseflow_index`` signature, a stdlib
Lyne-Hollick recomputation on the exported series. Those are
export-time consistency checks. They are not a replay: the manifest's
``replay_status`` stays ``archive_integrity`` and ``recomputation`` stays
``not_performed``.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
from pathlib import Path
from typing import Any

# Lyne-Hollick parameters used by aihydro-watershed's baseflow_index
# (BASEFLOW_SEPARATION_PARAMS). Duplicated here, not imported: the check below
# must agree with the recorded value for the right reason, and the capsule
# module stays free of the watershed package.
LH_ALPHA = 0.925
LH_PASSES = 3
BFI_TOLERANCE = 1e-6

SERIES_COLUMNS = ["date", "q_cms"]


def lyne_hollick_bfi(q: list[float], alpha: float = LH_ALPHA, passes: int = LH_PASSES) -> float:
    """Baseflow index sum(baseflow)/sum(q), alternating forward/backward sweeps."""

    def sweep(y: list[float], forward: bool) -> list[float]:
        n = len(y)
        f = [0.0] * n
        b = list(y)
        order = range(1, n) if forward else range(n - 2, -1, -1)
        prev = 0 if forward else n - 1
        for t in order:
            f[t] = alpha * f[prev] + (1.0 + alpha) / 2.0 * (y[t] - y[prev])
            b[t] = y[t] - f[t] if f[t] > 0.0 else y[t]
            b[t] = min(max(b[t], 0.0), y[t])
            prev = t
        return b

    bf = list(q)
    for i in range(passes):
        bf = sweep(bf, forward=(i % 2 == 0))
    total = sum(q)
    if total <= 0:
        return float("nan")
    bfi = sum(min(max(b, 0.0), x) for b, x in zip(bf, q)) / total
    return max(0.0, min(1.0, bfi))


def _isnum(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _from_slot(data: dict) -> tuple[list[str], list[float | None]] | None:
    dates, q = data.get("dates"), data.get("q_cms")
    if isinstance(dates, list) and isinstance(q, list) and dates and len(dates) == len(q):
        return [str(d) for d in dates], [float(v) if _isnum(v) else None for v in q]
    return None


def _from_data_file(data: dict) -> tuple[tuple[list[str], list[float | None]], Path] | None:
    path = data.get("_data_file")
    if not path or not Path(path).is_file():
        return None
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    got = _from_slot(body) if isinstance(body, dict) else None
    return (got, Path(path)) if got else None


def _from_aihydro_data(params: dict) -> tuple[tuple[list[str], list[float | None]], dict] | None:
    gauge, start, end = params.get("gauge_id"), params.get("start_date"), params.get("end_date")
    if not (gauge and start and end):
        return None
    try:
        from aihydro_data import fetch  # type: ignore[import]

        result = fetch("streamflow", gauge, start, end)
        df = result.data
        dates = df["date"].dt.strftime("%Y-%m-%d").tolist()
        q = [float(v) if _isnum(v) else None for v in df["streamflow"].tolist()]
    except Exception:
        return None
    if not dates:
        return None
    info = {
        "product": getattr(result, "product", None),
        "source": getattr(result, "source", None),
        "cache_hit": bool(getattr(result, "cache_hit", False)),
        "fetched_at": getattr(result, "fetched_at", None),
        "cache_key": getattr(result, "cache_key", None),
        "citation": getattr(result, "citation", None),
    }
    return (dates, q), info


def _format_q(v: float | None) -> str:
    return "" if v is None else repr(float(v))  # repr round-trips a float exactly


def _write_csv(path: Path, dates: list[str], q: list[float | None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(SERIES_COLUMNS)
        for d, v in zip(dates, q):
            w.writerow([d, _format_q(v)])


def _slot(session: Any, name: str) -> dict | None:
    try:
        v = session.get(name)
    except Exception:
        return None
    return v if isinstance(v, dict) else None


def _record_digest(session: Any, run_id: str | None) -> str | None:
    if not run_id:
        return None
    try:
        row = (session.get("_run_log") or {}).get(run_id) or {}
        return (row.get("record") or {}).get("record_digest")
    except Exception:
        return None


def _served_digest_check(session: Any, run_id: str | None, q: list[float | None]) -> dict | None:
    """Compare the exported series with the digest a consumer recorded for it.

    Consumers (extract_hydrological_signatures) record a ``served_data`` input
    ref ``<run_id>#q_cms`` with the digest of the series they actually read.
    """
    if not run_id:
        return None
    try:
        from aihydro_core.records import digest_or_error

        recorded = None
        for row in (session.get("_run_log") or {}).values():
            for ref in ((row or {}).get("record") or {}).get("input_refs") or []:
                if ref.get("ref") == f"{run_id}#q_cms" and ref.get("digest"):
                    recorded = ref["digest"]
        if recorded is None:
            return None
        got, err = digest_or_error(list(q))
        return {"recorded": recorded, "from_exported_series": got,
                "status": "agrees" if got == recorded else "differs"}
    except Exception:
        return None


def _consistency(session: Any, slot_data: dict, q_valid: list[float]) -> dict:
    checks: dict[str, Any] = {}
    n_recorded = slot_data.get("n_days")
    if isinstance(n_recorded, int):
        checks["n_rows_vs_recorded_n_days"] = {
            "recorded": n_recorded, "exported": None,  # filled by caller (rows incl. missing)
        }
    sig = _slot(session, "signatures")
    recorded = ((sig or {}).get("data") or {}).get("baseflow_index")
    if _isnum(recorded) and q_valid:
        got = lyne_hollick_bfi(q_valid)
        diff = abs(got - float(recorded))
        checks["baseflow_index"] = {
            "method": f"lyne_hollick alpha={LH_ALPHA} passes={LH_PASSES} (stdlib, export time)",
            "recorded": float(recorded),
            "from_exported_series": got,
            "abs_diff": diff,
            "status": "agrees" if diff <= BFI_TOLERANCE else "differs",
            "tolerance": BFI_TOLERANCE,
        }
    return checks


def collect_data_artifacts(session: Any, capsule_dir: Path) -> list[dict]:
    """Write the served streamflow series into ``capsule_dir/data`` and describe it.

    Returns one entry per attempted artifact. An unobtainable series yields an
    entry with ``status: "unavailable"`` and the reason, never a silent omission.
    """
    slot = _slot(session, "streamflow")
    if not slot:
        return []
    data = slot.get("data") or {}
    meta = slot.get("meta") or {}
    params = meta.get("params") or {}
    gauge = str(params.get("gauge_id") or data.get("gauge_id") or "unknown")
    run_id = meta.get("run_id")
    entry: dict[str, Any] = {
        "variable": "streamflow",
        "role": "served_data",
        "request": {k: params.get(k) for k in ("gauge_id", "start_date", "end_date", "interval")},
        "produced_by_run_id": run_id,
        "producer_record_digest": _record_digest(session, run_id),
        "units": data.get("units") or "m3/s",
    }

    series = None
    retained: Path | None = None
    if (got := _from_data_file(data)) is not None:
        series, retained = got
        entry["retrieval"] = {"mechanism": "retained_data_file"}
    elif (got := _from_slot(data)) is not None:
        series, entry["retrieval"] = got, {"mechanism": "session_slot"}
    else:
        fetched = _from_aihydro_data(params)
        if fetched is not None:
            series, info = fetched
            entry["retrieval"] = {"mechanism": "aihydro_data_refetch", **info}
    if series is None:
        entry.update(status="unavailable",
                     reason="series not in the session slot or workspace, and not retrievable "
                            "from aihydro-data for the recorded request")
        return [entry]

    dates, q = series
    rel = f"data/served_streamflow_{gauge}.csv"
    path = capsule_dir / rel
    _write_csv(path, dates, q)
    q_valid = [v for v in q if v is not None]
    if retained is not None:
        # Verbatim copy of the run's own retained bytes. The session-side file
        # is named "<session>.data.<name>"; keep only <name>.
        name = retained.name.split(".data.", 1)[-1]
        dest = capsule_dir / "data" / name
        if dest != path:
            shutil.copy2(retained, dest)
            entry["retained_artifact"] = {
                "path": f"data/{name}",
                "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
                "note": "verbatim copy of the file the fetch tool retained when it served the series",
            }
    checks = _consistency(session, data, q_valid)
    served = _served_digest_check(session, run_id, q)
    if served is not None:
        checks["served_series_digest"] = served
    if "n_rows_vs_recorded_n_days" in checks:
        checks["n_rows_vs_recorded_n_days"]["exported"] = len(q)
        c = checks["n_rows_vs_recorded_n_days"]
        c["status"] = "agrees" if c["recorded"] == c["exported"] else "differs"
    bad = [k for k, v in checks.items() if v.get("status") == "differs"]
    entry.update(
        status="exported" if not bad else "exported_with_inconsistency",
        path=rel,
        columns=SERIES_COLUMNS,
        n_rows=len(q),
        n_missing=len(q) - len(q_valid),
        first_date=dates[0],
        last_date=dates[-1],
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        consistency_checks=checks,
        note=("Export-time consistency checks only; this is not a replay. "
              "A reader recomputes from the CSV with their own code."),
    )
    return [entry]
