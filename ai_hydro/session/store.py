"""
AI-Hydro Research Session (HydroSession)
==========================================

Persistent research state across MCP tool calls.

The primary key is ``session_id`` — any string the researcher or LLM chooses.
It can be a slug ("piscataquis-snowmelt-2020"), a UUID, a USGS gauge number
used as a shorthand ("01031500"), or anything else meaningful to the study.

``site_id`` and ``site_type`` are optional metadata describing the data source
(e.g., a USGS gauge number, GRDC station, DEM tile). They are NOT the session
identity — the session identity is ``session_id``.

Storage: ~/.aihydro/sessions/<session_id>.json

Dynamic slots
-------------
Plugins can register their own result slots without editing core code:
    session.set("my_plugin_result", {...})
    session.get("my_plugin_result")  # → dict or None
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SESSIONS_DIR = Path.home() / ".aihydro" / "sessions"
_SESSIONS_DIR = SESSIONS_DIR # for backward compat within file
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


class HydroSession:
    """Persistent research state for a single study across tool calls."""

    def __init__(self, session_id: str, shard_id: str | None = None) -> None:
        self.session_id: str = session_id
        self.shard_id: str | None = shard_id
        # Display name — LLM-generated slug describing the research
        # e.g. "piscataquis-snowmelt-signatures-2000-2020"
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
        # Platform platform citations are injected at export time, not stored here.
        self._citations: set[str] = set()
        # Provenance: artifact_id -> {type, source, fetch_parameters, parameter_hash, content_hash, ...}
        self.artifact_manifest: dict[str, dict] = {}
        # Scientific Claims: claim_id -> ScientificClaim (dict)
        self.claims: dict[str, dict] = {}
        # Assumptions: assumption_id -> Assumption (dict)
        self.assumptions: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Backward-compat property: gauge_id → site_id (or session_id)
    # Kept so legacy callers that read session.gauge_id still work.
    # ------------------------------------------------------------------

    @property
    def gauge_id(self) -> str:
        """Backward-compat alias — returns site_id if set, else session_id."""
        return self.site_id or self.session_id

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
        """
        Record a scientifically defensible result in a session slot.
        """
        val: dict[str, Any] = {
            "data": data,
            "meta": {
                "computed_at": datetime.now(timezone.utc).isoformat(),
                "tool": tool_name,
                "metric_ref": metric_ref
            }
        }
        if uncertainty:
            # method must be from: [bootstrap, analytical, ensemble, none]
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
    # Persistence
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
        with open(path) as f:
            raw = json.load(f)
        session = cls(session_id, shard_id)
        _META_KEYS = {
            "session_id", "site_name", "site_id", "site_type",
            "workspace_dir", "working_geometry_path", "notes", "created_at", "updated_at", "interpretation",
            "interpretation_at", "archived",
            "_citations", "artifact_manifest", "claims", "assumptions",
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
        return session

    def save(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        with open(self._path(self.session_id, self.shard_id), "w") as f:
            json.dump(self._to_raw(), f, indent=2)
        if not self.shard_id:
            self.write_research_context()

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
        Check if a field or slot is older than its configured threshold.
        If the session is archived, all fields are considered stale for
        active context purposes.
        """
        if self.archived:
            return True

        # 1. Computed slots
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

        # 3. Notes (currently use session updated_at as proxy for list staleness)
        if field == "notes":
            try:
                ts = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - ts
                return delta.days > STALENESS_THRESHOLD["notes"]
            except Exception:
                return False

        return False

    def summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "site_name": self.site_name,
            "site_id": self.site_id,
            "site_type": self.site_type,
            "archived": self.archived,
            "computed": self.computed(),
            "pending": self.pending(),
            "notes": self.notes,
            "has_interpretation": bool(self.interpretation),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

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

        Returned by ``get_session_raw_state`` so the LLM can reason
        across all computed results before writing an interpretation.
        """
        out: dict = {}
        for slot in self.computed():
            result = self.get(slot)
            if not result:
                continue
            data = result.get("data", {})
            meta = result.get("meta", {})
            synopsis: dict = {}
            # Flat scalars and short lists only; strip large arrays
            for k, v in data.items():
                if k.startswith("_"):
                    continue  # private implementation keys
                if isinstance(v, list):
                    if len(v) > _ARRAY_STRIP_THRESHOLD:
                        synopsis[f"{k}_n"] = len(v)
                    else:
                        synopsis[k] = v
                elif isinstance(v, dict):
                    # Keep nested dicts (e.g. attribute_groups, calibrated_params)
                    # but strip any large list values inside them
                    synopsis[k] = {
                        dk: (dv if not (isinstance(dv, list) and
                                        len(dv) > _ARRAY_STRIP_THRESHOLD)
                             else f"[{len(dv)} items]")
                        for dk, dv in v.items()
                    }
                else:
                    synopsis[k] = v
            # Attach lightweight provenance
            computed_at = meta.get("computed_at", "")
            synopsis["_computed_at"] = computed_at[:10] if computed_at else None
            synopsis["_tool"] = meta.get("tool", slot)
            if meta.get("params"):
                synopsis["_params"] = meta["params"]
            out[slot] = synopsis
        return out

    def raw_session_data(self) -> dict:
        """
        Backward-compat alias → synopsis_for_llm().

        Returns lean per-slot summaries (no raw arrays).
        Previously returned the full data dict including arrays;
        callers that needed raw arrays should load from ``_data_file``.
        """
        return self.synopsis_for_llm()

    # ------------------------------------------------------------------
    # research.md sync
    # ------------------------------------------------------------------

    def write_research_context(self) -> None:
        """
        Write research.md to .aihydrorules/ for VS Code context injection.

        Separates ACTIVE context from HISTORICAL (stale/archived) context.
        """
        display = self.site_name or self.site_id or self.session_id
        name_str = self._display_name_str()
        computed = self.computed()
        pending  = self.pending()

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

        # --- Section: Active Computations ---
        active_computed = [s for s in computed if not self.is_stale(s)]
        stale_computed  = [s for s in computed if self.is_stale(s)]

        if active_computed:
            lines.append(f"**Computed (Active)**: " + ", ".join(active_computed))
        if pending:
            lines.append(f"**Pending**: " + ", ".join(pending))
        lines.append("")

        # --- Section: Active Scientific Context ---
        interp_stale = self.is_stale("interpretation")
        if self.interpretation and not interp_stale:
            lines.append("## Scientific Context (Active)")
            lines.append(self.interpretation)
            lines.append("")

        # --- Section: Active Researcher Notes ---
        notes_stale = self.is_stale("notes")
        if self.notes and not notes_stale:
            lines.append("## Researcher Notes (Active)")
            for note in self.notes:
                lines.append(f"- {note}")
            lines.append("")

        # --- Section: Historical / Stale Context ---
        if self.archived or stale_computed or (self.interpretation and interp_stale) or (self.notes and notes_stale):
            lines.append("---")
            lines.append("## Historical / Stale Context")
            lines.append("> The following context is older than the staleness threshold. "
                         "Use with caution.")
            lines.append("")
            
            if stale_computed:
                lines.append(f"**Computed (Stale)**: " + ", ".join(stale_computed))
            
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

        # --- Section: Profile ---
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
