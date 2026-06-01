"""
AI-Hydro Research Session (HydroSession)
==========================================

Persistent research state across MCP tool calls.

Identity model
--------------
Three identifier fields, distinct on purpose:

  session_id   : primary key. Any string the researcher or LLM chose
                 ("piscataquis-snowmelt-2020", UUID, a USGS gauge used as
                 shorthand). Immutable after creation — anything that
                 changes session identity creates a new session.

  site_id      : data-source ID for the primary monitoring point
                 (e.g. USGS gauge "01031500", GRDC station, MERIT outlet).
                 Empty for ungauged / multi-site studies. Set by the
                 data-fetching tools (delineate_watershed, fetch_streamflow_data),
                 not by the user.

  site_name    : human-readable display name (e.g.
                 "Piscataquis River near Dover-Foxcroft, ME"). Should match
                 the canonical name returned by the data source. Drift
                 from `watershed.data.gauge_name` is flagged by
                 ``validate_identity()`` and exposed via the
                 ``get_session_health`` MCP tool.

Storage
-------
~/.aihydro/sessions/<session_id>.json    (atomic write-then-rename)

The canonical identifier used in workspace filenames is given by
``session.canonical_id`` — preferring ``site_id`` when set, falling
back to a slug of ``session_id``. Tools should always call
``session.workspace_filename(prefix, ext)`` rather than building paths
by hand, so the same study's outputs land in a consistent namespace.

Slot model (C1 — aihydro-core)
-------------------------------
Slots now use a three-level keyed structure:

    _slots[product][feature_id][params_key] = result_dict

This means two features' results for the same product (e.g. TWI for two
map annotations) coexist without collision. Caching is keyed by
(product, feature_id, params_key) — identical geometry+params is a hit;
different geometry is always a miss.

Backward compatibility
----------------------
All existing tool code uses:
  - ``session.twi`` / ``session.watershed`` etc. (property getters/setters)
  - ``session.set(slot, value)`` / ``session.get(slot)``
  - ``session.record_result(slot, data)``

These all write to / read from the ``__legacy__`` sentinel feature ID so
that un-migrated tools continue working exactly as before. New tools use
the Store Protocol methods (``put_result``, ``get_result``) with explicit
feature IDs.

Old session files (``_hydro_slots_v2`` key absent) are migrated losslessly
on first load: each single-value slot becomes
``{slot: {"__legacy__": {"": old_value}}}``.

Store Protocol (aihydro_core.store.Store)
-----------------------------------------
HydroSession implements the Store Protocol from aihydro-core so that
FeatureRegistry and (C2) @feature_tool can operate on it without importing
the session layer. All Store methods are defined below.

Dynamic slots
-------------
Plugins can register their own result slots without editing core code:
    session.set("my_plugin_result", {...})
    session.get("my_plugin_result")  # → dict or None
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ai_hydro.session")

SESSIONS_DIR = Path.home() / ".aihydro" / "sessions"
_SESSIONS_DIR = SESSIONS_DIR  # backward compat alias
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_RULES_DIR_NAME = ".aihydrorules"

# Sentinel feature ID used by backward-compat set()/get() path.
# Old tools write results here; new @feature_tool tools use real feature IDs.
_LEGACY_FEATURE_ID = "__legacy__"

# Params key used by backward-compat set()/get() (params unknown for old results).
_LEGACY_PARAMS_KEY = ""

# Lists longer than this are stripped from the session JSON on disk.
_ARRAY_STRIP_THRESHOLD = 50

# Common slot names corresponding to built-in MCP tools.
_COMMON_SLOTS = (
    "watershed",
    "streamflow",
    "signatures",
    "geomorphic",
    "camels",
    "forcing",
    "twi",
    "cn",
    "model",
)

STALENESS_THRESHOLD = {
    "computed": 365,
    "notes": 30,
    "interpretation": 14,
}

_NOTE_DEDUP_WINDOW_SEC = 3600


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #

def _lean_slot(val: Any) -> Any:
    """Return a disk-safe (lean) copy of a single result dict."""
    if val is None:
        return None
    if not isinstance(val, dict):
        return val
    if "data" not in val:
        lean: dict = {}
        for k, v in val.items():
            if isinstance(v, list) and len(v) > _ARRAY_STRIP_THRESHOLD:
                lean[f"{k}_n"] = len(v)
            else:
                lean[k] = v
        return lean
    lean_data: dict = {}
    for k, v in val["data"].items():
        if isinstance(v, list) and len(v) > _ARRAY_STRIP_THRESHOLD:
            lean_data[f"{k}_n"] = len(v)
        else:
            lean_data[k] = v
    return {**val, "data": lean_data}


def _lean_product_slot(by_feature: dict) -> dict:
    """Apply _lean_slot to every result dict in a v2 product slot."""
    return {
        feature_id: {params_key: _lean_slot(result) for params_key, result in by_key.items()}
        for feature_id, by_key in by_feature.items()
    }


def _slugify_for_filename(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(s)).strip("-_.")
    return s.lower() or "session"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _synopsis_from_result(result: dict | None) -> dict:
    """Extract a lean LLM-readable synopsis from one result dict."""
    if not result:
        return {}
    data = result.get("data", {})
    meta = result.get("meta", {})
    synopsis: dict = {}
    for k, v in data.items():
        if k.startswith("_"):
            continue
        if isinstance(v, list):
            if len(v) > _ARRAY_STRIP_THRESHOLD:
                synopsis[f"{k}_n"] = len(v)
            else:
                synopsis[k] = v
        elif isinstance(v, dict):
            synopsis[k] = {
                dk: (dv if not (isinstance(dv, list) and len(dv) > _ARRAY_STRIP_THRESHOLD)
                     else f"[{len(dv)} items]")
                for dk, dv in v.items()
            }
        else:
            synopsis[k] = v
    computed_at = meta.get("computed_at", "")
    synopsis["_computed_at"] = computed_at[:10] if computed_at else None
    synopsis["_tool"] = meta.get("tool")
    if meta.get("params"):
        synopsis["_params"] = meta["params"]
    return synopsis


def _latest_result_in_feature(by_key: dict) -> dict | None:
    """Return the most recently computed result from a feature's params-keyed dict."""
    valid = [(k, v) for k, v in by_key.items() if v is not None]
    if not valid:
        return None
    return max(
        valid,
        key=lambda kv: (kv[1] or {}).get("meta", {}).get("computed_at", ""),
    )[1]


