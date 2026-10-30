"""
Citation lookup MCP tools — CrossRef → Semantic Scholar → DataCite cascade.

These tools enforce the anti-hallucination contract: the agent MUST call
`lookup_citation` before writing any reference in a module. If the tool
returns not_found, the agent writes "no peer-reviewed citation found" and
NEVER invents a DOI.

Tools
-----
  lookup_citation(query, source_hint?)  — look up a citation by title/author/year
  get_citation_by_doi(doi)              — get a cached or fresh citation by exact DOI
  list_cached_citations()               — list all cached DOIs for the current session
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict
from ai_hydro.registry.paths import home_path

log = logging.getLogger("ai_hydro.mcp")

_CACHE_DIR = home_path("citations")
_TTL_SECONDS = 30 * 24 * 3600  # 30 days
_TIMEOUT = 8  # seconds per HTTP call
_POLITE_UA = "AI-Hydro/0.1.24 (+mailto:gh9690@myamu.ac.in)"


# ── Cache helpers ──────────────────────────────────────────────────────────

def _doi_to_filename(doi: str) -> str:
    clean = doi.lower().replace("https://doi.org/", "").replace("/", "__")
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in clean)


def _cache_get(doi: str) -> dict[str, Any] | None:
    try:
        fp = _CACHE_DIR / f"{_doi_to_filename(doi)}.json"
        if not fp.exists():
            return None
        entry = json.loads(fp.read_text(encoding="utf-8"))
        if time.time() - entry.get("cachedAtMs", 0) / 1000 > _TTL_SECONDS:
            return None
        return entry
    except Exception:
        return None


def _cache_put(entry: dict[str, Any]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        doi = entry.get("doi", "unknown")
        fp = _CACHE_DIR / f"{_doi_to_filename(doi)}.json"
        fp.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log.debug("Citation cache write failed: %s", exc)


# Outcomes of one provider call. "unavailable" (network error, timeout, 5xx, unreadable body) means the
# provider could not answer: it is never evidence that no citation exists (R-32).
FOUND, NOT_FOUND, UNAVAILABLE = "found", "not_found", "unavailable"


def _http_fetch(url: str) -> tuple[str, dict[str, Any] | None]:
    """``("ok", data)``, ``("not_found", None)`` on HTTP 404, else ``("unavailable", None)``."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": _POLITE_UA, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return "ok", json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return NOT_FOUND, None
        log.debug("HTTP GET failed (%s): HTTP %s", url, exc.code)
        return UNAVAILABLE, None
    except Exception as exc:
        log.debug("HTTP GET failed (%s): %s", url, exc)
        return UNAVAILABLE, None


