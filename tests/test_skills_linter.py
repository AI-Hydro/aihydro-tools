import pytest
from ai_hydro.skills.registry import list_skills

def test_skill_linter_filters_invalid():
    # 'broken-skill' has empty description and missing when_to_use
    skills = list_skills()
    names = [s["name"] for s in skills]
    
    assert "broken-skill" not in names
    
    # 'flood-frequency-analysis' should be there
    assert "flood-frequency-analysis" in names
    
    # New skills should be there
    assert "snow-hydrology-trends" in names
    assert "ungauged-basin-transcription" in names
    assert "drought-indices-calculation" in names

def test_skill_count():
    skills = list_skills()
    # baseflow: 1
    # composition: 3 (batch, watershed, ungauged)
    # frequency: 2 (flood, drought)
    # interpretation: 2 (signature, snow)
    # total should be at least 8 (ignoring modelling for now)
    assert len(skills) >= 8
