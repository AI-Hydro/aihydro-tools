"""Citation lookup integrity (R-05, R-32). Offline: the single HTTP seam ``_http_fetch`` is replaced.

The contract these tests hold: a DOI is resolved at the DOI endpoint and the returned DOI must equal the
one asked for; a weak free-text hit is a candidate, never a citation; and a provider that could not be
reached is reported as ``unavailable``, never as ``not_found``.
"""
from __future__ import annotations

import pytest

from ai_hydro.mcp import tools_citations as tc

REAL = "10.1061/(asce)0733-9496(1994)120:4(485)"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_CACHE_DIR", tmp_path / "citations")


class Net:
    """Scripted network: ``routes`` maps a URL substring to ("ok", body) | "404" | "down"; first match wins."""

    def __init__(self, routes):
        self.routes, self.urls = routes, []

    def __call__(self, url):
        self.urls.append(url)
        for frag, resp in self.routes:
            if frag in url:
                if resp == "404":
                    return tc.NOT_FOUND, None
                if resp == "down":
                    return tc.UNAVAILABLE, None
                return "ok", resp
        return tc.UNAVAILABLE, None


def crossref_item(doi, title="Flow-duration curves I: review and use"):
    return {"message": {"DOI": doi.upper(), "title": [title], "author": [{"family": "Vogel", "given": "R"}],
                        "issued": {"date-parts": [[1994]]}, "container-title": ["J. Water Resour. Plann. Manage."],
                        "volume": "120", "page": "485-504"}}


def test_a_doi_that_does_not_exist_is_not_found_at_the_doi_endpoint_not_by_search(monkeypatch):
    net = Net([("api.crossref.org/works/10.9999", "404"), ("api.datacite.org/dois/10.9999", "404"),
               ("semanticscholar.org/graph/v1/paper/DOI:10.9999", "404"),
               # the old behaviour: a free-text search that would return an unrelated paper
               ("api.crossref.org/works?query=", {"message": {"items": [
                   {"DOI": "10.1016/j.real.2001.01.001", "title": ["Unrelated paper about soil microbes"]}]}})])
    monkeypatch.setattr(tc, "_http_fetch", net)
    out = tc.get_citation_by_doi("10.9999/this.doi.does.not.exist")
    assert out.get("not_found") is True and "doi" not in out
    assert not out.get("unavailable")
    assert all("?query=" not in u for u in net.urls), net.urls
    assert any("/works/10.9999/this.doi.does.not.exist" in u for u in net.urls)


def test_a_different_doi_in_the_response_is_never_returned(monkeypatch):
    monkeypatch.setattr(tc, "_http_fetch", Net([
        ("api.crossref.org/works/10.1000", crossref_item("10.5555/another.paper")),
        ("api.datacite.org", "404"), ("semanticscholar", "404")]))
    out = tc.get_citation_by_doi("10.1000/wanted")
    assert out.get("not_found") is True and "doi" not in out


def test_a_matching_doi_is_returned_cached_and_case_insensitive(monkeypatch):
    net = Net([("api.crossref.org/works/", crossref_item(REAL))])
    monkeypatch.setattr(tc, "_http_fetch", net)
    out = tc.get_citation_by_doi("https://doi.org/" + REAL.upper())
    assert out["doi"] == REAL and out["confidence"] == "high" and out["from_cache"] is False
    assert "Vogel" in out["formatted_apa"]
    n = len(net.urls)
    again = tc.get_citation_by_doi(REAL)
    assert again["from_cache"] is True and again["doi"] == REAL and len(net.urls) == n


def test_network_failure_is_unavailable_never_not_found_for_a_doi(monkeypatch):
    monkeypatch.setattr(tc, "_http_fetch", Net([]))                   # every call "down"
    out = tc.get_citation_by_doi(REAL)
    assert out["unavailable"] is True and out["not_found"] is False and out["code"] == "CITATION_LOOKUP_UNAVAILABLE"
    assert "does NOT mean no citation exists" in out["message"] and "not_found" not in out.get("_note", "")
    assert set(out["providers"].values()) == {tc.UNAVAILABLE}


