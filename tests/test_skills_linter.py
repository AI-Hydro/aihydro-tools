import pytest
from ai_hydro.skills import registry

def test_skill_linter_filters_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_USER_SKILLS_DIR", tmp_path)
    marketplace = tmp_path / "marketplace"
    (marketplace / "broken").mkdir(parents=True)
    (marketplace / "broken" / "SKILL.md").write_text(
        "---\nname: broken-skill\ndescription: ''\n---\ninvalid fixture\n",
        encoding="utf-8",
    )
    expected = {
        "flood-frequency-analysis",
        "snow-hydrology-trends",
        "ungauged-basin-transcription",
        "drought-indices-calculation",
    }
    for name in expected:
        directory = marketplace / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: fixture\nwhen_to_use: testing\ndomain: hydrology\n---\nfixture\n",
            encoding="utf-8",
        )

    skills = registry.list_skills()
    names = [s["name"] for s in skills]
    
    assert "broken-skill" not in names
    
    # 'flood-frequency-analysis' should be there
    assert "flood-frequency-analysis" in names
    
    # New skills should be there
    assert "snow-hydrology-trends" in names
    assert "ungauged-basin-transcription" in names
    assert "drought-indices-calculation" in names

def test_skill_count(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_USER_SKILLS_DIR", tmp_path)
    marketplace = tmp_path / "marketplace"
    marketplace.mkdir()
    for index in range(8):
        directory = marketplace / f"skill-{index}"
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            f"---\nname: skill-{index}\ndescription: fixture\nwhen_to_use: testing\ndomain: hydrology\n---\nfixture\n",
            encoding="utf-8",
        )

    assert len(registry.list_skills()) == 8
