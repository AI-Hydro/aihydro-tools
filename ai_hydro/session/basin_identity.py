"""Basin identity for session results (tool integrity T1-B, D01-D05, D33).

A session holds one watershed (plus any registered features). Every result derived
from a basin is stamped with *which* basin it was computed for, and every read of a
basin-dependent result checks the stamp against the basin it is being asked about.
Without that, a second delineation in a used session answered with the first basin's
numbers (same cache key, same slot, same ``site_id``).

Identity is two facts, both optional:

* ``geometry_digest``: content hash of the bare basin geometry. This is the digest
  the feature cache already folded into registered-feature keys
  (``aihydro_core.primitives.hashing.content_hash`` of ``Feature.geometry_dict()``);
  there is one definition, here.
* ``gauge_id``: the gauge the basin is observed at (``session.site_id`` for the session
  basin, ``metadata.gauge_id`` for a registered feature).

A result stamped ``meta.basin_identity = {"geometry_digest", "gauge_id"}`` is checked
against the expected identity of the basin being read:

``verified``    every component present on both sides agrees
``stale``       a component present on both sides differs: the result belongs to another basin
``unverified``  the result carries no stamp (written before this mechanism existed, or with
                no basin yet): it is neither trusted as verified nor discarded as wrong

Policy (enforced in ``HydroSession``, the single loader):

* ``get_result`` (cache lookups: twi, cn, geomorphic, signatures, forcing, ssurgo ...) serves
  only ``verified`` results; ``stale`` and ``unverified`` are a miss, so the tool recomputes
  and the fresh result is stamped.
* ``get`` (slot consumers: streamflow, forcing, geomorphic, cn, soil ...) returns nothing for
  ``stale``; an ``unverified`` slot is returned (an existing session must keep loading) and
  is listed by ``HydroSession.unverified_slots`` / the ``basin_identity`` block of the session
  summary and of the consuming tool results.
* Replacing the session watershed with a different geometry drops every dependent result held
  under the session basin and clears ``site_id`` (unless the new watershed names the same gauge).
"""
from __future__ import annotations

import json
import os
from typing import Any, Iterable, Mapping, Optional

IDENTITY_KEY = "basin_identity"

VERIFIED = "verified"
STALE = "stale"
UNVERIFIED = "unverified"

#: Slots whose content depends on which basin they were computed for. ``watershed`` is the
#: identity source and ``model`` / ``camels`` are deliberately outside: a trained model and a
#: CAMELS attribute table are multi-basin by design and must survive a basin change.
BASIN_DEPENDENT_SLOTS = frozenset({
    "streamflow", "signatures", "geomorphic", "forcing", "twi", "cn", "soil", "soil_loss",
    "ssurgo", "baseflow", "flow_duration", "flood_frequency", "drought", "design_hydrograph",
    "inundation", "inundation_hydrograph", "inundation_physics", "inundation_surrogate",
})

#: The sentinel feature id used for the session's own basin (mirrors ``store._LEGACY_FEATURE_ID``).
LEGACY_FEATURE_ID = "__legacy__"

_DIGEST_LEN = 16


def bare_geometry(geojson: Any) -> Optional[dict]:
    """The geometry dict inside a GeoJSON Geometry / Feature / FeatureCollection, else ``None``."""
    if not isinstance(geojson, Mapping):
        return None
    kind = geojson.get("type")
    if kind == "Feature":
        g = geojson.get("geometry")
        return dict(g) if isinstance(g, Mapping) else None
    if kind == "FeatureCollection":
        geoms = [f.get("geometry") for f in (geojson.get("features") or []) if isinstance(f, Mapping)]
        geoms = [dict(g) for g in geoms if isinstance(g, Mapping)]
        if not geoms:
            return None
        return geoms[0] if len(geoms) == 1 else {"type": "GeometryCollection", "geometries": geoms}
    return dict(geojson) if kind else None


def geometry_digest(geojson: Any) -> Optional[str]:
    """Content hash of the bare geometry (``None`` for an empty / unreadable one)."""
    geom = bare_geometry(geojson)
    if not geom:
        return None
    from aihydro_core.primitives.hashing import content_hash
    return content_hash(geom, length=_DIGEST_LEN)