def test_network_failure_is_unavailable_never_not_found_for_free_text(monkeypatch):
    monkeypatch.setattr(tc, "_http_fetch", Net([]))
    out = tc.lookup_citation("Vogel Fennessey flow duration curves review")
    assert out["unavailable"] is True and out["not_found"] is False


def test_one_provider_down_and_the_rest_not_found_is_still_unavailable(monkeypatch):
    monkeypatch.setattr(tc, "_http_fetch", Net([
        ("api.crossref.org", "404"), ("api.datacite.org", "down"), ("semanticscholar", "404")]))
    out = tc.get_citation_by_doi("10.9999/x.y")
    assert out["unavailable"] is True and out["providers"]["datacite"] == tc.UNAVAILABLE


def test_a_provider_that_finds_it_wins_even_if_another_is_down(monkeypatch):
    monkeypatch.setattr(tc, "_http_fetch", Net([("api.crossref.org/works/", "down"),
                                                  ("api.datacite.org", "down"),
                                                  ("semanticscholar.org/graph/v1/paper/DOI:", {
                                                      "title": "Flow-duration curves", "year": 1994,
                                                      "externalIds": {"DOI": REAL}, "authors": [{"name": "R Vogel"}],
                                                      "journal": None})]))
    out = tc.get_citation_by_doi(REAL)
    assert out["doi"] == REAL and out["source"] == "semantic-scholar"      # no journal: no crash (R-32)


def test_free_text_below_the_title_threshold_is_not_a_citation(monkeypatch):
    unrelated = {"message": {"items": [{"DOI": "10.1016/j.real.2001.01.001",
                                       "title": ["Unrelated paper about soil microbes"]}]}}
    s2 = {"data": [{"title": "Totally different paper", "externalIds": {"DOI": "10.2222/other"}, "authors": []}]}
    monkeypatch.setattr(tc, "_http_fetch", Net([("api.crossref.org/works?query=", unrelated),
                                                  ("semanticscholar.org/graph/v1/paper/search", s2),
                                                  ("api.datacite.org/dois?query=", {"data": []})]))
    out = tc.lookup_citation("Madeup Author 2031 quantum baseflow of martian catchments")
    assert out.get("not_confirmed") is True and out["not_found"] is False and "doi" not in out
    assert {c["doi"] for c in out["candidates"]} == {"10.1016/j.real.2001.01.001", "10.2222/other"}
    assert "does NOT show that no citation exists" in out["_note"]


def test_free_text_with_a_good_title_match_is_found(monkeypatch):
    body = {"message": {"items": [{"DOI": REAL, "title": ["Flow-duration curves I: review and use"],
                                   "container-title": []}]}}                       # empty journal list: no crash
    monkeypatch.setattr(tc, "_http_fetch", Net([("api.crossref.org/works?query=", body)]))
    out = tc.lookup_citation("flow duration curves review")
    # the whole title is only four content words: a topic phrase of that size is never `high`
    assert out["doi"] == REAL and out["confidence"] == "medium"
    body2 = {"message": {"items": [{"DOI": REAL, "title": ["Flow-duration curves I: review and use of statistics for hydrologic design"]}]}}
    monkeypatch.setattr(tc, "_http_fetch", Net([("api.crossref.org/works?query=", body2)]))
    out2 = tc.lookup_citation("flow duration curves review")           # a topic phrase, not the title
    assert out2["confidence"] == "medium" and "Verify before citing" in out2["match_note"]


def test_semantic_scholar_hit_without_a_journal_does_not_crash(monkeypatch):
    s2 = {"data": [{"title": "Flow duration curves review", "year": 1994, "journal": None,
                    "externalIds": {"DOI": REAL}, "authors": [{"name": "R Vogel"}]}]}
    monkeypatch.setattr(tc, "_http_fetch", Net([("api.crossref.org", "404"),
                                                  ("semanticscholar.org/graph/v1/paper/search", s2)]))
    out = tc.lookup_citation("flow duration curves review")
    assert out["doi"] == REAL and out["source"] == "semantic-scholar"


def test_render_apa_tolerates_empty_csl_lists():
    assert tc._render_apa({"title": [], "container-title": [], "author": [], "DOI": "10.1/x"}).startswith("Unknown")


def test_unsupported_source_hint_is_refused_not_ignored(monkeypatch):
    monkeypatch.setattr(tc, "_http_fetch", Net([]))
    out = tc.lookup_citation("flow duration curves", source_hint="pubmed")
    assert out["code"] == "INVALID_ARGUMENT"


