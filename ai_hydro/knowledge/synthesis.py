"""
Synthesis page frontmatter validator.

Synthesis pages are LLM-proposed, human-reviewed markdown documents that
synthesise findings across multiple sessions or papers. They are distinct
from claims (no evidence binding) and from raw session notes (broader scope).

Every synthesis page must carry an epistemic_status field so the agent and
researchers know how much trust to place in the content.

Allowed statuses:
  stub          — placeholder, minimal content
  evolving      — actively being updated, expect changes
  well_supported — consensus across multiple independent sources
  contested     — active disagreement, handle with care
  deprecated    — superseded; do not use for new analyses
  needs_review  — stale or flagged for human reassessment

Usage:
    from ai_hydro.knowledge.synthesis import validate_synthesis_frontmatter

    result = validate_synthesis_frontmatter(page_content)
    print(result.epistemic_status)  # EpistemicStatus.EVOLVING
"""
from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator


class EpistemicStatus(str, Enum):
    STUB = "stub"
    EVOLVING = "evolving"
    WELL_SUPPORTED = "well_supported"
    CONTESTED = "contested"
    DEPRECATED = "deprecated"
    NEEDS_REVIEW = "needs_review"


class SynthesisFrontmatter(BaseModel):
    """Validated frontmatter for a synthesis markdown page."""
    id: str
    epistemic_status: EpistemicStatus
    source_count: int = Field(ge=0)
    last_updated: str                      # ISO date string (YAML date auto-coerced)
    known_contradictions: int = Field(default=0, ge=0)

    @field_validator("last_updated", mode="before")
    @classmethod
    def coerce_date(cls, v: object) -> str:
        if isinstance(v, date):
            return v.isoformat()
        return str(v)
    open_questions: int = Field(default=0, ge=0)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def validate_synthesis_frontmatter(content: str) -> SynthesisFrontmatter:
    """
    Parse and validate the YAML frontmatter of a synthesis markdown page.

    Raises:
        ValueError: if the page has no frontmatter block.
        pydantic.ValidationError: if required fields are missing or invalid.

    Returns:
        SynthesisFrontmatter: validated frontmatter object.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise ValueError(
            "Synthesis page has no YAML frontmatter block (expected '---' delimiters). "
            "Add a frontmatter block with at least: id, epistemic_status, "
            "source_count, last_updated."
        )
    raw = yaml.safe_load(match.group(1)) or {}
    return SynthesisFrontmatter(**raw)
