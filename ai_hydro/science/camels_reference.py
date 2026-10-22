"""The CAMELS-US reference a signatures run is compared against (Science Wave 3, PR-4b).

``build_camels_reference`` turns one signatures result into

* a ``camels_reference`` block (plain summary: which reference was used and its hash, period, area, forcing,
  climate context, how many signatures can carry a "differs from CAMELS" claim), and
* ``reference_comparison``: aihydro-watershed's per-signature table
  ``{"table": {sig: {..., "can_claim_difference"}}, "context", "climate_context"}``.

Both go into the signatures result data. The run row keeps ``reference_comparison`` at ONE place,
``key_outputs.reference_comparison``, which is exactly where ``ai_hydro.claims.status_gate`` reads it.

Reference source, in order:

1. the session ``camels`` slot (``fetch_camels_us``, pygeohydro) when it holds THIS gauge;
2. the packaged CAMELS-US v1.2 tables (``ai_hydro/knowledge/reference/camels_us_v1_2``, CC-BY 4.0), whose files
   are re-checked against the md5 in their provenance JSON before use.

The source actually used is recorded with a hash either way (critique S6): the md5/sha256 of the packaged
files, or the sha256 of the slot row. Nothing here is a network call.
"""
from __future__ import annotations

import csv
import functools
import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Any, Mapping, Optional

log = logging.getLogger("ai_hydro.science")

PACKAGED_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "reference" / "camels_us_v1_2"
PACKAGED_FILES = {"hydro": "camels_hydro.txt", "clim": "camels_clim.txt", "topo": "camels_topo.txt"}
REFERENCE_NAME = "CAMELS-US v1.2"
LICENSE = "CC-BY-4.0"
FETCH_HINT = "call fetch_camels_us(gauge_id=...) to use the pygeohydro table instead"

#: Columns read from the packaged tables (everything else in the files is ignored).
CLIMATE_COLUMNS = ("frac_snow", "aridity", "p_seasonality")
AREA_COLUMN = "area_gages2"


def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_gauge_id(gauge_id: Any) -> Optional[str]:
    """USGS station number as the 8-digit CAMELS id, or ``None`` when it is not a plausible station number."""
    g = str(gauge_id or "").strip()
    if not g.isdigit() or not 7 <= len(g) <= 8:
        return None
    return g.zfill(8)


@functools.lru_cache(maxsize=1)
def _packaged_tables() -> tuple:
    """``(tables, source, problem)``: ``tables`` is ``{gauge_id: row}`` and ``problem`` is ``None`` when every
    packaged file exists and matches the md5 its provenance JSON records."""
    files: dict = {}
    for key, name in PACKAGED_FILES.items():
        path = PACKAGED_DIR / name
        prov_path = PACKAGED_DIR / (name + ".provenance.json")
        if not path.is_file() or not prov_path.is_file():
            return {}, None, f"packaged file {name} or its provenance JSON is missing"
        try:
            prov = json.loads(prov_path.read_text(encoding="utf-8"))
            have = _md5(path)
        except Exception as exc:                          # noqa: BLE001
            return {}, None, f"packaged file {name} unreadable ({type(exc).__name__})"
        if have != prov.get("md5_published"):
            return {}, None, (f"packaged file {name} md5 {have} differs from the published md5 "
                              f"{prov.get('md5_published')} recorded in its provenance JSON")
        files[name] = {"md5": have, "sha256": _sha256(path), "size_bytes": path.stat().st_size,
                       "source": prov.get("source"), "record": prov.get("record")}
    tables: dict = {}
    for key, name in PACKAGED_FILES.items():
        with (PACKAGED_DIR / name).open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter=";"):
                gid = normalize_gauge_id(row.get("gauge_id"))
                if gid is None:
                    continue
                tables.setdefault(gid, {}).update({k: v for k, v in row.items() if k != "gauge_id"})
    first = next(iter(files.values()))
    source = {
        "kind": "packaged", "reference": REFERENCE_NAME, "record": first.get("record"),
        "license": LICENSE, "attribution": "ai_hydro/knowledge/reference/camels_us_v1_2/ATTRIBUTION.md",
        "files": files,
        "hash": "sha256:" + hashlib.sha256(
            "\n".join(f"{n}:{files[n]['sha256']}" for n in sorted(files)).encode()).hexdigest(),
    }
    return tables, source, None


def packaged_row(gauge_id: Any) -> tuple:
    """``(row, source, problem)`` for one gauge from the packaged tables."""
    gid = normalize_gauge_id(gauge_id)
    if gid is None:
        return None, None, "the gauge id is not a USGS station number"
    tables, source, problem = _packaged_tables()
    if problem:
        return None, None, problem
    row = tables.get(gid)
    if row is None:
        return None, source, f"gauge {gid} is not one of the CAMELS-US v1.2 basins"
    return row, source, None