def test_source_hint_restricts_the_providers(monkeypatch):
    net = Net([("api.datacite.org/dois/", "404")])
    monkeypatch.setattr(tc, "_http_fetch", net)
    out = tc.lookup_citation("10.5281/zenodo.1", source_hint="datacite")
    assert out.get("not_found") is True and list(out["providers"]) == ["datacite"]
    assert all("crossref" not in u for u in net.urls)


@pytest.mark.parametrize("bad", ["not a doi", "10.12/short", "", "https://example.org/x"])
def test_a_string_that_is_not_a_doi_is_refused_by_get_citation_by_doi(bad, monkeypatch):
    monkeypatch.setattr(tc, "_http_fetch", Net([]))
    out = tc.get_citation_by_doi(bad)
    assert out["code"] == "INVALID_ARGUMENT"


def cr(doi, title, authors, year):
    return {"DOI": doi, "title": [title], "author": [{"family": f, "given": "X"} for f in authors],
            "issued": {"date-parts": [[year]]}}


def search(monkeypatch, items):
    monkeypatch.setattr(tc, "_http_fetch", Net([("api.crossref.org/works?query=", {"message": {"items": items}}),
                                                  ("semanticscholar", {"data": []}), ("datacite", {"data": []})]))


TRUE_RECORD = cr(REAL, "Flow-duration curves. I: New interpretation and confidence intervals", ["Vogel", "Fennessey"], 1994)


def test_the_true_record_with_matching_authors_and_year_is_high(monkeypatch):
    search(monkeypatch, [TRUE_RECORD])
    out = tc.lookup_citation("Vogel Fennessey 1994 flow duration curves")
    assert out["doi"] == REAL and out["confidence"] == "high" and "match_note" not in out


def test_row1_same_topic_different_authors_is_not_returned(monkeypatch):
    search(monkeypatch, [cr("10.5555/brazil", "Flow duration curves for ungauged basins in Brazil", ["Silva"], 2015)])
    out = tc.lookup_citation("Vogel Fennessey 1994 flow duration curves")
    assert "doi" not in out and out["not_confirmed"] is True
    assert "neither in the title nor a family name" in out["candidates"][0]["rejected"]


def test_row2_right_topic_wrong_year_is_not_returned(monkeypatch):
    search(monkeypatch, [cr("10.5555/review", "A review of baseflow recession analysis methods", ["Smith"], 2018)])
    out = tc.lookup_citation("Smith 2010 baseflow recession analysis")
    assert "doi" not in out and "differs from the record's year 2018" in out["candidates"][0]["rejected"]


def test_year_tolerance_of_one_allows_online_first_dates(monkeypatch):
    search(monkeypatch, [cr("10.5555/review", "A review of baseflow recession analysis methods", ["Smith"], 2011)])
    out = tc.lookup_citation("Smith 2010 baseflow recession analysis")
    assert out["doi"] == "10.5555/review" and out["confidence"] == "high"


def test_row3_two_author_names_missing_from_the_record_are_rejected(monkeypatch):
    search(monkeypatch, [cr("10.5555/dl", "Deep learning for streamflow prediction in Europe", ["Kratzert"], 2019)])
    out = tc.lookup_citation("Zhang Wang deep learning streamflow")
    assert "doi" not in out and out["not_confirmed"] is True


def test_row4_extra_names_that_are_not_authors_cannot_reach_high(monkeypatch):
    search(monkeypatch, [cr("10.5555/fdc", "Flow duration curves", ["Vogel"], 1994)])
    out = tc.lookup_citation("Vogel Fennessey Smith Jones flow duration curves")
    assert "doi" not in out and out["not_confirmed"] is True        # the extra names score as topic words: 3/6


def test_a_hit_with_no_authors_from_the_provider_cannot_confirm_a_surname_so_it_is_a_candidate(monkeypatch):
    search(monkeypatch, [{"DOI": REAL, "title": ["Flow-duration curves. I: New interpretation and confidence intervals"],
                          "issued": {"date-parts": [[1994]]}}])
    out = tc.lookup_citation("Vogel Fennessey 1994 flow duration curves")
    assert "doi" not in out and out["not_confirmed"] is True
    assert "the provider gave none" in out["candidates"][0]["rejected"]


