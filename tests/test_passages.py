"""
Tests for ai_hydro/knowledge/passages.py — Phase 2.4 literature grounding.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_hydro.knowledge.passages import (
    PASSAGE_WORDS,
    PASSAGE_OVERLAP,
    _passage_hash,
    _chunk_text,
    _read_doc,
    build_passage_index,
    load_all_passages,
    search_passages,
    resolve_passage_hash,
    is_hash_format,
)


class TestPassageHash(unittest.TestCase):
    def test_deterministic(self):
        h1 = _passage_hash("KGE is a widely used metric.")
        h2 = _passage_hash("KGE is a widely used metric.")
        self.assertEqual(h1, h2)

    def test_normalisation_strips_whitespace(self):
        h1 = _passage_hash("  KGE metric  ")
        h2 = _passage_hash("KGE metric")
        self.assertEqual(h1, h2)

    def test_normalisation_lowercases(self):
        h1 = _passage_hash("KGE")
        h2 = _passage_hash("kge")
        self.assertEqual(h1, h2)

    def test_length_16(self):
        h = _passage_hash("some text")
        self.assertEqual(len(h), 16)

    def test_hex_format(self):
        h = _passage_hash("another piece of text")
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_different_texts_differ(self):
        h1 = _passage_hash("text A")
        h2 = _passage_hash("text B")
        self.assertNotEqual(h1, h2)


class TestChunkText(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        text = "hello world this is short"
        chunks = _chunk_text(text, words=200, overlap=40)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], text)

    def test_empty_text_returns_empty(self):
        self.assertEqual(_chunk_text(""), [])

    def test_overlap_produces_multiple_chunks(self):
        # 20 words with window=10, overlap=5 → step=5 → starts at 0, 5, 10, 15
        words = ["w"] * 20
        text = " ".join(words)
        chunks = _chunk_text(text, words=10, overlap=5)
        self.assertGreater(len(chunks), 1)

    def test_chunks_cover_all_words(self):
        words_list = [f"w{i}" for i in range(50)]
        text = " ".join(words_list)
        chunks = _chunk_text(text, words=10, overlap=3)
        # Last chunk must end with the last word
        last_chunk_words = chunks[-1].split()
        self.assertIn(f"w49", last_chunk_words)

    def test_exact_window_size(self):
        text = " ".join([f"w{i}" for i in range(10)])
        chunks = _chunk_text(text, words=10, overlap=0)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0].split()), 10)


class TestReadDoc(unittest.TestCase):
    def test_read_markdown(self):
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write("# Title\nContent here.")
            path = Path(f.name)
        try:
            text = _read_doc(path)
            self.assertIsNotNone(text)
            self.assertIn("Content here", text)
        finally:
            os.unlink(path)

    def test_read_txt(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            f.write("Plain text content.")
            path = Path(f.name)
        try:
            text = _read_doc(path)
            self.assertIsNotNone(text)
            self.assertIn("Plain text", text)
        finally:
            os.unlink(path)

    def test_unsupported_extension_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".docx", mode="w", delete=False) as f:
            f.write("some content")
            path = Path(f.name)
        try:
            result = _read_doc(path)
            self.assertIsNone(result)
        finally:
            os.unlink(path)

    def test_nonexistent_file_returns_none(self):
        result = _read_doc(Path("/nonexistent/file.md"))
        self.assertIsNone(result)


class TestBuildPassageIndex(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._index_path = Path(self._tmpdir) / "passage_index.jsonl"

    def _patch_index_path(self):
        return mock.patch(
            "ai_hydro.knowledge.passages.PASSAGE_INDEX_PATH",
            self._index_path,
        )

    def _make_doc(self, name: str, content: str) -> Path:
        p = Path(self._tmpdir) / name
        p.write_text(content)
        return p

    def test_builds_index_from_markdown(self):
        doc = self._make_doc("paper.md", "This is a test paper about KGE metrics in hydrology. " * 5)
        with self._patch_index_path():
            result = build_passage_index([doc])
        self.assertIn("n_passages", result)
        self.assertGreater(result["n_passages"], 0)
        self.assertEqual(result["n_docs"], 1)

    def test_index_written_atomically(self):
        doc = self._make_doc("study.md", "Streamflow data from USGS gauges. " * 10)
        with self._patch_index_path():
            build_passage_index([doc])
        self.assertTrue(self._index_path.exists())

    def test_jsonl_format(self):
        doc = self._make_doc("ref.md", "Hydrological signatures include baseflow. " * 3)
        with self._patch_index_path():
            build_passage_index([doc])
        lines = [l for l in self._index_path.read_text().splitlines() if l.strip()]
        for line in lines:
            rec = json.loads(line)
            self.assertIn("passage_hash", rec)
            self.assertIn("doc_name", rec)
            self.assertIn("text", rec)
            self.assertIn("indexed_at", rec)

    def test_skips_unreadable_extensions(self):
        doc = self._make_doc("paper.md", "Valid content. " * 5)
        bad = self._make_doc("data.csv", "col1,col2\n1,2")
        with self._patch_index_path():
            # csv is not in the accepted extensions, so caller shouldn't pass it,
            # but even if they do, _read_doc returns None for unknown suffixes
            result = build_passage_index([doc, bad])
        # Only the md file is read
        self.assertEqual(result["n_docs"], 1)

    def test_passage_hash_stable_across_rebuilds(self):
        content = "Nash-Sutcliffe efficiency is a widely used metric for model calibration."
        doc = self._make_doc("note.md", content)
        with self._patch_index_path():
            build_passage_index([doc])
            passages_1 = load_all_passages()
            build_passage_index([doc])  # rebuild
            passages_2 = load_all_passages()
        hashes_1 = {p["passage_hash"] for p in passages_1}
        hashes_2 = {p["passage_hash"] for p in passages_2}
        self.assertEqual(hashes_1, hashes_2)


class TestLoadAndSearch(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._index_path = Path(self._tmpdir) / "passage_index.jsonl"

    def _patch_index_path(self):
        return mock.patch(
            "ai_hydro.knowledge.passages.PASSAGE_INDEX_PATH",
            self._index_path,
        )

    def _build(self, docs: dict[str, str]):
        paths = []
        for name, content in docs.items():
            p = Path(self._tmpdir) / name
            p.write_text(content)
            paths.append(p)
        with self._patch_index_path():
            build_passage_index(paths)

    def test_load_all_returns_records(self):
        self._build({"hydro.md": "Streamflow data analysis. " * 5})
        with self._patch_index_path():
            recs = load_all_passages()
        self.assertGreater(len(recs), 0)
        self.assertIn("passage_hash", recs[0])

    def test_load_returns_empty_when_index_absent(self):
        with self._patch_index_path():
            recs = load_all_passages()
        self.assertEqual(recs, [])

    def test_search_returns_relevant_passages(self):
        self._build({
            "kge.md": "KGE Kling-Gupta efficiency metric for model evaluation. " * 8,
            "flow.md": "Streamflow discharge measurement at USGS gauges. " * 8,
        })
        with self._patch_index_path():
            results = search_passages("KGE efficiency metric", n=3)
        self.assertGreater(len(results), 0)
        # The KGE paper should rank first
        self.assertIn("kge", results[0]["doc_name"])

    def test_search_returns_empty_when_no_index(self):
        with self._patch_index_path():
            results = search_passages("anything")
        self.assertEqual(results, [])

    def test_search_n_limits_results(self):
        content = "word " * 300
        self._build({"big.md": content})
        with self._patch_index_path():
            results = search_passages("word", n=2)
        self.assertLessEqual(len(results), 2)


class TestResolvePassageHash(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._index_path = Path(self._tmpdir) / "passage_index.jsonl"

    def _patch_index_path(self):
        return mock.patch(
            "ai_hydro.knowledge.passages.PASSAGE_INDEX_PATH",
            self._index_path,
        )

    def test_resolve_known_hash(self):
        content = "The KGE metric decomposes bias, correlation, and variability."
        p = Path(self._tmpdir) / "metric.md"
        p.write_text(content)
        with self._patch_index_path():
            build_passage_index([p])
            passages = load_all_passages()
        self.assertGreater(len(passages), 0)
        known_hash = passages[0]["passage_hash"]
        with self._patch_index_path():
            rec = resolve_passage_hash(known_hash)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["passage_hash"], known_hash)

    def test_resolve_unknown_hash_returns_none(self):
        p = Path(self._tmpdir) / "doc.md"
        p.write_text("Some content here.")
        with self._patch_index_path():
            build_passage_index([p])
            result = resolve_passage_hash("deadbeef00000000")
        self.assertIsNone(result)

    def test_resolve_on_empty_index_returns_none(self):
        with self._patch_index_path():
            result = resolve_passage_hash("abcdef1234567890")
        self.assertIsNone(result)


class TestIsHashFormat(unittest.TestCase):
    def test_valid_16_char_hex(self):
        self.assertTrue(is_hash_format("abcdef1234567890"))

    def test_valid_12_char_hex(self):
        self.assertTrue(is_hash_format("abcdef123456"))

    def test_valid_20_char_hex(self):
        self.assertTrue(is_hash_format("abcdef12345678901234"))

    def test_too_short_returns_false(self):
        self.assertFalse(is_hash_format("abcdef"))

    def test_too_long_returns_false(self):
        self.assertFalse(is_hash_format("a" * 21))

    def test_author_year_format_returns_false(self):
        self.assertFalse(is_hash_format("Nash1970"))

    def test_uppercase_normalised_to_lowercase(self):
        # is_hash_format normalises to lowercase before matching — uppercase hex accepted
        self.assertTrue(is_hash_format("ABCDEF1234567890"))

    def test_non_hex_chars_return_false(self):
        # 'g' and 'z' are not hex digits
        self.assertFalse(is_hash_format("abcdefg234567890"))


class TestAuditorLitResolution(unittest.TestCase):
    """Integration test: auditor resolver picks up passage hash [lit:...] tags."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._index_path = Path(self._tmpdir) / "passage_index.jsonl"

    def _patch_index_path(self):
        return mock.patch(
            "ai_hydro.knowledge.passages.PASSAGE_INDEX_PATH",
            self._index_path,
        )

    def test_resolvable_hash_lit_increments_lit_resolved(self):
        # Build an index so the hash exists
        content = "The KGE metric (0.82) is a standard benchmark."
        p = Path(self._tmpdir) / "paper.md"
        p.write_text(content)
        with self._patch_index_path():
            build_passage_index([p])
            passages = load_all_passages()
        self.assertGreater(len(passages), 0)
        known_hash = passages[0]["passage_hash"]

        from ai_hydro.audit.resolver import resolve_prose

        prose = f"The model achieved 0.82 [lit:{known_hash}] efficiency."
        with self._patch_index_path():
            with mock.patch("ai_hydro.audit.resolver._load_run_log", return_value={}):
                with mock.patch("ai_hydro.audit.resolver._load_claims", return_value={}):
                    report = resolve_prose(prose, "sess-001")

        self.assertTrue(report.passed)
        self.assertEqual(report.lit_span_count, 1)
        self.assertEqual(report.lit_resolved_count, 1)
        self.assertEqual(report.lit_advisories, [])

    def test_unresolvable_hash_lit_advisory_not_blocking(self):
        # Build index but use a hash that doesn't exist in it
        p = Path(self._tmpdir) / "paper.md"
        p.write_text("Some content.")
        with self._patch_index_path():
            build_passage_index([p])

        from ai_hydro.audit.resolver import resolve_prose

        fake_hash = "deadbeef12345678"
        prose = f"The model achieved 0.82 [lit:{fake_hash}] efficiency."
        with self._patch_index_path():
            with mock.patch("ai_hydro.audit.resolver._load_run_log", return_value={}):
                with mock.patch("ai_hydro.audit.resolver._load_claims", return_value={}):
                    report = resolve_prose(prose, "sess-002")

        # NOT blocked — advisory only
        self.assertTrue(report.passed)
        self.assertEqual(report.lit_span_count, 1)
        self.assertEqual(report.lit_resolved_count, 0)
        self.assertEqual(len(report.lit_advisories), 1)
        self.assertEqual(report.lit_advisories[0].kind, "lit_unresolvable")

    def test_legacy_author_year_lit_passes_without_check(self):
        from ai_hydro.audit.resolver import resolve_prose

        prose = "The model achieved 0.82 [lit:Nash1970] efficiency."
        with self._patch_index_path():  # index absent — doesn't matter
            with mock.patch("ai_hydro.audit.resolver._load_run_log", return_value={}):
                with mock.patch("ai_hydro.audit.resolver._load_claims", return_value={}):
                    report = resolve_prose(prose, "sess-003")

        self.assertTrue(report.passed)
        self.assertEqual(report.lit_span_count, 1)
        # Non-hash format: no resolution attempt, no advisory
        self.assertEqual(report.lit_advisories, [])


