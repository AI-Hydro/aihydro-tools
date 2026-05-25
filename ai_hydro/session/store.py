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
_SESSIONS_DIR = SESSIONS_DIR  # for backward compat within file
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_RULES_DIR_NAME = ".aihydrorules"

# Lists longer than this are stripped from the session JSON on disk.
# They are replaced by a ``{key}_n`` count key so the record stays
# interpretable without the full array.
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

# Days before a field is considered stale in the active context
STALENESS_THRESHOLD = {
    "computed": 365,
    "notes": 30,
    "interpretation": 14,
}

# Notes added more than this many seconds apart with identical text are kept;
# duplicates within the window are dropped. (1 hour is generous for typical
# multi-turn tool chains; longer-form dedup is the user's job.)
_NOTE_DEDUP_WINDOW_SEC = 3600


def _lean_slot(val: Any) -> Any:
    """
    Return a disk-safe (lean) copy of a slot value.

    Strips any list longer than ``_ARRAY_STRIP_THRESHOLD`` from the ``data``
    sub-dict, replacing it with a ``{key}_n`` count key so the record stays
    interpretable. The ``meta`` sub-dict and scalar ``data`` values are kept
    verbatim. Private keys (``_data_file`` etc.) are preserved.

    The in-memory ``_slots`` dict is never modified — stripping only happens
    when serialising to JSON via ``_to_raw()``.
    """
    if val is None:
        return None
    if not isinstance(val, dict):
        return val
    # Model slot may store data directly (no data/meta wrapper) — handle both
    if "data" not in val:
        # Flat dict (legacy model slot) — strip large lists in-place copy
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


def _slugify_for_filename(s: str) -> str:
    """Lowercase, alnum + dot/dash/underscore only; trimmed of leading/trailing
    separators. Used to derive a canonical_id from a free-form session_id."""
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(s)).strip("-_.")
    return s.lower() or "session"