def norm_gauge(g: Any) -> Optional[str]:
    """Gauge id as compared: stripped, ``USGS-`` prefix dropped, digits zero-padded to 8."""
    s = str(g or "").strip().upper().removeprefix("USGS-")
    if not s:
        return None
    return s.zfill(8) if s.isdigit() else s


def make_identity(digest: Optional[str], gauge: Any) -> dict:
    out: dict = {}
    if digest:
        out["geometry_digest"] = digest
    g = norm_gauge(gauge)
    if g:
        out["gauge_id"] = g
    return out


_FILE_DIGESTS: dict = {}


def _digest_of_file(path: str) -> Optional[str]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    if key not in _FILE_DIGESTS:
        if len(_FILE_DIGESTS) > 256:
            _FILE_DIGESTS.clear()
        try:
            with open(path) as f:
                _FILE_DIGESTS[key] = geometry_digest(json.load(f))
        except (OSError, ValueError):
            return None
    return _FILE_DIGESTS[key]


def watershed_slot_digest(ws: Any) -> Optional[str]:
    """Digest of the geometry a ``watershed`` slot value points at (file path or inline)."""
    data = ws.get("data") if isinstance(ws, Mapping) else None
    if not isinstance(data, Mapping):
        return None
    path = data.get("geometry_geojson_path")
    if path:
        d = _digest_of_file(str(path))
        if d:
            return d
    for k in ("geometry_geojson", "geometry", "geojson"):
        if data.get(k):
            return geometry_digest(data[k])
    return None


def identity_of(result: Any) -> Optional[dict]:
    """The stamp on a stored result, or ``None`` when it carries none."""
    meta = result.get("meta") if isinstance(result, Mapping) else None
    ident = meta.get(IDENTITY_KEY) if isinstance(meta, Mapping) else None
    return dict(ident) if isinstance(ident, Mapping) else None


def stamp(result: Any, identity: Mapping) -> None:
    """Stamp ``result["meta"]["basin_identity"]`` unless a stamp is already there (in place)."""
    if not isinstance(result, dict):
        return
    meta = result.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        result["meta"] = meta
    meta.setdefault(IDENTITY_KEY, dict(identity))


def stamp_unverified(result: Any, inputs: Iterable[str]) -> None:
    """Record that ``result`` was computed from slots that carry no basin stamp (in place): its
    own stamp is empty (so it reads back ``unverified``) and names those inputs."""
    if not isinstance(result, dict):
        return
    meta = result.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        result["meta"] = meta
    meta.setdefault(IDENTITY_KEY, {})
    meta.setdefault("basin_identity_inputs_unverified", sorted(set(inputs)))


def judge(result: Any, expected: Mapping) -> str:
    """``verified`` / ``stale`` / ``unverified`` for ``result`` read as the basin ``expected``."""
    ident = identity_of(result)
    if ident is None or (result.get("meta") or {}).get("basin_identity_inputs_unverified"):
        return UNVERIFIED
    compared = False
    for comp in ("geometry_digest", "gauge_id"):
        have, want = ident.get(comp), expected.get(comp)
        if have and want:
            if have != want:
                return STALE
            compared = True
    if compared:
        return VERIFIED
    # Nothing comparable. A stamp with no components says "computed with no basin known";
    # it can only be trusted when the reader has no basin either.
    return VERIFIED if not expected else UNVERIFIED


def stale_message(slot: str, ident: Mapping, expected: Mapping) -> str:
    return (f"The {slot} result was computed for basin {dict(ident)} but the session basin is "
            f"{dict(expected)}; it was not used.")


def unverified_note(slots: Iterable[str]) -> dict:
    """The block consuming tools attach when they read slots that carry no basin stamp."""
    slots = sorted(set(slots))
    return {
        "status": UNVERIFIED,
        "slots": slots,
        "message": (f"The session slot(s) {slots} were written before basin identity was recorded, so it "
                    "cannot be shown that they belong to the session's current basin."),
        "recovery": ("Re-run the tool that produced each slot (clear_session first if it answers from "
                     "cache); the fresh result is stamped with the basin."),
    }