# --------------------------------------------------------------------------- #
# HydroSession
# --------------------------------------------------------------------------- #

class HydroSession:
    """Persistent research state for a single study across tool calls.

    Implements the aihydro_core.store.Store Protocol so FeatureRegistry and
    the (C2) @feature_tool decorator can address it directly.
    """

    def __init__(self, session_id: str, shard_id: str | None = None) -> None:
        self.session_id: str = session_id
        self.shard_id: str | None = shard_id
        self.site_name: str = ""
        self.site_id: str = ""
        self.site_type: str = ""
        self.workspace_dir: str | None = None
        self.working_geometry_path: str | None = None

        # Three-level slot store: product → feature_id → params_key → result_dict.
        # Old tools write to _LEGACY_FEATURE_ID via set()/get() backward compat.
        # New @feature_tool tools write with real feature IDs via put_result().
        self._slots: dict[str, dict[str, dict[str, dict | None]]] = {}

        # Feature registry: feature_id → Feature.to_dict() (aihydro_core)
        self._features: dict[str, dict] = {}
        self.active_feature_id: str | None = None

        self.notes: list[str] = []
        self.interpretation: str = ""
        self.interpretation_at: str | None = None
        self.created_at: str = _now()
        self.updated_at: str = self.created_at
        self.archived: bool = False
        self._citations: set[str] = set()
        self.artifact_manifest: dict[str, dict] = {}
        self.claims: dict[str, dict] = {}
        self.assumptions: dict[str, dict] = {}
        self._site_name_history: list[dict] = []

    # ------------------------------------------------------------------
    # Backward-compat: gauge_id → site_id
    # ------------------------------------------------------------------

    @property
    def gauge_id(self) -> str:
        return self.site_id or self.session_id

    # ------------------------------------------------------------------
    # Canonical identity
    # ------------------------------------------------------------------

    @property
    def canonical_id(self) -> str:
        if self.site_id:
            return _slugify_for_filename(self.site_id)
        return _slugify_for_filename(self.session_id)

    def workspace_filename(self, prefix: str, ext: str = "json") -> str:
        prefix = prefix.strip("_")
        ext = ext.lstrip(".")
        return f"{prefix}_{self.canonical_id}.{ext}"

    # ------------------------------------------------------------------
    # Identity mutation with audit
    # ------------------------------------------------------------------

    def set_site_name(self, name: str, reason: str | None = None) -> None:
        name = (name or "").strip()
        prev = (self.site_name or "").strip()
        if name == prev:
            return
        if prev:
            self._site_name_history.append({
                "prev": prev, "next": name,
                "at": _now(), "reason": reason or "",
            })
        self.site_name = name

    def validate_identity(self) -> list[dict]:
        warnings: list[dict] = []

        if self.watershed and self.site_name:
            canonical = (self.watershed.get("data", {}) or {}).get("gauge_name") or ""
            canonical = canonical.strip()
            if canonical:
                def _norm(s: str) -> str:
                    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()
                if _norm(self.site_name) != _norm(canonical) and (
                    _norm(canonical) not in _norm(self.site_name)
                    and _norm(self.site_name) not in _norm(canonical)
                ):
                    warnings.append({
                        "code": "site_name_drift", "severity": "warn",
                        "message": (
                            f"site_name '{self.site_name}' disagrees with the "
                            f"canonical gauge_name '{canonical}' from the watershed slot."
                        ),
                        "site_name": self.site_name, "canonical_name": canonical,
                    })

        if self.site_type == "usgs_gauge" and self.site_id:
            if not (self.site_id.isdigit() and 7 <= len(self.site_id) <= 10):
                warnings.append({
                    "code": "site_id_format", "severity": "warn",
                    "message": (
                        f"site_type is 'usgs_gauge' but site_id '{self.site_id}' "
                        "is not a 7-10 digit USGS station number."
                    ),
                })

        if (self.session_id.isdigit()
                and 7 <= len(self.session_id) <= 10
                and self.site_id
                and self.site_id != self.session_id):
            warnings.append({
                "code": "session_id_gauge_mismatch", "severity": "info",
                "message": (
                    f"session_id '{self.session_id}' looks like a USGS gauge "
                    f"but the analysed site is '{self.site_id}'."
                ),
            })

        if self.workspace_dir and not Path(self.workspace_dir).is_dir():
            warnings.append({
                "code": "workspace_missing", "severity": "warn",
                "message": f"workspace_dir '{self.workspace_dir}' no longer exists.",
            })

        if self.interpretation and not self.site_name and self.site_id:
            warnings.append({
                "code": "site_name_unset", "severity": "info",
                "message": "An interpretation exists but site_name is empty.",
            })

        return warnings

    # ------------------------------------------------------------------
    # Backward-compat slot access  (set / get)
    #
    # Both write to / read from __legacy__ sentinel feature so all existing
    # tool code keeps working unchanged through the C1→C2 migration window.
    # ------------------------------------------------------------------

    def set(self, slot: str, value: dict | None) -> None:
        """Backward-compat: store a result under the __legacy__ feature."""
        self._slots.setdefault(slot, {}).setdefault(_LEGACY_FEATURE_ID, {})[_LEGACY_PARAMS_KEY] = value

    def get(self, slot: str) -> dict | None:
        """
        Backward-compat: return the active feature's latest result, or __legacy__.

        Resolution order:
        1. active_feature_id's most recent result (new @feature_tool path)
        2. __legacy__ most recent result (old tool path)
        3. None
        """
        by_feature = self._slots.get(slot)
        if not by_feature:
            return None

        # 1. Try active feature (non-legacy)
        if self.active_feature_id and self.active_feature_id != _LEGACY_FEATURE_ID:
            by_key = by_feature.get(self.active_feature_id, {})
            result = _latest_result_in_feature(by_key)
            if result is not None:
                return result

        # 2. Fall back to __legacy__
        by_key = by_feature.get(_LEGACY_FEATURE_ID, {})
        return _latest_result_in_feature(by_key)

    # ------------------------------------------------------------------
    # Provenance (unchanged API — backward compat)
    # ------------------------------------------------------------------

    def add_artifact(
        self,
        artifact_id: str,
        data: Any,
        fetch_parameters: dict,
        source: str,
        artifact_type: str = "timeseries",
        units: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Record a data artifact with provenance hashes (existing API)."""
        from ai_hydro.session.store import _hash_obj
        param_hash = _hash_obj(fetch_parameters)
        content_hash = _hash_obj(data)
        self.artifact_manifest[artifact_id] = {
            "artifact_id": artifact_id,
            "type": artifact_type,
            "source": source,
            "fetch_parameters": fetch_parameters,
            "parameter_hash": param_hash,
            "content_hash": content_hash,
            "units": units,
            "created_at": _now(),
            **(metadata or {}),
        }
        return artifact_id

    def record_result(
        self,
        slot: str,
        data: dict,
        uncertainty: dict | None = None,
        artifacts_used: list[str] | None = None,
        metric_ref: str | None = None,
        tool_name: str | None = None,
        *,
        feature_id: str | None = None,
    ) -> None:
        """
        Record a result in a session slot.

        feature_id (keyword-only): if given, stores under that feature id rather
        than __legacy__. New tools (C2+) pass this explicitly; old tools omit it.
        """
        val: dict[str, Any] = {
            "data": data,
            "meta": {
                "computed_at": _now(),
                "tool": tool_name,
                "metric_ref": metric_ref,
            },
        }
        if uncertainty:
            val["uncertainty"] = uncertainty
        if artifacts_used:
            val["artifacts_used"] = artifacts_used

        fid = feature_id or self.active_feature_id or _LEGACY_FEATURE_ID
        self.put_result(slot, fid, _LEGACY_PARAMS_KEY, val)

    def _hash(self, obj: Any) -> str:
        return _hash_obj(obj)

    # ------------------------------------------------------------------
    # Store Protocol — feature registry
    # ------------------------------------------------------------------

    def put_feature(self, feature: Any) -> None:  # feature: aihydro_core Feature
        """Register or update a Feature in the session's feature registry."""
        self._features[feature.feature_id] = feature.to_dict()

    def get_feature(self, feature_id: str) -> Any:  # → Feature | None
        """Look up a Feature by id."""
        d = self._features.get(feature_id)
        if d is None:
            return None
        try:
            from aihydro_core.primitives.geometry import Feature
            return Feature.from_dict(d)
        except Exception:
            return None

    def list_features(self) -> list[Any]:  # → list[Feature]
        """Return all registered features."""
        try:
            from aihydro_core.primitives.geometry import Feature
            return [Feature.from_dict(d) for d in self._features.values()]
        except Exception:
            return []

    def get_active_feature_id(self) -> str | None:
        """Return the active (default) feature id."""
        return self.active_feature_id

    def set_active_feature_id(self, feature_id: str) -> None:
        """Set the active (default) feature id."""
        self.active_feature_id = feature_id

    # ------------------------------------------------------------------
    # Store Protocol — keyed result store
    # ------------------------------------------------------------------

    def put_result(
        self,
        product: str,
        feature_id: str,
        params_key: str,
        value: dict | None,
    ) -> None:
        """Store a result under (product, feature_id, params_key)."""
        self._slots.setdefault(product, {}).setdefault(feature_id, {})[params_key] = value

    def get_result(
        self,
        product: str,
        feature_id: str,
        params_key: str,
    ) -> dict | None:
        """Retrieve a result. Returns None on cache miss."""
        return self._slots.get(product, {}).get(feature_id, {}).get(params_key)

    def list_results(self, product: str) -> dict[str, list[str]]:
        """Return {feature_id: [params_key, ...]} for a product."""
        by_feature = self._slots.get(product, {})
        return {fid: list(by_key.keys()) for fid, by_key in by_feature.items()}

    # ------------------------------------------------------------------
    # Store Protocol — provenance (new API)
    # ------------------------------------------------------------------

    def store_artifact(self, art: Any) -> None:  # art: aihydro_core Artifact
        """Record an Artifact (aihydro_core.primitives.Artifact) in the manifest."""
        self.artifact_manifest[art.artifact_id] = art.to_dict()

    def add_citations(self, keys: list[str]) -> None:
        """Accumulate citation keys (no auto-save — caller must call save())."""
        self._citations.update(keys)

    def commit(self) -> None:
        """Persist state (Store Protocol alias for save())."""
        self.save()

    # ------------------------------------------------------------------
    # Notes with dedup
    # ------------------------------------------------------------------

    def add_note(self, note: str) -> bool:
        note = (note or "").strip()
        if not note:
            return False
        for existing in self.notes[-5:]:
            if existing.strip() == note:
                if self.notes[-1].strip() == note:
                    return False
        if len(self.notes) >= 500:
            self.notes = self.notes[-499:]
        self.notes.append(note)
        return True

    # ------------------------------------------------------------------
    # Backward-compat property accessors for the 9 common slots
    #
    # Getters call self.get() → returns active or __legacy__ result.
    # Setters call self.set() → stores under __legacy__ (old tools).
    # ------------------------------------------------------------------

    @property
    def watershed(self) -> dict | None:
        return self.get("watershed")

    @watershed.setter
    def watershed(self, v: dict | None) -> None:
        self.set("watershed", v)

    @property
    def streamflow(self) -> dict | None:
        return self.get("streamflow")

    @streamflow.setter
    def streamflow(self, v: dict | None) -> None:
        self.set("streamflow", v)

    @property
    def signatures(self) -> dict | None:
        return self.get("signatures")

    @signatures.setter
    def signatures(self, v: dict | None) -> None:
        self.set("signatures", v)

    @property
    def geomorphic(self) -> dict | None:
        return self.get("geomorphic")

    @geomorphic.setter
    def geomorphic(self, v: dict | None) -> None:
        self.set("geomorphic", v)

    @property
    def camels(self) -> dict | None:
        return self.get("camels")

    @camels.setter
    def camels(self, v: dict | None) -> None:
        self.set("camels", v)

    @property
    def forcing(self) -> dict | None:
        return self.get("forcing")

    @forcing.setter
    def forcing(self, v: dict | None) -> None:
        self.set("forcing", v)

    @property
    def twi(self) -> dict | None:
        return self.get("twi")

    @twi.setter
    def twi(self, v: dict | None) -> None:
        self.set("twi", v)

    @property
    def cn(self) -> dict | None:
        return self.get("cn")

    @cn.setter
    def cn(self, v: dict | None) -> None:
        self.set("cn", v)

    @property
    def model(self) -> dict | None:
        return self.get("model")

    @model.setter
    def model(self, v: dict | None) -> None:
        self.set("model", v)

    # ------------------------------------------------------------------
    # Persistence — atomic write-then-rename
    # ------------------------------------------------------------------

    @classmethod
    def _path(cls, session_id: str, shard_id: str | None = None) -> Path:
        if shard_id:
            return _SESSIONS_DIR / f"{session_id}.{shard_id}.shard.json"
        return _SESSIONS_DIR / f"{session_id}.json"

    @classmethod
    def load(cls, session_id: str, shard_id: str | None = None) -> HydroSession:
        """Load an existing session or return a new empty one."""
        path = cls._path(session_id, shard_id)
        if not path.exists():
            return cls(session_id, shard_id)
        try:
            with open(path) as f:
                raw = json.load(f)
        except json.JSONDecodeError as exc:
            log.error("Session file at %s is corrupted: %s", path, exc)
            raise RuntimeError(
                f"Session file '{path}' is corrupted JSON ({exc}). "
                "Affected session_id: " + session_id
            ) from exc

        session = cls(session_id, shard_id)

        # Keys that are stored at the top level but are NOT product slots.
        _META_KEYS = {
            "session_id", "site_name", "site_id", "site_type",
            "workspace_dir", "working_geometry_path", "notes", "created_at",
            "updated_at", "interpretation", "interpretation_at", "archived",
            "_citations", "artifact_manifest", "claims", "assumptions",
            "_site_name_history",
            "gauge_id",          # legacy key
            "_hydro_slots_v2",   # C1 schema version marker
            "_features",         # C1 feature registry
            "active_feature_id", # C1 active feature
        }

        is_v2 = raw.get("_hydro_slots_v2", False)

        for key, val in raw.items():
            if key in _META_KEYS:
                continue
            if isinstance(val, dict) or val is None:
                if is_v2:
                    # New format: already a 3-level dict — load directly
                    session._slots[key] = val if isinstance(val, dict) else {}
                else:
                    # Old format: migrate single-value slot → 3-level
                    # {data, meta} → {__legacy__: {"": {data, meta}}}
                    session._slots[key] = {_LEGACY_FEATURE_ID: {_LEGACY_PARAMS_KEY: val}}

        # Metadata fields
        session.site_name = raw.get("site_name", "")
        session.site_id = raw.get("site_id", "") or raw.get("gauge_id", "")
        session.site_type = raw.get("site_type", "")
        session.workspace_dir = raw.get("workspace_dir")
        session.working_geometry_path = raw.get("working_geometry_path")
        session.notes = raw.get("notes", [])
        session.interpretation = raw.get("interpretation", "")
        session.interpretation_at = raw.get("interpretation_at")
        session.archived = raw.get("archived", False)
        session.created_at = raw.get("created_at", session.created_at)
        session.updated_at = raw.get("updated_at", session.updated_at)
        session._citations = set(raw.get("_citations", []))
        session.artifact_manifest = raw.get("artifact_manifest", {})
        session.claims = raw.get("claims", {})
        session.assumptions = raw.get("assumptions", {})
        session._site_name_history = raw.get("_site_name_history", [])

        # C1: feature registry (absent in old sessions → empty, active → None)
        session._features = raw.get("_features", {})
        session.active_feature_id = raw.get("active_feature_id")

        return session

    def save(self) -> None:
        """Persist atomically (write temp, rename over target)."""
        self.updated_at = _now()
        try:
            for w in self.validate_identity():
                if w.get("severity") in ("warn", "error"):
                    log.warning("[session %s] %s: %s",
                                self.session_id, w.get("code"), w.get("message"))
        except Exception:
            pass
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        target = self._path(self.session_id, self.shard_id)
        tmp = target.with_suffix(
            f"{target.suffix}.tmp.{os.getpid()}.{int(time.time() * 1000)}"
        )
        payload = json.dumps(self._to_raw(), indent=2)
        with open(tmp, "w") as f:
            f.write(payload)
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, target)
        if not self.shard_id:
            try:
                self.write_research_context()
            except Exception as exc:
                log.warning("research.md write failed (session saved OK): %s", exc)

    def archive(self) -> None:
        self.archived = True
        self.save()

    def _to_raw(self) -> dict:
        raw: dict[str, Any] = {
            "session_id": self.session_id,
            "site_name": self.site_name,
            "site_id": self.site_id,
            "site_type": self.site_type,
            "workspace_dir": self.workspace_dir,
            "working_geometry_path": self.working_geometry_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived": self.archived,
            "notes": self.notes,
            "interpretation": self.interpretation,
            "interpretation_at": self.interpretation_at,
            "_citations": sorted(self._citations),
            "artifact_manifest": self.artifact_manifest,
            "claims": self.claims,
            "assumptions": self.assumptions,
            "_site_name_history": self._site_name_history,
            # C1 additions
            "_hydro_slots_v2": True,
            "_features": self._features,
            "active_feature_id": self.active_feature_id,
        }
        # Serialize 3-level slots, applying lean to innermost result dicts
        for slot, by_feature in self._slots.items():
            raw[slot] = _lean_product_slot(by_feature)
        return raw

    # ------------------------------------------------------------------
    # Workspace file writing
    # ------------------------------------------------------------------

    def write_workspace_file(self, filename: str, content: Any) -> str | None:
        if not self.workspace_dir:
            return None
        out_path = Path(self.workspace_dir) / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            if isinstance(content, str):
                f.write(content)
            else:
                json.dump(content, f, indent=2)
        return str(out_path)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def computed(self) -> list[str]:
        """Products that have at least one non-None result across any feature."""
        result = []
        for product, by_feature in self._slots.items():
            if any(
                v is not None
                for by_key in by_feature.values()
                for v in by_key.values()
            ):
                result.append(product)
        return result

    def pending(self) -> list[str]:
        """Common slots not yet computed for the active/legacy feature."""
        return [s for s in _COMMON_SLOTS if self.get(s) is None]

    def is_stale(self, field: str) -> bool:
        if self.archived:
            return True
        if field in self._slots:
            result = self.get(field)
            if not result:
                return False
            ts_str = result.get("meta", {}).get("computed_at")
            if not ts_str:
                return False
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - ts
                return delta.days > STALENESS_THRESHOLD["computed"]
            except Exception:
                return False
        if field == "interpretation":
            if not self.interpretation_at:
                return False
            try:
                ts = datetime.fromisoformat(self.interpretation_at.replace("Z", "+00:00"))
                return (datetime.now(timezone.utc) - ts).days > STALENESS_THRESHOLD["interpretation"]
            except Exception:
                return False
        if field == "notes":
            try:
                ts = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
                return (datetime.now(timezone.utc) - ts).days > STALENESS_THRESHOLD["notes"]
            except Exception:
                return False
        return False

    def summary(self) -> dict:
        warnings = self.validate_identity()
        out: dict[str, Any] = {
            "session_id": self.session_id,
            "site_name": self.site_name,
            "site_id": self.site_id,
            "site_type": self.site_type,
            "canonical_id": self.canonical_id,
            "archived": self.archived,
            "computed": self.computed(),
            "pending": self.pending(),
            "notes": self.notes,
            "has_interpretation": bool(self.interpretation),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        # C1: surface registered features if any exist beyond __legacy__
        real_features = {k: v for k, v in self._features.items()
                         if k != _LEGACY_FEATURE_ID}
        if real_features:
            out["features"] = [
                {"feature_id": k, "name": v.get("name", ""), "source": v.get("source", "")}
                for k, v in real_features.items()
            ]
            out["active_feature_id"] = self.active_feature_id
        if warnings:
            out["identity_warnings"] = warnings
        return out

    def to_json(self) -> str:
        return json.dumps(self._to_raw(), indent=2)

    # ------------------------------------------------------------------
    # Citation management
    # ------------------------------------------------------------------

    def get_citations(self) -> set[str]:
        return set(self._citations)

    def export_bibtex(self) -> str:
        from ai_hydro.citations import build_bibtex
        return build_bibtex(self._citations)

    def cite_all(self) -> str:
        return self.export_bibtex()

    # ------------------------------------------------------------------
    # Synopsis for LLM
    #
    # Single-feature sessions: flat dict (identical to pre-C1 output).
    # Multi-feature sessions: nested by feature_id per slot.
    # ------------------------------------------------------------------

    def synopsis_for_llm(self) -> dict:
        """Concise per-slot summaries for LLM reasoning."""
        out: dict = {}
        for slot in self.computed():
            by_feature = self._slots.get(slot, {})

            # Separate real features from legacy sentinel
            real = {k: v for k, v in by_feature.items() if k != _LEGACY_FEATURE_ID}
            legacy = by_feature.get(_LEGACY_FEATURE_ID, {})

            if real:
                # Multi-feature: nest by feature_id
                out[slot] = {}
                for fid, by_key in real.items():
                    result = _latest_result_in_feature(by_key)
                    if result:
                        name = self._features.get(fid, {}).get("name", fid)
                        key = name if name else fid
                        out[slot][key] = _synopsis_from_result(result)
                # Also include legacy if present
                result = _latest_result_in_feature(legacy)
                if result:
                    out[slot][_LEGACY_FEATURE_ID] = _synopsis_from_result(result)
            else:
                # Single-feature (legacy only): flat, same as before
                result = _latest_result_in_feature(legacy)
                if result:
                    out[slot] = _synopsis_from_result(result)

        return out

    def raw_session_data(self) -> dict:
        return self.synopsis_for_llm()

    # ------------------------------------------------------------------
    # research.md sync
    # ------------------------------------------------------------------

    def write_research_context(self) -> None:
        display = self.site_name or self.site_id or self.session_id
        name_str = self._display_name_str()
        computed = self.computed()
        pending = self.pending()

        lines: list[str] = [
            "# Research Session",
            f"**Session**: {display}{name_str}",
            f"**ID**: {self.session_id}",
        ]
        if self.archived:
            lines += ["> [!IMPORTANT]", "> **This session is ARCHIVED.**", ""]

        if self.site_id:
            lines.append(f"**Site**: {self.site_id}"
                         + (f" ({self.site_type})" if self.site_type else ""))
        lines += [f"**Updated**: {self.updated_at[:10]}", ""]

        # Identity warnings
        warnings = self.validate_identity()
        if warnings:
            lines.append("> [!WARNING]")
            lines.append("> **Identity warnings — investigate before exporting:**")
            for w in warnings:
                lines.append(f"> - `{w['code']}` ({w['severity']}): {w['message']}")
            lines.append("")

        # C1: surface registered features
        real_features = {k: v for k, v in self._features.items()
                         if k != _LEGACY_FEATURE_ID}
        if real_features:
            lines.append("**Registered features** (addressable by id or name):")
            for fid, fd in real_features.items():
                active_marker = " ← active" if fid == self.active_feature_id else ""
                name = fd.get("name", "")
                lines.append(f"  - `{fid}`" + (f" ({name})" if name else "") + active_marker)
            lines.append("")

        active_computed = [s for s in computed if not self.is_stale(s)]
        stale_computed = [s for s in computed if self.is_stale(s)]
        if active_computed:
            lines.append("**Computed (Active)**: " + ", ".join(active_computed))
        if pending:
            lines.append("**Pending**: " + ", ".join(pending))
        lines.append("")

        interp_stale = self.is_stale("interpretation")
        if self.interpretation and not interp_stale:
            lines.append("## Scientific Context (Active)")
            lines.append(self.interpretation)
            lines.append("")

        notes_stale = self.is_stale("notes")
        if self.notes and not notes_stale:
            lines.append("## Researcher Notes (Active)")
            for note in self.notes:
                lines.append(f"- {note}")
            lines.append("")

        if self.archived or stale_computed or (self.interpretation and interp_stale) or (self.notes and notes_stale):
            lines += ["---", "## Historical / Stale Context",
                      "> The following context is older than the staleness threshold.", ""]
            if stale_computed:
                lines.append("**Computed (Stale)**: " + ", ".join(stale_computed))
            if self.interpretation and interp_stale:
                lines += [
                    "### Historical Interpretation",
                    f"*(Authored {self.interpretation_at[:10] if self.interpretation_at else 'unknown'})*",
                    self.interpretation, "",
                ]
            if self.notes and notes_stale:
                lines.append("### Historical Notes")
                for note in self.notes:
                    lines.append(f"- {note}")
                lines.append("")

        try:
            from ai_hydro.session.persona import ResearcherProfile
            profile = ResearcherProfile.load()
            if not profile.is_blank():
                lines.append(profile.to_context_string())
                lines.append("")
        except Exception:
            pass

        if not self.interpretation:
            lines += [
                "_No scientific interpretation yet — call `get_session_raw_state` "
                "then `write_research_interpretation` to generate one._", "",
            ]

        lines.append(
            "> *Skeleton auto-generated by HydroSession. "
            "Scientific context authored by the LLM via `write_research_interpretation`.*"
        )

        base = Path(self.workspace_dir) if self.workspace_dir else _REPO_ROOT
        research_md = base / _RULES_DIR_NAME / "research.md"
        research_md.parent.mkdir(parents=True, exist_ok=True)
        research_md.write_text("\n".join(lines))

    def _display_name_str(self) -> str:
        if self.watershed:
            name = self.watershed.get("data", {}).get("gauge_name", "")
            if name and name not in (self.site_name, self.site_id, self.session_id):
                return f" ({name})"
        return ""


# --------------------------------------------------------------------------- #
# Module-level helpers (used internally and by helpers.py)
# --------------------------------------------------------------------------- #

def _hash_obj(obj: Any) -> str:
    """Deterministic SHA-256 hash of a serialisable object (16-char hex)."""
    import hashlib
    try:
        s = json.dumps(obj, sort_keys=True, default=str)
    except Exception:
        s = str(obj)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]
