"""The passage-search benchmark harness runs, and its seed gold set is well formed.

This does not assert that BM25 beats grep: that is the question the human-written gold set is for.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "bench" / "literature" / "gold_seed.json"

try:
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    HAS_FTS5 = True
except sqlite3.OperationalError:
    HAS_FTS5 = False


def _run_module():
    spec = importlib.util.spec_from_file_location("lit_bench_run", ROOT / "bench" / "literature" / "run.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.skipif(not HAS_FTS5, reason="this SQLite has no FTS5")
def test_seed_gold_is_well_formed_and_the_harness_reports_all_rankers():
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    res = _run_module().evaluate(gold)
    assert res["n_questions"] == len(gold["questions"]) >= 20
    assert res["questions_with_no_relevant_passage"] == [], "a gold question whose phrase is in no passage can never score"
    assert set(res["recall_percent"]) == {"bm25", "grep", "legacy"}
    for r in res["recall_percent"].values():
        assert 0.0 <= r["recall@1"] <= r["recall@3"] <= r["recall@5"] <= 100.0
    # the retired scorer ranks substring fragments above real matches; a wide gap, not a close call
    assert res["recall_percent"]["bm25"]["recall@5"] - res["recall_percent"]["legacy"]["recall@5"] > 30
    assert res["bm25_minus_grep_at_5_points"] == round(
        res["recall_percent"]["bm25"]["recall@5"] - res["recall_percent"]["grep"]["recall@5"], 1)