def _slot_row(session: Any, gauge_id: Optional[str]) -> tuple:
    """``(row, source, problem)`` from the session ``camels`` slot when it holds this gauge."""
    try:
        slot = session.get("camels") if session is not None else None
    except Exception:                                                  # noqa: BLE001
        slot = None
    if not isinstance(slot, dict):
        return None, None, None
    data = slot.get("data") if isinstance(slot.get("data"), dict) else {}
    attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    if data.get("mode") == "multi":
        return None, None, ("the session camels slot holds a multi-gauge request (mode 'multi'), whose rows are "
                            "not read here; call fetch_camels_us(gauge_id=...) for this gauge")
    if not attrs:
        return None, None, "the session camels slot has no attributes (the gauge is not in CAMELS-US)"
    if normalize_gauge_id(data.get("gauge_id")) != gauge_id:
        return None, None, f"the session camels slot holds gauge {data.get('gauge_id')!r}, not {gauge_id}"
    if _num(attrs.get(AREA_COLUMN)) is None:
        return None, None, "the session camels slot has no area_gages2"
    wanted = sorted(set(_wanted_columns()) | {AREA_COLUMN})
    row = {k: attrs[k] for k in wanted if k in attrs}
    meta = slot.get("meta") if isinstance(slot.get("meta"), dict) else {}
    source = {
        "kind": "session_slot", "slot": "camels", "tool": meta.get("tool", "fetch_camels_us"),
        "source": meta.get("source"), "citation": meta.get("citation"),
        "reference": REFERENCE_NAME,
        "version_note": "the CAMELS table pygeohydro served; its version is not recorded by the slot",
        "hash": "sha256:" + hashlib.sha256(
            json.dumps(row, sort_keys=True, default=str).encode()).hexdigest(),
        "hash_of": "the attribute row used (sorted-key JSON)",
    }
    return row, source, None


def _wanted_columns() -> tuple:
    from aihydro_watershed.signatures.comparison import CAMELS_SIGNATURES, CLIMATE_CONTEXT_KEYS
    return tuple(CAMELS_SIGNATURES) + tuple(CLIMATE_CONTEXT_KEYS)


def resolve_reference(session: Any, gauge_id: Any) -> dict:
    """``{"row", "source", "overrode", "problem"}``; the session slot overrides the packaged table."""
    gid = normalize_gauge_id(gauge_id)
    if gid is None:
        return {"row": None, "source": None, "overrode": None,
                "problem": "no USGS station number for this basin, so there is no CAMELS-US row to compare with"}
    row, source, slot_problem = _slot_row(session, gid)
    packaged, psource, pproblem = packaged_row(gid)
    if row is not None:
        return {"row": row, "source": source, "problem": None, "gauge_id": gid,
                "overrode": ({"kind": "packaged", "hash": psource["hash"]} if packaged is not None and psource else None)}
    if packaged is None:
        return {"row": None, "source": psource, "overrode": None, "problem": pproblem, "gauge_id": gid}
    out = {"row": {k: v for k, v in packaged.items()}, "source": psource, "overrode": None,
           "problem": None, "gauge_id": gid}
    if slot_problem:
        out["session_slot_ignored"] = slot_problem
    return out


def _unavailable(reason: str, *, source: Optional[dict] = None, gauge_id: Optional[str] = None) -> dict:
    return {"available": False, "reference": REFERENCE_NAME, "gauge_id": gauge_id, "reason": reason,
            "source": source, "fix": FETCH_HINT if "packaged file" in reason else None}


def build_camels_reference(
    sigs: Mapping,
    gauge_id: Any,
    area_km2: Any,
    start_date: str,
    end_date: str,
    session: Any = None,
) -> tuple:
    """``(camels_reference, reference_comparison)`` for one signatures result (``reference_comparison`` may be ``None``).

    Never raises: a failure is reported as ``available: False`` with the reason.
    """
    gid = normalize_gauge_id(gauge_id)
    try:
        ref = resolve_reference(session, gauge_id)
        if ref["row"] is None:
            return _unavailable(ref["problem"], source=ref.get("source"), gauge_id=gid), None
        from aihydro_watershed.signatures.comparison import reference_comparison

        row = {k: _num(v) for k, v in ref["row"].items()}
        area_ref = row.get(AREA_COLUMN)
        comparison = reference_comparison(sigs, row, _num(area_km2), area_ref,
                                          (start_date, end_date))
    except Exception as exc:                                                # noqa: BLE001
        log.warning("camels_reference failed (non-fatal): %s", exc)
        return _unavailable(f"comparison failed: {type(exc).__name__}: {exc}", gauge_id=gid), None
    ctx = comparison.get("context") or {}
    table = comparison.get("table") or {}
    product = ctx.get("precipitation_product")
    block: dict = {
        "available": True,
        "reference": REFERENCE_NAME,
        "gauge_id": ref["gauge_id"],
        "source": ref["source"],
        "period": {"used": ctx.get("period_used"), "used_source": ctx.get("period_used_source"),
                   "ref": ctx.get("period_ref"), "differs": ctx.get("period_differs")},
        "area": {"used_km2": ctx.get("area_used_km2"), "ref_km2": ctx.get("area_ref_km2"),
                 "ratio": ctx.get("area_ratio"), "differs": ctx.get("area_differs"),
                 "ref_column": AREA_COLUMN},
        "forcing": {"precipitation_product": product, "ref": ctx.get("forcing_ref"),
                    "differs": ctx.get("forcing_differs"),
                    "disclosure": (f"runoff_ratio and stream_elas here use {product} precipitation; "
                                   f"{REFERENCE_NAME} uses {ctx.get('forcing_ref')}: a difference may be the "
                                   "forcing, not the basin" if product else
                                   "no precipitation was used, so runoff_ratio and stream_elas are not compared")},
        "climate_context": comparison.get("climate_context") or {},
        "can_claim_difference": {k: bool(v.get("can_claim_difference")) for k, v in table.items()},
        "blockers": {k: list(v.get("blockers") or []) for k, v in table.items() if v.get("blockers")},
        "n_signatures": len(table),
        "n_can_claim_difference": sum(1 for v in table.values() if v.get("can_claim_difference")),
        "table_location": "reference_comparison (key_outputs.reference_comparison in the run row)",
        "note": ctx.get("note"),
    }
    if ref.get("overrode"):
        block["overrode"] = ref["overrode"]
    if ref.get("session_slot_ignored"):
        block["session_slot_ignored"] = ref["session_slot_ignored"]
    return block, comparison
