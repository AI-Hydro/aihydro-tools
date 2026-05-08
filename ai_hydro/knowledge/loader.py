"""
Knowledge registry loader — 3-tier discovery with validation.

Priority order (highest to lowest):
  1. Session memory (claims, assumptions, artifacts — handled by HydroSession)
  2. Workspace/project knowledge (.aihydrorules/knowledge/)
  3. Built-in package knowledge (ai_hydro/knowledge/)

Workspace overrides MUST include 'overrides' + 'override_reason' fields.
Workspace objects using a built-in ID without these fields raise
KnowledgeConflictError. The MCP server catches and logs this to avoid
crashing on startup; CI and tests should call validate_workspace_knowledge()
which lets the error propagate.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from ai_hydro.knowledge.models import (
    DatasetDefinition,
    EquationDefinition,
    KnowledgeRegistry,
    MetricDefinition,
    VariableDefinition,
)

log = logging.getLogger("ai_hydro.knowledge")


class KnowledgeConflictError(ValueError):
    """
    Raised when a workspace knowledge object silently shadows a built-in ID
    without declaring 'overrides' + 'override_reason'.

    Silent shadowing is forbidden because it can cause the agent to operate
    on a researcher-modified definition without knowing it has deviated from
    the curated built-in. Require explicit declaration so deviations are
    visible, reviewable, and intentional.

    Resolution: add to the workspace YAML entry:
        overrides: <built-in-id>
        override_reason: "<one sentence explaining the deviation>"
    """


_KNOWLEDGE_DIR = Path(__file__).parent
_WORKSPACE_SUBPATH = Path(".aihydrorules") / "knowledge"

# Map file basename → (schema class, registry attribute)
_FILE_REGISTRY: dict[str, tuple[type, str]] = {
    "variables": (VariableDefinition, "variables"),
    "metrics": (MetricDefinition, "metrics"),
    "datasets": (DatasetDefinition, "datasets"),
    "equations": (EquationDefinition, "equations"),
}


def _load_yaml_file(path: Path) -> list[dict]:
    """Load a YAML file and return a list of dicts. Returns [] on any error."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            log.warning("Knowledge file %s must be a YAML list. Skipping.", path)
            return []
        return raw
    except Exception as exc:
        log.error("Failed to load knowledge file %s: %s", path, exc)
        return []


def _parse_objects(
    entries: list[dict],
    schema: type,
    source_label: str,
    built_in_ids: set[str],
    is_workspace: bool = False,
    strict: bool = False,
    built_in_objects: dict[str, object] | None = None,
) -> dict[str, object]:
    """
    Validate and parse a list of raw dicts against a Pydantic schema.

    strict=True: KnowledgeConflictError propagates (use in CI / validate_workspace_knowledge).
    strict=False (default): conflict is logged and the entry is skipped (server stays up).

    built_in_objects: parsed built-in objects, used to detect verified entries and
    apply the escalated override guard (requires 'scientific_justification').
    """
    result: dict[str, object] = {}
    for raw in entries:
        obj_id = raw.get("id", "<unknown>")
        try:
            # Override guard: workspace objects may not silently shadow built-ins.
            if is_workspace and obj_id in built_in_ids:
                if not (raw.get("overrides") and raw.get("override_reason")):
                    msg = (
                        f"Workspace knowledge object '{obj_id}' shadows a built-in ID "
                        f"without 'overrides' + 'override_reason' fields. "
                        f"Add both fields to declare this deviation intentional, "
                        f"or remove the entry. See DESIGN_PRINCIPLES.md §Override policy."
                    )
                    raise KnowledgeConflictError(msg)

                # Escalated guard for verified entries
                if built_in_objects:
                    built_in_obj = built_in_objects.get(obj_id)
                    if getattr(built_in_obj, "verified", False):
                        if not raw.get("scientific_justification"):
                            msg = (
                                f"Workspace knowledge object '{obj_id}' overrides a "
                                f"VERIFIED built-in entry. Verified entries require "
                                f"'scientific_justification' (a peer-reviewed reference "
                                f"supporting the deviation) in addition to "
                                f"'overrides' + 'override_reason'. "
                                f"See DESIGN_PRINCIPLES.md §Override policy §Verified entries."
                            )
                            raise KnowledgeConflictError(msg)

            obj = schema(**raw)
            result[obj_id] = obj

        except KnowledgeConflictError:
            if strict:
                raise
            log.error(
                "Knowledge conflict on '%s' from %s — entry skipped. "
                "Run validate_workspace_knowledge() for the full error.",
                obj_id, source_label,
            )
        except ValidationError as exc:
            log.warning(
                "Knowledge object '%s' from %s failed schema validation (%d error(s)). Skipping.",
                obj_id, source_label, exc.error_count(),
            )
        except Exception as exc:
            log.warning("Failed to parse knowledge object '%s': %s", obj_id, exc)

    return result


