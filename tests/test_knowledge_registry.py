import pytest
from pathlib import Path
from ai_hydro.knowledge.loader import (
    get_registry,
    get_verified_knowledge,
    invalidate_cache,
    validate_workspace_knowledge,
    KnowledgeConflictError,
)
from ai_hydro.knowledge.models import MetricDefinition, VariableDefinition
from ai_hydro.knowledge.synthesis import validate_synthesis_frontmatter, EpistemicStatus

def test_builtin_registry_load():
    invalidate_cache()
    registry = get_registry()
    
    # Check variables
    assert "variable.streamflow" in registry.variables
    assert registry.variables["variable.streamflow"].standard_units == "m3/s"
    
    # Check metrics
    assert "metric.kge" in registry.metrics
    assert registry.metrics["metric.kge"].name == "Kling-Gupta Efficiency"
    assert "gupta_2009" in registry.metrics["metric.kge"].citations
    
    # Check datasets
    assert "dataset.usgs_nwis" in registry.datasets
    assert registry.datasets["dataset.usgs_nwis"].access_tool == "fetch_streamflow_data"

def test_workspace_override(tmp_path):
    # Create a workspace override
    ws_dir = tmp_path / "workspace"
    ws_knowledge = ws_dir / ".aihydrorules" / "knowledge"
    ws_knowledge.mkdir(parents=True)
    
    # Override KGE with a custom reason + scientific_justification (required for verified entries)
    metrics_yaml = ws_knowledge / "metrics.yaml"
    metrics_yaml.write_text("""
- id: metric.kge
  name: "Custom KGE"
  domain: hydrology
  category: model_evaluation
  overrides: metric.kge
  override_reason: "Testing overrides"
  scientific_justification: >
    Using log-transformed KGE per Kling et al. (2012) for high-CV basins.
    This is a test override to verify the override mechanism works correctly.
  inputs:
    observed: {units: m3/s}
    simulated: {units: m3/s}
  outputs:
    kge: {type: float}
  validation: ["Custom rule"]
  citations: []
  related_tools: []
""", encoding="utf-8")

    registry = get_registry(workspace_dir=str(ws_dir))
    assert registry.metrics["metric.kge"].name == "Custom KGE"
    assert registry.metrics["metric.kge"].validation == ["Custom rule"]

_SHADOW_YAML = """
- id: metric.kge
  name: "Shadow KGE"
  domain: hydrology
  category: model_evaluation
  inputs:
    observed: {units: m3/s}
    simulated: {units: m3/s}
  outputs:
    kge: {type: float}
  validation: []
  citations: []
  related_tools: []
"""


def test_workspace_shadow_server_graceful(tmp_path, caplog):
    """get_registry() (server mode) logs conflict and falls back to built-in."""
    ws_dir = tmp_path / "workspace_fail"
    (ws_dir / ".aihydrorules" / "knowledge").mkdir(parents=True)
    (ws_dir / ".aihydrorules" / "knowledge" / "metrics.yaml").write_text(
        _SHADOW_YAML, encoding="utf-8"
    )
    registry = get_registry(workspace_dir=str(ws_dir))
    assert registry.metrics["metric.kge"].name == "Kling-Gupta Efficiency"
    assert "Knowledge conflict" in caplog.text


def test_workspace_shadow_raises_in_strict_mode(tmp_path):
    """validate_workspace_knowledge() raises KnowledgeConflictError on silent shadow."""
    ws_dir = tmp_path / "workspace_strict"
    (ws_dir / ".aihydrorules" / "knowledge").mkdir(parents=True)
    (ws_dir / ".aihydrorules" / "knowledge" / "metrics.yaml").write_text(
        _SHADOW_YAML, encoding="utf-8"
    )
    with pytest.raises(KnowledgeConflictError, match="without 'overrides'"):
        validate_workspace_knowledge(str(ws_dir))


def test_synthesis_frontmatter_valid():
    """Well-formed synthesis page passes validation."""
    page = """\
---
id: synthesis.lstm_vs_hbv
epistemic_status: evolving
source_count: 12
last_updated: 2026-05-07
known_contradictions: 2
open_questions: 4
---

# LSTM vs HBV-light: comparative notes

...
"""
    result = validate_synthesis_frontmatter(page)
    assert result.epistemic_status == EpistemicStatus.EVOLVING
    assert result.source_count == 12