# ---------------------------------------------------------------------------
# BM25 ranking (SQLite FTS5) -- pytest style
# ---------------------------------------------------------------------------

import sqlite3

import pytest

from ai_hydro.knowledge import passages as P

try:
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    _HAS_FTS5 = True
except sqlite3.OperationalError:
    _HAS_FTS5 = False

needs_fts5 = pytest.mark.skipif(not _HAS_FTS5, reason="this SQLite has no FTS5")


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A tiny library whose passages are SYNTHETIC text written for these tests."""
    monkeypatch.setattr(P, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(P, "PASSAGE_INDEX_PATH", tmp_path / "passage_index.jsonl")
    docs = tmp_path / "docs"
    docs.mkdir()
    # one real-looking passage on the topic, one short passage that only repeats generic words
    (docs / "bfi.md").write_text(
        "The baseflow index is the long-term ratio of baseflow to total streamflow. "
        "Recursive digital filters separate the slow component of the hydrograph from quickflow. "
        "A filter parameter near 0.925 is common for daily data. " + "Further text. " * 40)
    (docs / "junk.md").write_text("workflow overflow flow index flow index flow index")
    (docs / "other.md").write_text("Kling-Gupta efficiency combines correlation, bias and variability ratios. " * 12)
    P.build_passage_index(sorted(docs.glob("*.md")))
    return tmp_path


@needs_fts5
def test_bm25_ranks_the_real_passage_above_a_short_junk_one(lib):
    """The substring-overlap scorer put a four-word passage of 'flow'/'index' fragments first."""
    top = P.search_passages("baseflow index", n=3)
    assert top[0]["doc_name"] == "bfi.md"
    assert top[0]["score"] > 0 and all(top[0]["score"] > r["score"] for r in top[1:])


@needs_fts5
def test_search_is_stemmed_ranked_and_deterministic(lib):
    a = P.search_passages("separating baseflows", n=3)
    assert a and a[0]["doc_name"] == "bfi.md"            # 'separating' ~ 'separate', 'baseflows' ~ 'baseflow'
    assert a == P.search_passages("separating baseflows", n=3)
    assert [r["score"] for r in a] == sorted((r["score"] for r in a), reverse=True)


@needs_fts5
def test_search_does_not_match_a_synonym_it_is_keyword_not_semantic(lib):
    assert P.search_passages("discharge", n=3) == []


@needs_fts5
def test_query_with_no_usable_word_returns_nothing(lib):
    assert P.search_passages("the of a", n=3) == [] and P.search_passages("", n=3) == []
    assert P.search_passages('"unbalanced quote OR (', n=3) is not None      # FTS syntax characters are not interpreted


@needs_fts5
def test_hash_lookup_is_keyed_and_unchanged_by_the_ranking_method(lib):
    rec = P.search_passages("Kling-Gupta efficiency", n=1)[0]
    got = P.resolve_passage_hash(rec["passage_hash"])
    assert got["text"] == rec["text"] and got["doc_name"] == "other.md"
    assert P.resolve_passage_hash("0" * 16) is None
    # passage_hash is the unchanged SHA-256[:16] of normalised text
    assert rec["passage_hash"] == P._passage_hash(rec["text"])
    # pinned from the pre-FTS5 implementation (origin/main 733d83a): hashes in stored claims must not move
    assert P._passage_hash("KGE is a widely used metric.") == "19639d48af1beb4a"
    assert P._passage_hash("The baseflow index is the long-term ratio of baseflow to total streamflow.") == "d6a14d912da0e99e"


@needs_fts5
def test_search_never_writes_the_derived_table_it_is_built_in_memory_when_missing_or_stale(lib):
    side = P._sidecar_path()
    assert side.exists()                               # written by build_passage_index
    side.unlink()
    assert P.search_passages("baseflow", n=1)[0]["doc_name"] == "bfi.md"
    assert not side.exists()                           # a read-only call wrote nothing
    # the JSONL is the record: append a passage by hand and the next search sees it
    rec = dict(P.load_all_passages()[0], passage_hash="a" * 16, doc_name="added.md",
               text="tidal backwater affects stage discharge ratings")
    with open(P.PASSAGE_INDEX_PATH, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    assert P.search_passages("backwater", n=1)[0]["doc_name"] == "added.md"
    assert P.resolve_passage_hash("a" * 16)["doc_name"] == "added.md"
    assert not side.exists()
    # a stale file (built from older JSONL) is ignored, not trusted and not rewritten
    P._build_sidecar()
    with open(P.PASSAGE_INDEX_PATH, "a") as fh:
        fh.write(json.dumps(dict(rec, passage_hash="b" * 16, text="levee overtopping")) + "\n")
    before = side.stat().st_mtime_ns
    assert P.search_passages("overtopping", n=1)[0]["passage_hash"] == "b" * 16
    assert side.stat().st_mtime_ns == before


@needs_fts5
def test_rebuilding_the_index_rewrites_the_table(lib):
    side = P._sidecar_path()
    side.unlink()
    P.build_passage_index(sorted((lib / "docs").glob("*.md")))
    assert side.exists() and P.search_passages("baseflow", n=1)[0]["doc_name"] == "bfi.md"


def test_absent_index_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PASSAGE_INDEX_PATH", tmp_path / "none.jsonl")
    assert P.search_passages("anything") == [] and P.resolve_passage_hash("a" * 16) is None


def test_the_old_module_name_is_the_same_module():
    import ai_hydro.knowledge.embeddings as old

    assert old is P


@needs_fts5
def test_hash_resolution_survives_a_python_without_fts5(lib, monkeypatch):
    rec = P.search_passages("Kling-Gupta efficiency", n=1)[0]

    def no_fts(*a, **k):
        raise P.PassageSearchUnavailable("no fts5")
    monkeypatch.setattr(P, "_sidecar", no_fts)
    assert P.resolve_passage_hash(rec["passage_hash"])["doc_name"] == "other.md"
    assert P.resolve_passage_hash("0" * 16) is None
    with pytest.raises(P.PassageSearchUnavailable):
        P.search_passages("baseflow")           # ranking really does need it


@needs_fts5
def test_concurrent_builds_do_not_share_a_temp_file(lib, monkeypatch):
    import tempfile
    names = []
    real = tempfile.mkstemp

    def spy(*a, **k):
        fd, name = real(*a, **k)
        names.append(name)
        return fd, name
    monkeypatch.setattr(tempfile, "mkstemp", spy)
    docs = sorted((lib / "docs").glob("*.md"))
    P.build_passage_index(docs)
    P.build_passage_index(docs)
    assert len(names) == len(set(names)) == 4                    # jsonl + sqlite per build, all distinct
    assert not [p for p in lib.iterdir() if p.name.endswith(".tmp")]    # and cleaned up