def _build_registry(
    workspace_dir: str | None = None,
    strict: bool = False,
) -> KnowledgeRegistry:
    """
    Build the full knowledge registry from all tiers.

    Tier 3 (built-in) loaded first; Tier 2 (workspace) overrides where declared.
    Override guard enforced for workspace objects that shadow built-in IDs.

    strict=True raises KnowledgeConflictError on silent shadowing (CI mode).
    strict=False logs and skips conflicts so the server keeps running (default).
    """
    registry = KnowledgeRegistry()

    for filename, (schema, attr) in _FILE_REGISTRY.items():
        built_in_path = _KNOWLEDGE_DIR / f"{filename}.yaml"
        built_in_entries: dict[str, object] = {}

        if built_in_path.exists():
            raw = _load_yaml_file(built_in_path)
            built_in_entries = _parse_objects(
                raw, schema, f"built-in/{filename}.yaml",
                set(), is_workspace=False, strict=False,
            )
            getattr(registry, attr).update(built_in_entries)

        # Workspace tier
        if workspace_dir:
            ws_path = Path(workspace_dir) / _WORKSPACE_SUBPATH / f"{filename}.yaml"
            if ws_path.exists():
                raw = _load_yaml_file(ws_path)
                ws_entries = _parse_objects(
                    raw, schema, f"workspace/{filename}.yaml",
                    set(built_in_entries.keys()), is_workspace=True, strict=strict,
                    built_in_objects=built_in_entries,
                )
                getattr(registry, attr).update(ws_entries)

    return registry


# Module-level cached registry for the built-in tier only
@lru_cache(maxsize=1)
def get_builtin_registry() -> KnowledgeRegistry:
    """Return the cached built-in knowledge registry (no workspace layer)."""
    return _build_registry(workspace_dir=None)


def get_registry(workspace_dir: str | None = None) -> KnowledgeRegistry:
    """
    Return a knowledge registry, optionally merged with workspace overrides.

    If workspace_dir is None, returns the cached built-in registry.
    If workspace_dir is provided, always builds fresh (not cached) to pick up changes.
    """
    if workspace_dir is None:
        return get_builtin_registry()
    return _build_registry(workspace_dir=workspace_dir)


def validate_workspace_knowledge(workspace_dir: str) -> None:
    """
    Validate all workspace knowledge files and raise KnowledgeConflictError
    on any silent built-in shadowing.

    Call this in CI, pre-commit hooks, or aihydro-bench to catch conflicts
    before they silently corrupt the running registry. The MCP server itself
    uses strict=False (log + skip) so it never crashes on startup.

    Raises:
        KnowledgeConflictError: if any workspace entry shadows a built-in ID
            without 'overrides' + 'override_reason' declared.
    """
    _build_registry(workspace_dir=workspace_dir, strict=True)


def get_verified_knowledge() -> dict[str, list[object]]:
    """
    Return all verified built-in knowledge entries, grouped by category.

    Verified entries have passed peer review and carry an escalated workspace
    override guard (require 'scientific_justification' in addition to the
    standard 'overrides' + 'override_reason').

    Returns:
        dict with keys 'variables', 'metrics', 'datasets', 'equations',
        each mapping to a list of verified objects.
    """
    registry = get_builtin_registry()
    return {
        "variables": [v for v in registry.variables.values() if getattr(v, "verified", False)],
        "metrics":   [m for m in registry.metrics.values()   if getattr(m, "verified", False)],
        "datasets":  [d for d in registry.datasets.values()  if getattr(d, "verified", False)],
        "equations": [e for e in registry.equations.values() if getattr(e, "verified", False)],
    }


def invalidate_cache() -> None:
    """Clear the built-in registry cache. Used by tests and hot-reload scenarios."""
    get_builtin_registry.cache_clear()
