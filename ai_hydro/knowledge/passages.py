"""
Passage-level keyword index for the papers library.

This is NOT an embeddings index and does no semantic matching. It is BM25 keyword ranking
(SQLite FTS5, English Porter stemming). A query finds passages that share words with it;
"discharge" will not find a passage that only says "streamflow". Use the words the paper uses.

Documents are split into ~200-word passages. Each passage gets a deterministic
`passage_hash` (SHA-256[:16] of normalised text) that serves as its immutable fingerprint,
suitable for EvidenceSpan(source_type="paper", source_id=passage_hash). The hash is part of the
claim record, so it never changes with the ranking method.

Two files, one source of truth:

* ``passage_index.jsonl`` (one JSON object per line, atomic writes) is the index of record.
* ``passage_index.sqlite3`` beside it is a derived search table (FTS5 + a keyed hash lookup). It
  carries the size and mtime of the JSONL it was built from and is rebuilt from the JSONL when
  they differ, so it can be deleted at any time. A single code path ranks (``search_passages``)
  and a single one resolves a hash (``resolve_passage_hash``).

Formerly ``ai_hydro.knowledge.embeddings`` (a name that promised more than the code did); that
module name still imports and is the same module object.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from ai_hydro.registry.paths import home_path

log = logging.getLogger("ai_hydro.knowledge.passages")

KNOWLEDGE_DIR = home_path("knowledge")
PASSAGE_INDEX_PATH = KNOWLEDGE_DIR / "passage_index.jsonl"

PASSAGE_WORDS = 200          # target words per passage
PASSAGE_OVERLAP = 40         # sliding overlap in words
_HEX_RE = re.compile(r"^[0-9a-f]{12,20}$")   # looks like a passage hash


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------

class PassageRecord(TypedDict, total=False):
    passage_hash: str        # SHA-256[:16] of normalised text
    doc_name: str            # filename (basename)
    chunk_idx: int           # 0-based chunk index within doc
    text: str                # the passage text (verbatim from source)
    source_path: str         # absolute path to source document
    indexed_at: str          # ISO-8601 timestamp
    score: float             # search results only: BM25, larger is a better match


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _passage_hash(text: str) -> str:
    """SHA-256 of normalised (stripped, lowercased) text, 16-char hex prefix."""
    normalised = " ".join(text.strip().lower().split())
    return hashlib.sha256(normalised.encode()).hexdigest()[:16]


def _chunk_text(text: str, words: int = PASSAGE_WORDS, overlap: int = PASSAGE_OVERLAP) -> list[str]:
    """Split text into overlapping word-windows."""
    tokens = text.split()
    if not tokens:
        return []
    chunks: list[str] = []
    step = max(1, words - overlap)
    i = 0
    while i < len(tokens):
        chunk = tokens[i : i + words]
        chunks.append(" ".join(chunk))
        if i + words >= len(tokens):
            break
        i += step
    return chunks


def _read_doc(path: Path) -> str | None:
    """Read a document to plain text. Returns None on failure."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".md", ".txt", ".bib", ".rst"):
            return path.read_text(errors="replace")
        if suffix == ".pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(str(path))
                return "\n".join(p.extract_text() or "" for p in reader.pages)
            except ImportError:
                log.debug("pypdf not available; skipping %s", path.name)
                return None
    except Exception as exc:
        log.warning("Could not read %s: %s", path, exc)
    return None


# ---------------------------------------------------------------------------
# Index build
# ---------------------------------------------------------------------------

def build_passage_index(paths: list[Path]) -> dict:
    """
    Build (or rebuild) the passage index from the given file paths.

    Writes atomically to PASSAGE_INDEX_PATH.
    Returns {n_passages, n_docs, index_path}.
    """
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    records: list[str] = []
    n_docs = 0

    for path in paths:
        text = _read_doc(path)
        if not text:
            continue
        n_docs += 1
        chunks = _chunk_text(text)
        for idx, chunk in enumerate(chunks):
            rec: PassageRecord = {
                "passage_hash": _passage_hash(chunk),
                "doc_name": path.name,
                "chunk_idx": idx,
                "text": chunk,
                "source_path": str(path.resolve()),
                "indexed_at": now,
            }
            records.append(json.dumps(rec, ensure_ascii=False))

    # Atomic write
    tmp = PASSAGE_INDEX_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(records) + ("\n" if records else ""))
    os.replace(tmp, PASSAGE_INDEX_PATH)
    _build_sidecar()

    return {
        "n_passages": len(records),
        "n_docs": n_docs,
        "index_path": str(PASSAGE_INDEX_PATH),
    }


# ---------------------------------------------------------------------------
# Search table (derived from the JSONL; SQLite FTS5)
# ---------------------------------------------------------------------------

class PassageSearchUnavailable(RuntimeError):
    """This Python's SQLite was built without FTS5."""


_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have how in into is it its of on or that the "
    "their then there these this those to was were what when where which who why will with".split()
)


def _sidecar_path() -> Path:
    return PASSAGE_INDEX_PATH.with_suffix(".sqlite3")