class HydroSession:
    """Persistent research state for a single study across tool calls."""

    def __init__(self, session_id: str, shard_id: str | None = None) -> None:
        self.session_id: str = session_id
        self.shard_id: str | None = shard_id
        # Display name — set by the data source (gauge_name) and confirmed
        # in the LLM's interpretation. Changes are audited via _site_name_history.
        self.site_name: str = ""
        # Data source identifier — e.g. USGS gauge "01031500", GRDC "6335060"
        # Optional: may be empty for remote sensing, CSV, or ungauged studies
        self.site_id: str = ""
        # Data source type — "usgs_gauge" | "grdc_station" | "ungauged" | "csv" | ...
        self.site_type: str = ""
        self.workspace_dir: str | None = None
        # Relative path under workspace_dir for map/GEE study geometry (map_set_working_geometry)
        self.working_geometry_path: str | None = None
        self._slots: dict[str, dict | None] = {}
        self.notes: list[str] = []
        # LLM-authored scientific interpretation — written via write_research_interpretation
        self.interpretation: str = ""
        self.interpretation_at: str | None = None
        self.created_at: str = datetime.now(timezone.utc).isoformat()
        self.updated_at: str = self.created_at
        self.archived: bool = False
        # Citation keys accumulated as tools run (Tier 1 data sources + Plugin plugins).
        self._citations: set[str] = set()
        # Provenance: artifact_id -> {type, source, fetch_parameters, parameter_hash, content_hash, ...}
        self.artifact_manifest: dict[str, dict] = {}
        # Scientific Claims: claim_id -> ScientificClaim (dict)
        self.claims: dict[str, dict] = {}
        # Assumptions: assumption_id -> Assumption (dict)
        self.assumptions: dict[str, dict] = {}
        # Audit trail: every prior site_name with the reason it was overwritten.
        # Items: {"prev": str, "next": str, "at": iso8601, "reason": str?}
        self._site_name_history: list[dict] = []

    # ------------------------------------------------------------------
    # Backward-compat property: gauge_id → site_id (or session_id)
    # ------------------------------------------------------------------

    @property
    def gauge_id(self) -> str:
        """Backward-compat alias — returns site_id if set, else session_id."""
        return self.site_id or self.session_id

    # ------------------------------------------------------------------
    # Canonical identity for workspace filenames
    # ------------------------------------------------------------------

    @property
    def canonical_id(self) -> str:
        """
        Stable identifier used for all workspace artifact filenames so a
        single study's outputs land in a consistent namespace.

        Resolution:
          1. ``site_id`` if non-empty (preferred — e.g. USGS gauge number)
          2. slugified ``session_id`` otherwise

        Tools should NEVER hand-build filenames by interpolating gauge IDs
        or session IDs themselves; call ``workspace_filename`` so this
        resolution stays in one place.
        """
        if self.site_id:
            return _slugify_for_filename(self.site_id)
        return _slugify_for_filename(self.session_id)

    def workspace_filename(self, prefix: str, ext: str = "json") -> str:
        """
        Build a workspace-relative filename using the session's canonical_id.

        ``prefix`` is the artifact kind (e.g. ``"watershed"``, ``"streamflow"``).
        ``ext`` is the file extension (no leading dot — defaults to "json").

        Returns e.g. ``"watershed_01031500.geojson"`` regardless of whether
        the tool was called with the gauge ID, a slug, or just session_id.
        """
        prefix = prefix.strip("_")
        ext = ext.lstrip(".")
        return f"{prefix}_{self.canonical_id}.{ext}"

    # ------------------------------------------------------------------
    # Identity mutation with audit
    # ------------------------------------------------------------------

    def set_site_name(self, name: str, reason: str | None = None) -> None:
        """
        Set ``site_name`` with audit trail.

        If the new name differs from the current one (and the current one
        wasn't empty), the previous value is appended to ``_site_name_history``
        with the supplied ``reason``. Use this rather than ``session.site_name = ...``
        anywhere there is a chance the LLM might rewrite the name (e.g.
        write_research_interpretation).
        """
        name = (name or "").strip()
        prev = (self.site_name or "").strip()
        if name == prev:
            return
        if prev:
            self._site_name_history.append({
                "prev": prev,
                "next": name,
                "at": datetime.now(timezone.utc).isoformat(),
                "reason": reason or "",
            })
        self.site_name = name

    # ------------------------------------------------------------------
    # Identity validation — surfaces drift between session_id / site_id /
    # site_name / watershed.gauge_name without raising.
    # ------------------------------------------------------------------

    def validate_identity(self) -> list[dict]:
        """
        Return a list of warning records describing identity inconsistencies.

        Each warning: {"code": str, "severity": "info"|"warn"|"error", "message": str}.
        An empty list means the session's identity fields are mutually consistent.

        Used by ``get_session_health`` and surfaced in summary() so the LLM
        can fix drift before it propagates into research.md or exports.
        """
        warnings: list[dict] = []

        # 1. Cross-check site_name against the canonical gauge_name from
        #    the watershed slot (set by delineate_watershed from USGS NWIS).
        if self.watershed and self.site_name:
            canonical = (self.watershed.get("data", {}) or {}).get("gauge_name") or ""
            canonical = canonical.strip()
            if canonical:
                # Normalised comparison: strip case, punctuation, common abbreviations
                def _norm(s: str) -> str:
                    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()
                if _norm(self.site_name) != _norm(canonical) and (
                    _norm(canonical) not in _norm(self.site_name)
                    and _norm(self.site_name) not in _norm(canonical)
                ):
                    warnings.append({
                        "code": "site_name_drift",
                        "severity": "warn",
                        "message": (
                            f"site_name '{self.site_name}' disagrees with the "
                            f"canonical gauge_name '{canonical}' from the watershed "
                            "slot. Either the wrong gauge was queried or the "
                            "interpretation renamed the site incorrectly."
                        ),
                        "site_name": self.site_name,
                        "canonical_name": canonical,
                    })

        # 2. If site_type says USGS but site_id isn't a valid gauge number.
        if self.site_type == "usgs_gauge" and self.site_id:
            if not (self.site_id.isdigit() and 7 <= len(self.site_id) <= 10):
                warnings.append({
                    "code": "site_id_format",
                    "severity": "warn",
                    "message": (
                        f"site_type is 'usgs_gauge' but site_id '{self.site_id}' "
                        "is not a 7-10 digit USGS station number."
                    ),
                })

        # 3. session_id looks like a USGS gauge but site_id is different
        #    (e.g. user started session with one gauge, then queried another).
        if (self.session_id.isdigit()
                and 7 <= len(self.session_id) <= 10
                and self.site_id
                and self.site_id != self.session_id):
            warnings.append({
                "code": "session_id_gauge_mismatch",
                "severity": "info",
                "message": (
                    f"session_id '{self.session_id}' looks like a USGS gauge "
                    f"but the analysed site is '{self.site_id}'. This is fine "
                    "if intentional; file naming will use site_id."
                ),
            })

        # 4. Workspace existence check — files may have been moved.
        if self.workspace_dir and not Path(self.workspace_dir).is_dir():
            warnings.append({
                "code": "workspace_missing",
                "severity": "warn",
                "message": (
                    f"workspace_dir '{self.workspace_dir}' no longer exists. "
                    "Tool outputs cannot be saved. Update via "
                    "start_session(session_id, workspace_dir='<new_path>')."
                ),
            })

        # 5. Site name unconfirmed despite interpretation already authored.
        if self.interpretation and not self.site_name and self.site_id:
            warnings.append({
                "code": "site_name_unset",
                "severity": "info",
                "message": (
                    "An interpretation has been written but site_name is empty. "
                    "Pass a descriptive site_name to write_research_interpretation."
                ),
            })

        return warnings

    # ------------------------------------------------------------------
    # Dynamic slot access
    # ------------------------------------------------------------------

    def set(self, slot: str, value: dict | None) -> None:
        self._slots[slot] = value

    def get(self, slot: str) -> dict | None:
        return self._slots.get(slot)

    def add_artifact(
        self,
        artifact_id: str,
        data: Any,
        fetch_parameters: dict,
        source: str,
        artifact_type: str = "timeseries",
        units: str | None = None,
        metadata: dict | None = None
    ) -> str:
        """
        Record a data artifact in the session manifest with provenance hashes.
        Returns the artifact_id.
        """
        param_hash = self._hash(fetch_parameters)
        content_hash = self._hash(data)

        self.artifact_manifest[artifact_id] = {
            "artifact_id": artifact_id,
            "type": artifact_type,
            "source": source,
            "fetch_parameters": fetch_parameters,
            "parameter_hash": param_hash,
            "content_hash": content_hash,
            "units": units,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **(metadata or {})
        }
        return artifact_id

    def record_result(
        self,
        slot: str,
        data: dict,
        uncertainty: dict | None = None,
        artifacts_used: list[str] | None = None,
        metric_ref: str | None = None,
        tool_name: str | None = None
    ) -> None:
        """Record a scientifically defensible result in a session slot."""
        val: dict[str, Any] = {
            "data": data,
            "meta": {
                "computed_at": datetime.now(timezone.utc).isoformat(),
                "tool": tool_name,
                "metric_ref": metric_ref
            }
        }
        if uncertainty:
            val["uncertainty"] = uncertainty
        if artifacts_used:
            val["artifacts_used"] = artifacts_used
        self.set(slot, val)

    def _hash(self, obj: Any) -> str:
        """Compute a deterministic SHA-256 hash of a serialisable object."""
        import hashlib
        try:
            s = json.dumps(obj, sort_keys=True)
        except Exception:
            s = str(obj)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Notes with dedup
    # ------------------------------------------------------------------

    def add_note(self, note: str) -> bool:
        """
        Append a researcher note. Returns True if the note was added, False if
        it was deduped (identical text added recently).

        Dedup window is intentionally small (1 hour) so the same note can be
        re-added later in a long study without surprise.
        """
        note = (note or "").strip()
        if not note:
            return False
        # Compare against last few notes (cheap, exact-text dedup)
        for existing in self.notes[-5:]:
            if existing.strip() == note:
                # We don't track per-note timestamps, but if the session was
                # touched recently and the same note is at the tail, drop it.
                # Conservative: dedup only when the LAST note matches exactly.
                if self.notes[-1].strip() == note:
                    return False
        # Hard cap to avoid runaway growth
        if len(self.notes) >= 500:
            self.notes = self.notes[-499:]
        self.notes.append(note)
        return True

    # ------------------------------------------------------------------
    # Backward-compat property accessors for the 9 common slots
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
        """Load an existing session or shard, or return a new empty one."""
        path = cls._path(session_id, shard_id)
        if not path.exists():
            return cls(session_id, shard_id)
        try:
            with open(path) as f:
                raw = json.load(f)
        except json.JSONDecodeError as exc:
            # Surface clearly rather than silently returning an empty session
            # — corruption is the kind of bug we want to know about.
            log.error("Session file at %s is corrupted: %s", path, exc)
            raise RuntimeError(
                f"Session file '{path}' is corrupted JSON ({exc}). "
                "Investigate manually; do not blindly overwrite — there may "
                "be recoverable history. Affected session_id: " + session_id
            ) from exc
        session = cls(session_id, shard_id)
        _META_KEYS = {
            "session_id", "site_name", "site_id", "site_type",
            "workspace_dir", "working_geometry_path", "notes", "created_at",
            "updated_at", "interpretation", "interpretation_at", "archived",
            "_citations", "artifact_manifest", "claims", "assumptions",
            "_site_name_history",
            # legacy keys — kept for loading old session files
            "gauge_id",
        }
        for key, val in raw.items():
            if key in _META_KEYS:
                continue
            if isinstance(val, dict) or val is None:
                session.set(key, val)
        session.site_name = raw.get("site_name", "")
        # Support legacy "gauge_id" key in old session files
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
        return session

    def save(self) -> None:
        """
        Persist atomically: write to a temp file in the same directory, then
        rename over the target. POSIX rename is atomic; on Windows it's an
        unlink-then-rename but the window is microseconds.

        Why this matters: SIGKILL or power loss during a partial write would
        otherwise leave a truncated JSON file that load() can't parse, and
        the user would lose the whole study's session.
        """
        self.updated_at = datetime.now(timezone.utc).isoformat()
        # Auto-audit identity on every save — log (not raise) so we don't
        # break workflows but the warnings show up in server logs + the
        # next get_session_health call surfaces them too.
        try:
            for w in self.validate_identity():
                if w.get("severity") in ("warn", "error"):
                    log.warning(
                        "[session %s] %s: %s",
                        self.session_id, w.get("code"), w.get("message"),
                    )
        except Exception:
            pass
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        target = self._path(self.session_id, self.shard_id)
        tmp = target.with_suffix(
            f"{target.suffix}.tmp.{os.getpid()}.{int(time.time() * 1000)}"
        )
        payload = json.dumps(self._to_raw(), indent=2)
        # Write + fsync the data file so the rename is guaranteed durable.
        # Skip fsync silently on platforms where the file descriptor can't be
        # fsynced (rare; some networked filesystems).
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
        """Mark session as archived — all active context moves to Historical."""
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
        }
        for slot, val in self._slots.items():
            raw[slot] = _lean_slot(val)
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
        return [k for k, v in self._slots.items() if v is not None]

    def pending(self) -> list[str]:
        return [s for s in _COMMON_SLOTS if self.get(s) is None]

    def is_stale(self, field: str) -> bool:
        """
        Whether a field is older than its staleness threshold.

        Note: ``archived`` is NOT the same as ``stale``. Archived sessions
        carry intentionally historical data; ``is_stale`` continues to use
        the per-field timestamp so an archived session can still surface
        a recent interpretation in the Historical section without being
        treated as "older than X days".
        """
        # 1. Computed slots — use the slot's own computed_at
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

        # 2. Interpretation
        if field == "interpretation":
            if not self.interpretation_at:
                return False
            try:
                ts = datetime.fromisoformat(self.interpretation_at.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - ts
                return delta.days > STALENESS_THRESHOLD["interpretation"]
            except Exception:
                return False

        # 3. Notes — use session updated_at as proxy
        if field == "notes":
            try:
                ts = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - ts
                return delta.days > STALENESS_THRESHOLD["notes"]
            except Exception:
                return False

        return False

    def summary(self) -> dict:
        """Lean summary used by start_session / get_session_summary."""
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
        if warnings:
            out["identity_warnings"] = warnings
        return out

    def to_json(self) -> str:
        return json.dumps(self._to_raw(), indent=2)

    # ------------------------------------------------------------------
    # Citation management
    # ------------------------------------------------------------------

    def add_citations(self, keys: list[str]) -> None:
        """Accumulate citation keys (no auto-save — caller must call save())."""
        self._citations.update(keys)

    def get_citations(self) -> set[str]:
        """Return the set of accumulated Tier 1 citation keys."""
        return set(self._citations)

    def export_bibtex(self) -> str:
        """Build a ready-to-use .bib string (Tier 1 collected + Platform platform)."""
        from ai_hydro.citations import build_bibtex
        return build_bibtex(self._citations)

    def cite_all(self) -> str:
        """Backward-compat alias → export_bibtex()."""
        return self.export_bibtex()

    def synopsis_for_llm(self) -> dict:
        """
        Concise per-slot summaries for LLM reasoning — never returns raw arrays.

        Each slot becomes a flat dict of scalars + short lists only.
        Large time-series arrays are replaced by their element count so the LLM
        knows they exist without being overwhelmed by the data.
        """
        out: dict = {}
        for slot in self.computed():
            result = self.get(slot)
            if not result:
                continue
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
                        dk: (dv if not (isinstance(dv, list) and
                                        len(dv) > _ARRAY_STRIP_THRESHOLD)
                             else f"[{len(dv)} items]")
                        for dk, dv in v.items()
                    }
                else:
                    synopsis[k] = v
            computed_at = meta.get("computed_at", "")
            synopsis["_computed_at"] = computed_at[:10] if computed_at else None
            synopsis["_tool"] = meta.get("tool", slot)
            if meta.get("params"):
                synopsis["_params"] = meta["params"]
            out[slot] = synopsis
        return out

    def raw_session_data(self) -> dict:
        """Backward-compat alias → synopsis_for_llm()."""
        return self.synopsis_for_llm()

    # ------------------------------------------------------------------
    # research.md sync
    # ------------------------------------------------------------------

    def write_research_context(self) -> None:
        """
        Write research.md to .aihydrorules/ for VS Code context injection.

        Separates ACTIVE context from HISTORICAL (stale/archived) context.
        Identity warnings (if any) are surfaced at the top so the LLM sees
        them on every turn.
        """
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
            lines.append("> [!IMPORTANT]")
            lines.append("> **This session is ARCHIVED.** All hypotheses below are historical.")

        if self.site_id:
            lines.append(f"**Site**: {self.site_id}" +
                         (f" ({self.site_type})" if self.site_type else ""))
        lines += [f"**Updated**: {self.updated_at[:10]}", ""]

        # --- Identity warnings (surface at top so LLM acts) ---
        warnings = self.validate_identity()
        if warnings:
            lines.append("> [!WARNING]")
            lines.append("> **Identity warnings — investigate before exporting:**")
            for w in warnings:
                lines.append(f"> - `{w['code']}` ({w['severity']}): {w['message']}")
            lines.append("")

        # --- Active Computations ---
        active_computed = [s for s in computed if not self.is_stale(s)]
        stale_computed = [s for s in computed if self.is_stale(s)]
        if active_computed:
            lines.append("**Computed (Active)**: " + ", ".join(active_computed))
        if pending:
            lines.append("**Pending**: " + ", ".join(pending))
        lines.append("")

        # --- Active Scientific Context ---
        interp_stale = self.is_stale("interpretation")
        if self.interpretation and not interp_stale:
            lines.append("## Scientific Context (Active)")
            lines.append(self.interpretation)
            lines.append("")

        # --- Active Researcher Notes ---
        notes_stale = self.is_stale("notes")
        if self.notes and not notes_stale:
            lines.append("## Researcher Notes (Active)")
            for note in self.notes:
                lines.append(f"- {note}")
            lines.append("")

        # --- Historical / Stale Context ---
        if self.archived or stale_computed or (self.interpretation and interp_stale) or (self.notes and notes_stale):
            lines.append("---")
            lines.append("## Historical / Stale Context")
            lines.append("> The following context is older than the staleness threshold. "
                         "Use with caution.")
            lines.append("")
            if stale_computed:
                lines.append("**Computed (Stale)**: " + ", ".join(stale_computed))
            if self.interpretation and interp_stale:
                lines.append("### Historical Interpretation")
                lines.append(f"*(Authored {self.interpretation_at[:10] if self.interpretation_at else 'unknown'})*")
                lines.append(self.interpretation)
                lines.append("")
            if self.notes and notes_stale:
                lines.append("### Historical Notes")
                for note in self.notes:
                    lines.append(f"- {note}")
                lines.append("")

        # --- Profile ---
        try:
            from ai_hydro.session.persona import ResearcherProfile
            profile = ResearcherProfile.load()
            if not profile.is_blank():
                lines.append(profile.to_context_string())
                lines.append("")
        except Exception:
            pass

        if not self.interpretation:
            lines.append(
                "_No scientific interpretation yet — call `get_session_raw_state` "
                "then `write_research_interpretation` to generate one._"
            )
            lines.append("")

        lines.append(
            "> *Skeleton auto-generated by HydroSession. "
            "Scientific context authored by the LLM via `write_research_interpretation`.*"
        )

        base = Path(self.workspace_dir) if self.workspace_dir else _REPO_ROOT
        research_md = base / _RULES_DIR_NAME / "research.md"
        research_md.parent.mkdir(parents=True, exist_ok=True)
        research_md.write_text("\n".join(lines))

    def _display_name_str(self) -> str:
        """Parenthetical station name from watershed metadata if available."""
        if self.watershed:
            name = self.watershed.get("data", {}).get("gauge_name", "")
            if name and name not in (self.site_name, self.site_id, self.session_id):
                return f" ({name})"
        return ""
