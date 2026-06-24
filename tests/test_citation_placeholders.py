from __future__ import annotations

from ai_hydro import citations


def test_production_bibtex_entries_have_no_xxxx_placeholders():
    bad = {
        key: value
        for key, value in citations.BIBTEX_ENTRIES.items()
        if "XXXX" in key or "XXXX" in value
    }

    assert bad == {}


def test_plugin_citation_example_does_not_look_like_unresolved_doi():
    source = citations.__doc__ or ""

    assert "zenodo.XXXX" not in source
    assert "doi = \"add your released Zenodo DOI here\"" in source
