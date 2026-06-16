"""
Passage-level index for the papers library.

Documents are split into ~200-word passages. Each passage gets a
deterministic `passage_hash` (SHA-256[:16] of normalised text) that
serves as its immutable fingerprint — suitable for use as an
EvidenceSpan(source_type="paper", source_id=passage_hash).

The index lives at ~/.aihydro/knowledge/passage_index.jsonl (one JSON
object per line, POSIX-atomic writes).  No external embeddings model is
required; search is term-frequency overlap.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

log = logging.getLogger("ai_hydro.knowledge.embeddings")

KNOWLEDGE_DIR = Path.home() / ".aihydro" / "knowledge"
PASSAGE_INDEX_PATH = KNOWLEDGE_DIR / "passage_index.jsonl"

PASSAGE_WORDS = 200          # target words per passage
PASSAGE_OVERLAP = 40         # sliding overlap in words
_HEX_RE = re.compile(r"^[0-9a-f]{12,20}$")   # looks like a passage hash


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------

class PassageRecord(TypedDict):
    passage_hash: str        # SHA-256[:16] of normalised text
    doc_name: str            # filename (basename)
    chunk_idx: int           # 0-based chunk index within doc
    text: str                # the passage text (verbatim from source)
    source_path: str         # absolute path to source document
    indexed_at: str          # ISO-8601 timestamp


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

    return {
        "n_passages": len(records),
        "n_docs": n_docs,
        "index_path": str(PASSAGE_INDEX_PATH),
    }


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


def _score(passage_text: str, query_terms: list[str]) -> float:
    """Simple term-frequency overlap score (normalised by passage length)."""
    words = passage_text.lower().split()
    if not words:
        return 0.0
    hits = sum(1 for w in words if any(t in w for t in query_terms))
    return hits / len(words)


def search_passages(query: str, n: int = 5) -> list[PassageRecord]:
    """
    Return the top-n passages from the index ranked by term-overlap with query.
    Returns [] if the index is empty or absent.
    """
    passages = load_all_passages()
    if not passages:
        return []
    terms = [t.lower() for t in re.split(r"\W+", query) if len(t) >= 3]
    if not terms:
        return passages[:n]
    scored = [(p, _score(p["text"], terms)) for p in passages]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in scored[:n]]


def resolve_passage_hash(passage_hash: str) -> PassageRecord | None:
    """Look up a passage by its exact hash. Returns None if not found."""
    for rec in load_all_passages():
        if rec["passage_hash"] == passage_hash:
            return rec
    return None


def is_hash_format(tag: str) -> bool:
    """Return True if tag looks like a passage hash (12–20 lowercase hex chars)."""
    return bool(_HEX_RE.match(tag.strip().lower()))