def test_a_hit_with_no_year_is_capped_at_medium(monkeypatch):
    rec = dict(TRUE_RECORD); rec.pop("issued")
    search(monkeypatch, [rec])
    out = tc.lookup_citation("Vogel Fennessey 1994 flow duration curves")
    assert out["confidence"] == "medium" and "no year" in out["match_note"]


def test_a_near_full_title_with_no_author_or_year_can_be_high(monkeypatch):
    search(monkeypatch, [cr("10.5555/x", "Flow duration curves for ungauged basins", ["A"], 2001)])
    assert tc.lookup_citation("flow duration curves for ungauged basins")["confidence"] == "high"
    search(monkeypatch, [cr("10.5555/x", "Flow duration curves for ungauged basins", ["A"], 2001)])
    assert tc.lookup_citation("flow duration curves")["confidence"] == "medium"


def test_the_content_word_guard_counts_scored_words_not_discarded_ones():
    # three long words, but two are capitalised names absent from the title: only one is scored
    a = tc._assess("Vogel Fennessey baseflow", ["Baseflow separation"], [{"vogel"}, {"fennessey"}], None)
    assert a["n_scored"] == 1 and a["score"] < tc.MIN_TITLE_SCORE and a["high"] is False


def test_a_single_keyword_never_matches_and_matching_is_on_whole_words(monkeypatch):
    items = {"message": {"items": [{"DOI": "10.1234/overflow", "title": ["Overflow of dams in arid regions"]}]}}
    monkeypatch.setattr(tc, "_http_fetch", Net([("api.crossref.org/works?query=", items),
                                                  ("semanticscholar", {"data": []}), ("datacite", {"data": []})]))
    one = tc.lookup_citation("flow")
    assert one.get("not_confirmed") is True and "doi" not in one
    assert tc._title_match_score("Overflow of dams", "flow regime shifts") == 0.0
    assert tc._title_match_score("Flow regime shifts", "flow") < tc.MIN_TITLE_SCORE


def test_nothing_returned_by_any_provider_is_the_only_plain_not_found(monkeypatch):
    monkeypatch.setattr(tc, "_http_fetch", Net([("api.crossref.org/works?query=", {"message": {"items": []}}),
                                                  ("semanticscholar", {"data": []}), ("datacite", {"data": []})]))
    out = tc.lookup_citation("a thoroughly nonexistent monograph title")
    assert out["not_found"] is True and "No peer-reviewed citation found" in out["_note"]


# ---- review follow-ups: diacritics, hyphenated names, capitalised topic words, exact titles

def test_diacritics_match_however_they_are_typed(monkeypatch):
    search(monkeypatch, [cr("10.5555/mu", "Rainfall runoff modelling with recurrent networks", ["Müller"], 2020)])
    assert tc.lookup_citation("Muller 2020 rainfall runoff modelling recurrent networks")["doi"] == "10.5555/mu"
    search(monkeypatch, [cr("10.5555/mu", "Rainfall runoff modelling with recurrent networks", ["Muller"], 2020)])
    assert tc.lookup_citation("Müller 2020 rainfall runoff modelling recurrent networks")["doi"] == "10.5555/mu"
    assert tc._family("Åström, K.") == {"astrom"} and tc._family("Søren Højgaard") == {"hojgaard"}


def test_hyphenated_and_particle_surnames_match_on_any_part(monkeypatch):
    assert tc._family("Smith-Jones", True) == {"smith", "jones", "smithjones"}
    assert tc._family("Anna Smith-Jones") >= {"smith", "jones"}
    for q in ("Smith 2015 groundwater storage recession", "Jones 2015 groundwater storage recession",
              "Smith-Jones 2015 groundwater storage recession"):
        search(monkeypatch, [cr("10.5555/sj", "Groundwater storage recession in karst", ["Smith-Jones"], 2015)])
        assert tc.lookup_citation(q)["doi"] == "10.5555/sj", q