# ── DOI helpers ───────────────────────────────────────────────────────────

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def _normalize_doi(text: str) -> str:
    """Lower-case DOI without a resolver prefix; ``""`` when ``text`` is not shaped like a DOI."""
    t = (text or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
    return t if _DOI_RE.match(t) else ""


# ── APA renderer ──────────────────────────────────────────────────────────

def _first(value: Any, default: str = "") -> str:
    """First element of a CSL list field, or the string itself; ``default`` when empty."""
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else default
    return str(value) if value else default


def _render_apa(csl: dict[str, Any]) -> str:
    authors_raw = csl.get("author", [])
    if isinstance(authors_raw, list) and authors_raw:
        parts = []
        for a in authors_raw[:6]:
            if isinstance(a, dict):
                fam = a.get("family", "")
                giv = a.get("given", "")
                parts.append(f"{fam}, {giv[:1]}." if fam and giv else fam or a.get("name", ""))
        author_str = ", ".join(parts)
        if len(authors_raw) > 6:
            author_str += " et al."
    else:
        author_str = "Unknown"

    issued = csl.get("issued") or {}
    date_parts = issued.get("date-parts") or [[""]]
    year = date_parts[0][0] if date_parts and date_parts[0] else ""

    title = _first(csl.get("title"), "Untitled")
    journal = _first(csl.get("container-title"))

    volume = csl.get("volume") or ""
    issue = f"({csl['issue']})" if csl.get("issue") else ""
    page = csl.get("page") or ""
    doi = csl.get("DOI", "")

    apa = f"{author_str} ({year}). {title}."
    if journal:
        apa += f" *{journal}*"
    if volume:
        apa += f", *{volume}*{issue}"
    if page:
        apa += f", {page}"
    apa += "."
    if doi:
        apa += f" https://doi.org/{doi}"
    return apa


# ── Provider functions ─────────────────────────────────────────────────────

#: A free-text hit below this title-word overlap is a candidate, never a citation (R-05).
MIN_TITLE_SCORE = 0.6
#: At or above this overlap a Crossref hit is labelled high confidence.
HIGH_TITLE_SCORE = 0.8


_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_YEAR_RE = re.compile(r"^(?:19|20)\d\d$")
#: A query year may differ from the record's year by this much: online-first and print dates often straddle
#: a year boundary (the Crossref ``issued`` date is one of them). Two or more years apart is a different paper.
YEAR_TOLERANCE = 1


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


def _family(name: str) -> str:
    """Lower-case family name from "Vogel, Richard", "Richard M. Vogel" or "Vogel"."""
    name = (name or "").strip()
    if "," in name:
        name = name.split(",", 1)[0]
    parts = _WORD_RE.findall(name)
    return parts[-1].lower() if parts else ""


def _assess(query: str, title: Any, families: list[str] | None, year: Any) -> dict[str, Any]:
    """Judge one provider hit against a free-text query.

    * Content words are the query words that count toward the title match: more than 3 letters, not a number, and
      not a capitalised word missing from the title. Matching is on whole words ("flow" is not "overflow").
    * A capitalised query word missing from the title is treated as an author name and MUST be a family name in
      the hit's author list; a four-digit query year MUST be within ``YEAR_TOLERANCE`` of the record's year.
      A mismatch rejects the hit (it is listed as an unverified candidate, never returned as the citation).
      A provider that gives no authors/year leaves the check unverified: the hit may be returned but not as `high`.
    * Fewer than two scored content words never reach the match threshold; ``n_scored`` counts the words actually
      scored, not the ones discarded.
    * `high` needs: title score >= HIGH_TITLE_SCORE, 3+ scored words, authors and year verified where the query
      gave them, and, when the query gave neither, a near-full-title match (the query covers >= 0.8 of the title's
      words), so a short topic phrase is never `high`.
    """
    t = title[0] if isinstance(title, list) and title else (title if isinstance(title, str) else "")
    title_words = {_stem(w.lower()) for w in _WORD_RE.findall(t)}
    content, names, years = [], [], []
    for w in _WORD_RE.findall(query):
        if _YEAR_RE.match(w):
            years.append(int(w))
        elif len(w) <= 3 or w.isdigit():
            continue
        elif w[0].isupper() and _stem(w.lower()) not in title_words:
            names.append(w.lower())
        else:
            content.append(_stem(w.lower()))
    matched = [w for w in content if w in title_words]
    score = len(matched) / len(content) if content else 0.0
    if len(content) < 2:
        score = min(score, MIN_TITLE_SCORE - 0.1)
    out: dict[str, Any] = {"score": score, "n_scored": len(content), "reject": None, "unverified": []}
    fams = {f for f in (families or []) if f}
    if names:
        if not fams:
            out["unverified"].append("the provider gave no authors to check the author name(s) against")
        elif not all(n in fams for n in names):
            out["reject"] = f"author(s) {', '.join(names)} not among the record's authors"
    if years and not out["reject"]:
        try:
            ry = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            ry = None
        if ry is None:
            out["unverified"].append("the provider gave no year to check the query year against")
        elif any(abs(y - ry) > YEAR_TOLERANCE for y in years):
            out["reject"] = f"year {', '.join(map(str, years))} differs from the record's year {ry}"
    covers = len(set(matched)) / len(title_words) if title_words else 0.0
    out["high"] = bool(
        not out["reject"] and not out["unverified"] and score >= HIGH_TITLE_SCORE and len(content) >= 3
        and (bool(names) or bool(years) or covers >= 0.8))
    return out


def _title_match_score(titles: list[str] | str | Any, query: str) -> float:
    return _assess(query, titles, None, None)["score"]


def _entry(doi: str, csl: dict[str, Any], source: str, confidence: str, match_note: str = "") -> dict[str, Any]:
    return {
        **({"matchNote": match_note} if match_note else {}),
        "doi": doi,
        "cslJson": csl,
        "formattedApa": _render_apa(csl),
        "source": source,
        "confidence": confidence,
        "cachedAtMs": int(time.time() * 1000),
    }


def _semantic_scholar_csl(item: dict[str, Any], doi: str) -> dict[str, Any]:
    journal = item.get("journal") or {}
    return {
        "DOI": doi,
        "title": [item.get("title") or ""],
        "author": [
            {"family": (n.split() or [""])[-1], "given": " ".join(n.split()[:-1])}
            for n in ((a.get("name") or "") for a in item.get("authors") or [])
        ],
        "issued": {"date-parts": [[item["year"]]]} if item.get("year") else {},
        "container-title": [journal["name"]] if journal.get("name") else [],
        "volume": journal.get("volume"),
        "page": journal.get("pages"),
    }


def _datacite_csl(attrs: dict[str, Any], doi: str) -> dict[str, Any]:
    titles = attrs.get("titles") or []
    return {
        "DOI": doi,
        "title": [titles[0].get("title", "")] if titles else [],
        "author": [
            {"family": c.get("familyName") or c.get("name", ""), "given": c.get("givenName", "")}
            for c in attrs.get("creators") or []
        ],
        "issued": {"date-parts": [[attrs["publicationYear"]]]} if attrs.get("publicationYear") else {},
        "publisher": attrs.get("publisher"),
        "type": "dataset",
    }


def _crossref_by_doi(doi: str) -> tuple[str, dict[str, Any] | None]:
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/')}?mailto=gh9690%40myamu.ac.in"
    status, data = _http_fetch(url)
    if status != "ok":
        return status, None
    item = (data or {}).get("message") or {}
    got = _normalize_doi(item.get("DOI", ""))
    return (FOUND, _entry(doi, item, "crossref", "high")) if got == doi else (NOT_FOUND, None)


def _datacite_by_doi(doi: str) -> tuple[str, dict[str, Any] | None]:
    status, data = _http_fetch(f"https://api.datacite.org/dois/{urllib.parse.quote(doi, safe='/')}")
    if status != "ok":
        return status, None
    row = (data or {}).get("data") or {}
    attrs = row.get("attributes") or {}
    got = _normalize_doi(attrs.get("doi") or row.get("id") or "")
    return (FOUND, _entry(doi, _datacite_csl(attrs, doi), "datacite", "high")) if got == doi else (NOT_FOUND, None)


def _semantic_scholar_by_doi(doi: str) -> tuple[str, dict[str, Any] | None]:
    url = (f"https://api.semanticscholar.org/graph/v1/paper/DOI:{urllib.parse.quote(doi, safe='/')}"
           f"?fields=title,authors,year,externalIds,journal")
    status, data = _http_fetch(url)
    if status != "ok":
        return status, None
    got = _normalize_doi(((data or {}).get("externalIds") or {}).get("DOI") or "")
    return (FOUND, _entry(doi, _semantic_scholar_csl(data, doi), "semantic-scholar", "high")) \
        if got == doi else (NOT_FOUND, None)


def _crossref_year(it: dict[str, Any]) -> Any:
    for key in ("issued", "published-print", "published-online", "published"):
        parts = (it.get(key) or {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            return parts[0][0]
    return None


def _search_crossref(query: str) -> tuple[str, dict[str, Any] | None, list[dict]]:
    url = (f"https://api.crossref.org/works?query={urllib.parse.quote(query)}"
           f"&rows=3&mailto=gh9690%40myamu.ac.in")
    status, data = _http_fetch(url)
    if status != "ok":
        return status, None, []
    cands = []
    for it in ((data or {}).get("message") or {}).get("items") or []:
        doi = _normalize_doi(it.get("DOI", ""))
        if doi:
            fams = [_family(a.get("family") or a.get("name") or "") for a in it.get("author") or []
                    if isinstance(a, dict)]
            cands.append((_assess(query, it.get("title"), fams, _crossref_year(it)), doi, it))
    return _pick(cands, "crossref", True, lambda it, doi: it)


def _search_semantic_scholar(query: str) -> tuple[str, dict[str, Any] | None, list[dict]]:
    url = (f"https://api.semanticscholar.org/graph/v1/paper/search?query={urllib.parse.quote(query)}&limit=3"
           f"&fields=title,authors,year,externalIds,journal,volume,pages")
    status, data = _http_fetch(url)
    if status != "ok":
        return status, None, []
    cands = []
    for it in (data or {}).get("data") or []:
        doi = _normalize_doi(((it.get("externalIds") or {}).get("DOI")) or "")
        if doi:
            fams = [_family(a.get("name") or "") for a in it.get("authors") or [] if isinstance(a, dict)]
            cands.append((_assess(query, it.get("title"), fams, it.get("year")), doi, it))
    return _pick(cands, "semantic-scholar", False, _semantic_scholar_csl)


def _search_datacite(query: str) -> tuple[str, dict[str, Any] | None, list[dict]]:
    status, data = _http_fetch(f"https://api.datacite.org/dois?query={urllib.parse.quote(query)}&page[size]=3")
    if status != "ok":
        return status, None, []
    cands = []
    for it in (data or {}).get("data") or []:
        attrs = it.get("attributes") or {}
        doi = _normalize_doi(attrs.get("doi") or it.get("id") or "")
        titles = attrs.get("titles") or []
        if doi:
            fams = [_family(c.get("familyName") or c.get("name") or "") for c in attrs.get("creators") or []
                    if isinstance(c, dict)]
            cands.append((_assess(query, [titles[0].get("title", "")] if titles else [], fams,
                                  attrs.get("publicationYear")), doi, attrs))
    return _pick(cands, "datacite", False, _datacite_csl, base="low")


def _pick(cands, source, may_be_high, csl_of, base="medium"):
    """Best non-rejected hit at or above ``MIN_TITLE_SCORE`` -> found; every other hit is a listed candidate."""
    if not cands:
        return NOT_FOUND, None, []
    listed = [{"doi": d, "title_match": round(a["score"], 2), "source": source,
               **({"rejected": a["reject"]} if a["reject"] else {})}
              for a, d, _ in sorted(cands, key=lambda c: -c[0]["score"])]
    ok = [c for c in cands if not c[0]["reject"] and c[0]["score"] >= MIN_TITLE_SCORE]
    if not ok:
        return NOT_FOUND, None, listed
    a, doi, raw = max(ok, key=lambda c: c[0]["score"])
    csl = csl_of(raw, doi)
    csl.setdefault("DOI", doi)
    confidence = "high" if (may_be_high and a["high"]) else base
    note = ""
    if confidence != "high" and may_be_high:
        note = ("Medium confidence: " + ("; ".join(a["unverified"]) if a["unverified"] else
                "a topic-only or partial title match is not enough for high") + ". Verify before citing.")
    return FOUND, _entry(doi, csl, source, confidence, note), listed


_PROVIDERS = ("crossref", "semantic-scholar", "datacite")
_DOI_PROVIDERS = {"crossref": _crossref_by_doi, "datacite": _datacite_by_doi,
                  "semantic-scholar": _semantic_scholar_by_doi}
_SEARCH_PROVIDERS = {"crossref": _search_crossref, "semantic-scholar": _search_semantic_scholar,
                     "datacite": _search_datacite}


def _unavailable(query: str, providers: dict[str, str]) -> dict[str, Any]:
    down = sorted(n for n, st in providers.items() if st == UNAVAILABLE)
    return {
        "error": True,
        "code": "CITATION_LOOKUP_UNAVAILABLE",
        "unavailable": True,
        "not_found": False,
        "query": query,
        "providers": providers,
        "message": (f"Could not reach {', '.join(down)}; no provider that answered knew this citation. "
                    "This does NOT mean no citation exists."),
        "recovery": ("Retry later or from a network that can reach the providers. Do not write 'No peer-reviewed "
                     "citation found' on this basis, and never invent a citation."),
        "next_tools": ["lookup_citation", "get_citation_by_doi"],
    }


def _not_found(query: str, providers: dict[str, str], candidates: list[dict] | None = None) -> dict[str, Any]:
    if candidates:
        return {
            "not_confirmed": True,
            "not_found": False,
            "query": query,
            "providers": providers,
            "candidates": candidates[:5],
            "_note": (
                "Not confirmed: no hit matched the query well enough to cite. `candidates` are the closest "
                "unverified hits; check them (or refine the query with title words), and cite one only after "
                "lookup_citation/get_citation_by_doi verifies it. This does NOT show that no citation exists, "
                "so do not write 'No peer-reviewed citation found' on this basis. Never invent a citation."),
        }
    return {
        "not_found": True,
        "query": query,
        "providers": providers,
        "_note": (
            "Every provider answered and returned nothing for this query. "
            "NEVER invent a citation: write 'No peer-reviewed citation found' in the module."),
    }


def _resolve_exact_doi(doi: str, hint: str) -> dict[str, Any]:
    """Resolve ``doi`` through the providers' DOI endpoints; the returned DOI must equal the one asked for."""
    states: dict[str, str] = {}
    for name in (_PROVIDERS if hint == "any" else (hint,)):
        status, result = _DOI_PROVIDERS[name](doi)
        states[name] = status
        if status == FOUND and result:
            return {"found": result}
    if UNAVAILABLE in states.values():
        return {"response": _unavailable(doi, states)}
    return {"response": _not_found(doi, states)}


def _resolve_search(query: str, hint: str) -> dict[str, Any]:
    states: dict[str, str] = {}
    candidates: list[dict] = []
    for name in (_PROVIDERS if hint == "any" else (hint,)):
        status, result, listed = _SEARCH_PROVIDERS[name](query)
        states[name] = status
        candidates += listed
        if status == FOUND and result:
            return {"found": result}
    if UNAVAILABLE in states.values():
        resp = _unavailable(query, states)
        if candidates:
            resp["candidates"] = candidates[:5]
        return {"response": resp}
    return {"response": _not_found(query, states, candidates)}


# ── MCP tools ──────────────────────────────────────────────────────────────

def _cached_response(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "doi": entry["doi"],
        "formatted_apa": entry["formattedApa"],
        "csl_json": entry["cslJson"],
        "source": entry["source"],
        "confidence": entry["confidence"],
        "from_cache": True,
        **({"match_note": entry["matchNote"]} if entry.get("matchNote") else {}),
    }


def _fresh_response(result: dict[str, Any]) -> dict[str, Any]:
    _cache_put(result)
    return {
        "doi": result["doi"],
        "formatted_apa": result["formattedApa"],
        "csl_json": result["cslJson"],
        "source": result["source"],
        "confidence": result["confidence"],
        "from_cache": False,
        **({"match_note": result["matchNote"]} if result.get("matchNote") else {}),
    }


def _bad_hint(source_hint: str | None) -> dict | None:
    hint = (source_hint or "any").strip().lower()
    if hint in ("any", *_PROVIDERS):
        return None
    return {
        "error": True,
        "code": "INVALID_ARGUMENT",
        "message": f"source_hint={source_hint!r} is not a supported provider.",
        "recovery": "Use one of: crossref, semantic-scholar, datacite, or omit it to try all.",
        "next_tools": ["lookup_citation"],
    }


@mcp.tool()
def lookup_citation(
    query: str,
    source_hint: str | None = None,
    force_refresh: bool = False,
) -> dict:
    """
    Look up a citation (free text or DOI). Cascade: CrossRef → Semantic
    Scholar → DataCite. Cached at ~/.aihydro/citations/ (30 day TTL).

    ALWAYS call before writing any reference. Three outcomes, never conflated:
    a verified citation; `not_found` (every provider answered and none matched
    well enough: write "No peer-reviewed citation found", NEVER invent a
    DOI/author/year); or `unavailable` (a provider could not be reached: this
    says nothing about whether the citation exists, so retry, do not write "not
    found"). A DOI query is resolved at the DOI endpoint and the returned DOI
    must equal the one asked for; a free-text hit needs enough title-word
    overlap, and closer misses come back as unverified `candidates`.
    Use the returned formatted_apa and doi exactly as returned.
    source_hint: crossref | semantic-scholar | datacite (default: cascade).
    """
    try:
        if not query.strip():
            return {"error": True, "message": "query cannot be empty"}
        bad = _bad_hint(source_hint)
        if bad:
            return bad
        hint = (source_hint or "any").strip().lower()

        doi = _normalize_doi(query)
        if doi:
            if not force_refresh:
                cached = _cache_get(doi)
                if cached and _normalize_doi(cached.get("doi", "")) == doi:
                    return _cached_response(cached)
            out = _resolve_exact_doi(doi, hint)
        else:
            out = _resolve_search(query, hint)
        if "found" in out:
            return _fresh_response(out["found"])
        return out["response"]

    except Exception as e:
        log.error("lookup_citation failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def get_citation_by_doi(doi: str, force_refresh: bool = False) -> dict:
    """
    Resolve a citation by exact DOI (uses cache when fresh). Same schema
    as lookup_citation. The DOI endpoint is used, never a search, and the
    DOI the provider returns must equal the one asked for. Network failure is
    `unavailable`, not `not_found`. A string that is not a DOI is refused.
    """
    try:
        if not _normalize_doi(doi):
            return {
                "error": True,
                "code": "INVALID_ARGUMENT",
                "message": f"{str(doi)[:80]!r} is not a DOI (expected 10.<registrant>/<suffix>).",
                "recovery": "Pass the bare DOI, e.g. 10.1061/(ASCE)0733-9496(1994)120:4(485), or use "
                            "lookup_citation for a title or author search.",
                "next_tools": ["lookup_citation"],
            }
        return lookup_citation(doi, force_refresh=force_refresh)
    except Exception as e:
        return _tool_error_to_dict(e)


@mcp.tool()
def list_cached_citations() -> dict:
    """List cached DOIs in ~/.aihydro/citations/. No API calls."""
    try:
        items = []
        # Listing never creates the cache directory (a read-only tool).
        for fp in (sorted(_CACHE_DIR.glob("*.json")) if _CACHE_DIR.is_dir() else []):
            try:
                entry = json.loads(fp.read_text(encoding="utf-8"))
                items.append({
                    "doi": entry.get("doi", fp.stem),
                    "source": entry.get("source", "unknown"),
                    "confidence": entry.get("confidence", "unknown"),
                    "cachedAt": entry.get("cachedAtMs", 0),
                    "formattedApa": entry.get("formattedApa", "")[:120] + "…",
                })
            except Exception:
                continue
        return {"cached": items, "count": len(items)}
    except Exception as e:
        log.error("list_cached_citations failed: %s", e)
        return _tool_error_to_dict(e)