def _stamp() -> str:
    st = PASSAGE_INDEX_PATH.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _build_sidecar() -> None:
    """Rebuild the search table from the JSONL, atomically. Never raises: a failure leaves the old
    table (or none) and the next read tries again."""
    if not PASSAGE_INDEX_PATH.exists():
        return
    path = _sidecar_path()
    tmp = path.with_suffix(".sqlite3.tmp")
    try:
        tmp.unlink(missing_ok=True)
        stamp = _stamp()
        conn = _connect(tmp)
        try:
            conn.executescript(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
                "CREATE TABLE passages(id INTEGER PRIMARY KEY, passage_hash TEXT NOT NULL, doc_name TEXT,"
                " chunk_idx INTEGER, text TEXT, source_path TEXT, indexed_at TEXT);"
                "CREATE INDEX passages_hash ON passages(passage_hash);"
                "CREATE VIRTUAL TABLE passages_fts USING fts5("
                " text, content='passages', content_rowid='id', tokenize='porter unicode61');"
            )
            conn.executemany(
                "INSERT INTO passages(passage_hash, doc_name, chunk_idx, text, source_path, indexed_at)"
                " VALUES (?,?,?,?,?,?)",
                [(r.get("passage_hash"), r.get("doc_name"), r.get("chunk_idx"), r.get("text", ""),
                  r.get("source_path"), r.get("indexed_at")) for r in load_all_passages()],
            )
            conn.execute("INSERT INTO passages_fts(passages_fts) VALUES('rebuild')")
            conn.execute("INSERT INTO meta VALUES('jsonl_stamp', ?)", (stamp,))
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp, path)
    except sqlite3.OperationalError as exc:
        tmp.unlink(missing_ok=True)
        log.warning("passage search table not built: %s", exc)
    except Exception as exc:                     # pragma: no cover - disk full, permissions
        tmp.unlink(missing_ok=True)
        log.warning("passage search table not built: %s", exc)


def _sidecar() -> sqlite3.Connection | None:
    """Connection to an up-to-date search table, rebuilding it first if the JSONL moved. None if there is no index."""
    if not PASSAGE_INDEX_PATH.exists():
        return None
    path = _sidecar_path()
    for attempt in (0, 1):
        if path.exists():
            conn = _connect(path)
            try:
                row = conn.execute("SELECT value FROM meta WHERE key='jsonl_stamp'").fetchone()
                if row is not None and row[0] == _stamp():
                    return conn
            except sqlite3.Error:
                pass
            conn.close()
        if attempt == 0:
            _build_sidecar()
    raise PassageSearchUnavailable(
        "the passage search table could not be built (SQLite FTS5 may be missing in this Python)")


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def load_all_passages() -> list[PassageRecord]:
    """Load every passage from the index. Returns [] if index absent."""
    if not PASSAGE_INDEX_PATH.exists():
        return []
    records: list[PassageRecord] = []
    for line in PASSAGE_INDEX_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def query_terms(query: str) -> list[str]:
    """Distinct search words of ``query`` (lowercased, order kept), without stop words and 1-character tokens."""
    seen: dict[str, None] = {}
    for t in re.findall(r"\w+", query.lower()):
        if len(t) >= 2 and t not in _STOPWORDS:
            seen.setdefault(t, None)
    return list(seen)


def search_passages(query: str, n: int = 5) -> list[PassageRecord]:
    """
    Top-n passages for ``query`` by BM25 (FTS5; any query word may match, rarer words count more,
    word endings are stemmed). Best first; each result carries ``score`` (larger is better).
    Returns [] when the index is absent or the query has no usable word.
    Raises ``PassageSearchUnavailable`` if this SQLite has no FTS5.
    """
    terms = query_terms(query)
    if not terms or n <= 0:
        return []
    conn = _sidecar()
    if conn is None:
        return []
    match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    try:
        rows = conn.execute(
            "SELECT p.passage_hash, p.doc_name, p.chunk_idx, p.text, p.source_path, p.indexed_at,"
            " -bm25(passages_fts) AS score"
            " FROM passages_fts JOIN passages p ON p.id = passages_fts.rowid"
            " WHERE passages_fts MATCH ? ORDER BY bm25(passages_fts), p.id LIMIT ?",
            (match, int(n)),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise PassageSearchUnavailable(str(exc)) from exc
    finally:
        conn.close()
    return [PassageRecord(passage_hash=r["passage_hash"], doc_name=r["doc_name"], chunk_idx=r["chunk_idx"],
                          text=r["text"], source_path=r["source_path"], indexed_at=r["indexed_at"],
                          score=round(float(r["score"]), 6)) for r in rows]


def resolve_passage_hash(passage_hash: str) -> PassageRecord | None:
    """Look up a passage by its exact hash (keyed lookup; first indexed wins). None if not found."""
    conn = _sidecar()
    if conn is None:
        return None
    try:
        r = conn.execute(
            "SELECT passage_hash, doc_name, chunk_idx, text, source_path, indexed_at FROM passages"
            " WHERE passage_hash = ? ORDER BY id LIMIT 1", (passage_hash,)).fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    return PassageRecord(passage_hash=r["passage_hash"], doc_name=r["doc_name"], chunk_idx=r["chunk_idx"],
                         text=r["text"], source_path=r["source_path"], indexed_at=r["indexed_at"])


def is_hash_format(tag: str) -> bool:
    """Return True if tag looks like a passage hash (12–20 lowercase hex chars)."""
    return bool(_HEX_RE.match(tag.strip().lower()))