def test_capitalised_topic_words_are_scored_as_topic_not_dropped_as_authors(monkeypatch):
    # "River" is not an author and does not open the query: it is a topic word, so the title match is judged on it
    a = tc._assess("Mississippi River flood frequency", "Flood frequency analysis of the Mississippi River",
                   [{"smith"}], None)
    assert a["reject"] is None and a["score"] == 1.0 and a["n_scored"] == 4
    # a paper that merely shares the lower-case topic words does not get a free pass on the capitalised ones
    b = tc._assess("flood frequency Mississippi River", "Flood frequency analysis in alpine catchments",
                   [{"smith"}], None)
    assert "mississippi, river" in b["reject"]       # not in the title, not an author: a candidate, not a citation
    # "Hydrological Modelling Using Deep Learning": capitalised words in the title are title words
    c = tc._assess("Hydrological Modelling Using Deep Learning", "Hydrological modelling using deep learning",
                   [{"kratzert"}], None)
    assert c["reject"] is None and c["score"] == 1.0 and c["high"] is True


def test_a_leading_capitalised_word_missing_from_the_title_is_still_an_author_check(monkeypatch):
    search(monkeypatch, [cr("10.5555/a", "Flow duration curves for ungauged basins", ["Silva"], 2015)])
    out = tc.lookup_citation("Vogel flow duration curves ungauged basins")
    assert "doi" not in out and "neither in the title nor a family name" in out["candidates"][0]["rejected"]


def test_an_exact_full_title_with_no_author_reaches_high_but_a_topic_phrase_does_not(monkeypatch):
    title = "Flow-duration curves. I: New interpretation and confidence intervals"
    search(monkeypatch, [cr(REAL, title, ["Vogel", "Fennessey"], 1994)])
    assert tc.lookup_citation(title)["confidence"] == "high"
    search(monkeypatch, [cr(REAL, title, ["Vogel", "Fennessey"], 1994)])
    assert tc.lookup_citation("flow duration curves")["confidence"] == "medium"
    search(monkeypatch, [cr(REAL, title, ["Vogel", "Fennessey"], 1994)])
    assert tc.lookup_citation("flow duration curves new interpretation")["confidence"] == "medium"


# ---- review round 3: every ordering of surname and year, with and without the year

BRAZIL = cr("10.5555/brazil", "Flow duration curves for ungauged basins in Brazil", ["Silva", "Costa"], 2015)


@pytest.mark.parametrize("query", [
    "flow duration curves Vogel Fennessey",             # surnames last, no year
    "flow duration curves Vogel Fennessey 1994",        # surnames then year, last
    "flow duration curves 1994 Vogel Fennessey",        # year between
    "1994 Vogel Fennessey flow duration curves",        # year first
    "Vogel 1994 Fennessey flow duration curves",
    "Vogel Fennessey flow duration curves",             # no year
    "flow duration curves, Vogel and Fennessey (1994)",
])
def test_a_wrong_paper_on_the_same_topic_is_never_found_whatever_the_order(monkeypatch, query):
    search(monkeypatch, [BRAZIL])
    out = tc.lookup_citation(query)
    assert "doi" not in out and out["not_confirmed"] is True, query
    assert "neither in the title nor a family name" in out["candidates"][0]["rejected"]
    assert "does NOT show that no citation exists" in out["_note"]


@pytest.mark.parametrize("query", [
    "flow duration curves Vogel Fennessey 1994", "1994 Vogel Fennessey flow duration curves",
    "Vogel Fennessey flow duration curves"])
def test_the_true_record_is_found_in_the_same_orderings(monkeypatch, query):
    search(monkeypatch, [TRUE_RECORD])
    out = tc.lookup_citation(query)
    assert out["doi"] == REAL
    assert out["confidence"] == "high"          # title, both authors (and the year, when given) all verified


def test_short_generic_titles_never_reach_high_on_a_topic_phrase(monkeypatch):
    search(monkeypatch, [cr("10.5555/rev", "Flow duration curves: a review", ["Lee"], 2012)])
    out = tc.lookup_citation("flow duration curves review")
    assert out["doi"] == "10.5555/rev" and out["confidence"] == "medium"
    # a title of 5+ content words fully covered by the query can
    search(monkeypatch, [cr("10.5555/long", "Flow duration curves for ungauged basins", ["Lee"], 2012)])
    assert tc.lookup_citation("flow duration curves for ungauged basins")["confidence"] == "high"
