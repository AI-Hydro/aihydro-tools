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
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp")

_CACHE_DIR = Path.home() / ".aihydro" / "citations"
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


def _http_get(url: str) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": _POLITE_UA, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.debug("HTTP GET failed (%s): %s", url, exc)
        return None


# ── APA renderer ──────────────────────────────────────────────────────────

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

    issued = csl.get("issued", {})
    date_parts = issued.get("date-parts", [[""]])
    year = date_parts[0][0] if date_parts and date_parts[0] else ""

    title_raw = csl.get("title", "Untitled")
    title = title_raw[0] if isinstance(title_raw, list) else title_raw

    journal_raw = csl.get("container-title", "")
    journal = journal_raw[0] if isinstance(journal_raw, list) else journal_raw

    volume = csl.get("volume", "")
    issue = f"({csl['issue']})" if csl.get("issue") else ""
    page = csl.get("page", "")
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

def _title_match_score(titles: list[str] | str | Any, query: str) -> float:
    if not titles:
        return 0.0
    title = (titles[0] if isinstance(titles, list) else str(titles)).lower()
    words = [w for w in query.lower().split() if len(w) > 3]
    if not words:
        return 0.0
    return sum(1 for w in words if w in title) / len(words)


def _from_crossref(query: str) -> dict[str, Any] | None:
    url = (
        f"https://api.crossref.org/works"
        f"?query={urllib.parse.quote(query)}"
        f"&rows=3&mailto=gh9690%40myamu.ac.in"
    )
    data = _http_get(url)
    if not data:
        return None
    items = data.get("message", {}).get("items", [])
    if not items:
        return None
    best = max(items, key=lambda x: _title_match_score(x.get("title"), query))
    doi = best.get("DOI", "").lower()
    if not doi:
        return None
    score = _title_match_score(best.get("title"), query)
    return {
        "doi": doi,
        "cslJson": best,
        "formattedApa": _render_apa(best),
        "source": "crossref",
        "confidence": "high" if score > 0.5 else "medium",
        "cachedAtMs": int(time.time() * 1000),
    }


def _from_semantic_scholar(query: str) -> dict[str, Any] | None:
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/search"
        f"?query={urllib.parse.quote(query)}&limit=3"
        f"&fields=title,authors,year,externalIds,journal,volume,pages"
    )
    data = _http_get(url)
    if not data:
        return None
    items = data.get("data", [])
    if not items:
        return None
    best = items[0]
    ext = best.get("externalIds") or {}
    doi = (ext.get("DOI") or "").lower()
    if not doi:
        return None
    csl: dict[str, Any] = {
        "DOI": doi,
        "title": [best.get("title", "")],
        "author": [
            {"family": (n := a.get("name", "")).split()[-1], "given": " ".join(n.split()[:-1])}
            for a in best.get("authors", [])
        ],
        "issued": {"date-parts": [[best["year"]]]} if best.get("year") else {},
        "container-title": [best["journal"]["name"]] if best.get("journal") else [],
        "volume": (best.get("journal") or {}).get("volume"),
        "page": (best.get("journal") or {}).get("pages"),
    }
    return {
        "doi": doi,
        "cslJson": csl,
        "formattedApa": _render_apa(csl),
        "source": "semantic-scholar",
        "confidence": "medium",
        "cachedAtMs": int(time.time() * 1000),
    }


def _from_datacite(query: str) -> dict[str, Any] | None:
    url = f"https://api.datacite.org/dois?query={urllib.parse.quote(query)}&page[size]=3"
    data = _http_get(url)
    if not data:
        return None
    items = data.get("data", [])
    if not items:
        return None
    best = items[0]
    attrs = best.get("attributes") or {}
    doi = (attrs.get("doi") or best.get("id") or "").lower()
    if not doi:
        return None
    csl: dict[str, Any] = {
        "DOI": doi,
        "title": [attrs["titles"][0]["title"]] if attrs.get("titles") else [],
        "author": [
            {
                "family": c.get("familyName") or c.get("name", ""),
                "given": c.get("givenName", ""),
            }
            for c in attrs.get("creators", [])
        ],
        "issued": {"date-parts": [[attrs["publicationYear"]]]} if attrs.get("publicationYear") else {},
        "publisher": attrs.get("publisher"),
        "type": "dataset",
    }
    return {
        "doi": doi,
        "cslJson": csl,
        "formattedApa": _render_apa(csl),
        "source": "datacite",
        "confidence": "low",
        "cachedAtMs": int(time.time() * 1000),
    }


# ── MCP tools ──────────────────────────────────────────────────────────────

@mcp.tool()
def lookup_citation(
    query: str,
    source_hint: str | None = None,
    force_refresh: bool = False,
) -> dict:
    """
    Look up a citation (free text or DOI). Cascade: CrossRef → Semantic
    Scholar → DataCite. Cached at ~/.aihydro/citations/ (30 day TTL).

    ALWAYS call before writing any reference. If not_found, write
    "No peer-reviewed citation found" — NEVER invent a DOI/author/year.
    Use the returned formatted_apa and doi exactly as returned.
    source_hint: crossref | semantic-scholar | datacite (default: cascade).
    """
    try:
        if not query.strip():
            return {"error": True, "message": "query cannot be empty"}

        # Check cache first for DOI queries
        doi_pattern = query.strip().lower().replace("https://doi.org/", "")
        if doi_pattern.startswith("10.") and not force_refresh:
            cached = _cache_get(doi_pattern)
            if cached:
                return {
                    "doi": cached["doi"],
                    "formatted_apa": cached["formattedApa"],
                    "csl_json": cached["cslJson"],
                    "source": cached["source"],
                    "confidence": cached["confidence"],
                    "from_cache": True,
                }

        # Cascade providers
        result: dict[str, Any] | None = None
        hint = (source_hint or "any").lower()

        if not result and hint not in ("semantic-scholar", "datacite"):
            result = _from_crossref(query)
        if not result and hint not in ("crossref", "datacite"):
            result = _from_semantic_scholar(query)
        if not result and hint not in ("crossref", "semantic-scholar"):
            result = _from_datacite(query)
        # Exhaustive fallback for "any"
        if not result and hint == "any":
            result = _from_semantic_scholar(query)
            if not result:
                result = _from_datacite(query)

        if not result:
            return {
                "not_found": True,
                "query": query,
                "_note": (
                    "No provider returned a match. "
                    "NEVER invent a citation — write 'No peer-reviewed citation found' in the module."
                ),
            }

        _cache_put(result)
        return {
            "doi": result["doi"],
            "formatted_apa": result["formattedApa"],
            "csl_json": result["cslJson"],
            "source": result["source"],
            "confidence": result["confidence"],
            "from_cache": False,
        }

    except Exception as e:
        log.error("lookup_citation failed: %s", e)
        return _tool_error_to_dict(e)


@mcp.tool()
def get_citation_by_doi(doi: str, force_refresh: bool = False) -> dict:
    """
    Resolve a citation by exact DOI (uses cache when fresh). Same schema
    as lookup_citation.
    """
    return lookup_citation(doi, force_refresh=force_refresh)


@mcp.tool()
def list_cached_citations() -> dict:
    """List cached DOIs in ~/.aihydro/citations/. No API calls."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        items = []
        for fp in sorted(_CACHE_DIR.glob("*.json")):
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