def test_synthesis_frontmatter_missing_status():
    """Synthesis page without epistemic_status fails validation."""
    page = """\
---
id: synthesis.no_status
source_count: 5
last_updated: 2026-05-07
---

# No status declared
"""
    with pytest.raises(Exception, match="epistemic_status"):
        validate_synthesis_frontmatter(page)


def test_synthesis_frontmatter_invalid_status():
    """Unknown epistemic_status value raises ValueError."""
    page = """\
---
id: synthesis.bad_status
epistemic_status: definitely_true
source_count: 3
last_updated: 2026-05-07
---
"""
    with pytest.raises(Exception):
        validate_synthesis_frontmatter(page)


# ---------------------------------------------------------------------------
# aihydro.verified namespace tests (Sprint 3)
# ---------------------------------------------------------------------------

def test_verified_entries_exist():
    """Built-in registry has at least 3 verified entries across categories."""
    invalidate_cache()
    verified = get_verified_knowledge()
    total = sum(len(v) for v in verified.values())
    assert total >= 3, f"Expected ≥3 verified entries, got {total}"
    assert any(e.id == "metric.kge"          for e in verified["metrics"]),   "metric.kge must be verified"
    assert any(e.id == "variable.streamflow"  for e in verified["variables"]), "variable.streamflow must be verified"
    assert any(e.id == "dataset.usgs_nwis"    for e in verified["datasets"]),  "dataset.usgs_nwis must be verified"


def test_verified_entry_override_requires_scientific_justification(tmp_path):
    """Overriding a verified entry without scientific_justification raises KnowledgeConflictError."""
    ws_dir = tmp_path / "workspace_verified"
    (ws_dir / ".aihydrorules" / "knowledge").mkdir(parents=True)
    (ws_dir / ".aihydrorules" / "knowledge" / "metrics.yaml").write_text(
        """
- id: metric.kge
  name: "Custom KGE"
  domain: hydrology
  category: model_evaluation
  overrides: metric.kge
  override_reason: "Testing verified guard"
  inputs:
    observed: {units: m3/s}
    simulated: {units: m3/s}
  outputs:
    kge: {type: float}
  validation: []
  citations: []
  related_tools: []
""",
        encoding="utf-8",
    )
    with pytest.raises(KnowledgeConflictError, match="VERIFIED"):
        validate_workspace_knowledge(str(ws_dir))


def test_verified_entry_override_with_justification_succeeds(tmp_path):
    """Overriding a verified entry WITH scientific_justification is allowed."""
    ws_dir = tmp_path / "workspace_verified_ok"
    (ws_dir / ".aihydrorules" / "knowledge").mkdir(parents=True)
    (ws_dir / ".aihydrorules" / "knowledge" / "metrics.yaml").write_text(
        """
- id: metric.kge
  name: "Modified KGE"
  domain: hydrology
  category: model_evaluation
  overrides: metric.kge
  override_reason: "Using Kling 2012 variant with log-transformed flows"
  scientific_justification: >
    Kling et al. (2012) WRR recommend log-transformed KGE for basins with
    high flow variability (CV > 2). This basin has CV=3.1 (verified in Table 1
    of Kling_2012 supporting data). Override is scientifically justified.
  inputs:
    observed: {units: m3/s}
    simulated: {units: m3/s}
  outputs:
    kge: {type: float}
  validation: []
  citations: [kling_2012]
  related_tools: []
""",
        encoding="utf-8",
    )
    registry = get_registry(workspace_dir=str(ws_dir))
    assert registry.metrics["metric.kge"].name == "Modified KGE"


def test_non_verified_entry_override_does_not_need_justification(tmp_path):
    """Non-verified built-in entries don't require scientific_justification."""
    ws_dir = tmp_path / "workspace_nonverified"
    (ws_dir / ".aihydrorules" / "knowledge").mkdir(parents=True)
    # variable.precipitation is NOT verified → only needs overrides + override_reason
    (ws_dir / ".aihydrorules" / "knowledge" / "variables.yaml").write_text(
        """
- id: variable.precipitation
  symbols: [P]
  names: [precipitation]
  standard_units: mm/day
  common_units: [mm/hr]
  unit_conversions: []
  temporal_support: [daily]
  spatial_support: [gridded]
  overrides: variable.precipitation
  override_reason: "Custom units for this project"
""",
        encoding="utf-8",
    )
    registry = get_registry(workspace_dir=str(ws_dir))
    assert registry.variables["variable.precipitation"].standard_units == "mm/day"
